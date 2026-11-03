#!/usr/bin/env python3
"""
Drive a full MiroFish run against a running backend, end to end, over its HTTP API:

    seed document -> ontology -> Zep graph -> simulation (agents + config)
    -> OASIS run -> ReportAgent report -> optional interview of all agents

Only the standard library is used, so it runs with any Python 3.9+.

Usage (backend must already be running, e.g. `npm run backend`):

    python3 scenarios/geoapi/run_scenario.py                 # Georgian, 10 rounds
    python3 scenarios/geoapi/run_scenario.py --max-rounds 20 --platform reddit
    python3 scenarios/geoapi/run_scenario.py --resume-simulation sim_xxx   # report only

Everything the run produces is written under --out (default scenarios/geoapi/output/<timestamp>/).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEFAULT_INTERVIEW = (
    "გამოიყენებდი GeoAPI.ge-ს? რომელ გეგმას აირჩევდი (Free, Starter 51 ₾, Growth 159 ₾) "
    "და რა უნდა შეიცვალოს, რომ გადაიხადო? უპასუხე მოკლედ, 2-3 წინადადებით."
)


class Api:
    def __init__(self, base: str, locale: str, timeout: int = 120):
        self.base = base.rstrip("/")
        self.locale = locale
        self.timeout = timeout

    def _send(self, req: urllib.request.Request) -> dict:
        req.add_header("Accept-Language", self.locale)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{req.get_method()} {req.full_url} -> HTTP {e.code}: {body[:2000]}") from None
        payload = json.loads(body)
        if isinstance(payload, dict) and payload.get("success") is False:
            raise RuntimeError(f"{req.get_method()} {req.full_url} failed: {payload.get('error')}")
        return payload

    def get(self, path: str) -> dict:
        return self._send(urllib.request.Request(self.base + path, method="GET"))

    def post(self, path: str, data: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        return self._send(req)

    def post_multipart(self, path: str, fields: dict, files: list[Path]) -> dict:
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )
        for f in files:
            ctype = mimetypes.guess_type(f.name)[0] or "text/markdown"
            parts.append(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{f.name}"\r\n'
                    f"Content-Type: {ctype}\r\n\r\n"
                ).encode("utf-8")
                + f.read_bytes()
                + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(
            self.base + path,
            data=b"".join(parts),
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return self._send(req)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def save(out: Path, name: str, data) -> None:
    path = out / name
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def poll(fetch, done, label: str, interval: float = 5.0, limit_s: float = 3 * 3600):
    """Call fetch() until done(data) is True; raise on a failed status."""
    start = time.time()
    last = None
    while True:
        data = fetch()
        status = data.get("status") or data.get("runner_status")
        progress = data.get("progress", data.get("progress_percent"))
        message = data.get("message") or ""
        line = f"{label}: {status} {progress if progress is not None else ''} {message}".strip()
        if line != last:
            log(line)
            last = line
        if status == "failed":
            raise RuntimeError(f"{label} failed: {data.get('error') or message}")
        if done(data):
            return data
        if time.time() - start > limit_s:
            raise TimeoutError(f"{label} did not finish within {limit_s:.0f}s")
        time.sleep(interval)


def build_graph(api: Api, args, out: Path) -> tuple[str, str]:
    requirement = (HERE / args.requirement).read_text(encoding="utf-8").strip()
    seeds = [HERE / s for s in args.seed]
    log(f"1/6 ontology from {', '.join(s.name for s in seeds)}")
    onto = api.post_multipart(
        "/api/graph/ontology/generate",
        {"simulation_requirement": requirement, "project_name": args.project_name},
        seeds,
    )["data"]
    save(out, "01_ontology.json", onto)
    project_id = onto["project_id"]
    log(
        f"   project {project_id}: {len(onto['ontology'].get('entity_types', []))} entity types, "
        f"{len(onto['ontology'].get('edge_types', []))} edge types"
    )

    log("2/6 building Zep graph")
    task_id = api.post("/api/graph/build", {"project_id": project_id})["data"]["task_id"]
    poll(lambda: api.get(f"/api/graph/task/{task_id}")["data"], lambda d: d["status"] == "completed", "graph")
    project = api.get(f"/api/graph/project/{project_id}")["data"]
    graph_id = project["graph_id"]
    graph = api.get(f"/api/graph/data/{graph_id}")["data"]
    save(out, "02_graph.json", graph)
    log(f"   graph {graph_id}: {graph.get('node_count', len(graph.get('nodes', [])))} nodes")
    return project_id, graph_id


def prepare_simulation(api: Api, args, out: Path, project_id: str, graph_id: str) -> str:
    log("3/6 creating simulation and generating agent profiles + config")
    sim = api.post(
        "/api/simulation/create",
        {
            "project_id": project_id,
            "graph_id": graph_id,
            "enable_twitter": args.platform in ("parallel", "twitter"),
            "enable_reddit": args.platform in ("parallel", "reddit"),
        },
    )["data"]
    sim_id = sim["simulation_id"]
    prep = api.post(
        "/api/simulation/prepare",
        {"simulation_id": sim_id, "use_llm_for_profiles": True, "parallel_profile_count": args.parallel_profiles},
    )["data"]
    if not prep.get("already_prepared"):
        task_id = prep["task_id"]
        poll(
            lambda: api.post("/api/simulation/prepare/status", {"task_id": task_id, "simulation_id": sim_id})["data"],
            lambda d: d["status"] in ("completed", "ready"),
            "prepare",
        )
    for platform in ("reddit", "twitter"):
        try:
            save(out, f"03_profiles_{platform}.json", api.get(f"/api/simulation/{sim_id}/profiles?platform={platform}"))
        except RuntimeError:
            pass
    save(out, "03_config.json", api.get(f"/api/simulation/{sim_id}/config"))
    log(f"   simulation {sim_id} ready")
    return sim_id


def run_simulation(api: Api, args, out: Path, sim_id: str) -> None:
    log(f"4/6 running OASIS ({args.platform}, max {args.max_rounds} rounds)")
    api.post(
        "/api/simulation/start",
        {"simulation_id": sim_id, "platform": args.platform, "max_rounds": args.max_rounds, "force": True},
    )
    poll(
        lambda: api.get(f"/api/simulation/{sim_id}/run-status")["data"],
        lambda d: d.get("runner_status") in ("completed", "stopped"),
        "simulation",
        interval=10,
    )
    for platform in ("twitter", "reddit"):
        try:
            save(out, f"04_posts_{platform}.json", api.get(f"/api/simulation/{sim_id}/posts?platform={platform}&limit=500"))
        except RuntimeError:
            pass
    save(out, "04_actions.json", api.get(f"/api/simulation/{sim_id}/actions?limit=2000"))
    save(out, "04_agent_stats.json", api.get(f"/api/simulation/{sim_id}/agent-stats"))


def generate_report(api: Api, out: Path, sim_id: str) -> str:
    log("5/6 generating report")
    gen = api.post("/api/report/generate", {"simulation_id": sim_id})["data"]
    report_id = gen["report_id"]
    if gen.get("task_id"):
        task_id = gen["task_id"]
        poll(
            lambda: api.post("/api/report/generate/status", {"task_id": task_id, "simulation_id": sim_id})["data"],
            lambda d: d["status"] == "completed",
            "report",
            interval=10,
        )
    report = api.get(f"/api/report/{report_id}")["data"]
    save(out, "05_report.json", report)
    save(out, "05_report.md", report.get("markdown_content") or "")
    log(f"   report {report_id} -> {out / '05_report.md'}")
    return report_id


def interview(api: Api, out: Path, sim_id: str, question: str) -> None:
    log("6/6 interviewing all agents")
    try:
        res = api.post("/api/simulation/interview/all", {"simulation_id": sim_id, "prompt": question, "timeout": 300})
        save(out, "06_interviews.json", res)
    except RuntimeError as e:  # needs the simulation environment to still be alive
        log(f"   interview skipped: {e}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://localhost:5001")
    p.add_argument("--locale", default="ka", help="Accept-Language sent to the backend (ka, en, zh)")
    p.add_argument("--seed", nargs="+", default=["seed_geoapi_ka.md"], help="seed files, relative to this folder")
    p.add_argument("--requirement", default="requirement_ka.txt")
    p.add_argument("--project-name", default="GeoAPI.ge launch reaction")
    p.add_argument("--platform", choices=["parallel", "twitter", "reddit"], default="parallel")
    p.add_argument("--max-rounds", type=int, default=10, help="keep small first: LLM cost grows with rounds x agents")
    p.add_argument("--parallel-profiles", type=int, default=5)
    p.add_argument("--interview", default=DEFAULT_INTERVIEW, help="question for all agents; empty string to skip")
    p.add_argument("--resume-simulation", help="skip to the report for an existing simulation id")
    p.add_argument("--out", type=Path)
    args = p.parse_args()

    out = args.out or HERE / "output" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    api = Api(args.base_url, args.locale)
    started = time.time()

    try:
        api.get("/health")
    except Exception as e:  # noqa: BLE001
        log(f"backend not reachable at {args.base_url}: {e}")
        return 2

    try:
        if args.resume_simulation:
            sim_id = args.resume_simulation
        else:
            project_id, graph_id = build_graph(api, args, out)
            sim_id = prepare_simulation(api, args, out, project_id, graph_id)
            run_simulation(api, args, out, sim_id)
        if args.interview:
            interview(api, out, sim_id, args.interview)
        generate_report(api, out, sim_id)
    except (RuntimeError, TimeoutError) as e:
        log(f"stopped: {e}")
        log(f"partial results in {out}")
        return 1

    log(f"done in {(time.time() - started) / 60:.1f} min; results in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
