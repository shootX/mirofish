---
name: run-mirofish
description: Build, run, and drive MiroFish (Vue frontend on :3000 + Flask backend on :5001). Use when asked to start or launch MiroFish, run its backend tests, take a screenshot of its UI, upload a seed document and start a simulation, or check that a frontend/backend change works in the running app.
---

MiroFish is a Vue 3 + Vite frontend (`frontend/`, port 3000) and a Flask backend
(`backend/`, port 5001, Python via `uv`). Launch both with `npm run dev`, then drive the UI
headlessly with the Playwright driver `.claude/skills/run-mirofish/driver.mjs`
(`chromium-cli` is not installed in these containers). All paths are relative to the repo root.

## Prerequisites

Node 18+, `uv`, and a Python 3.11 or 3.12 interpreter (`pyproject.toml` pins `<3.13`; the
container's default `python3` is 3.13, so pass `--python 3.12`). Playwright and Chromium are
preinstalled in Claude cloud containers. Nothing needed `apt-get`.

## Setup

```bash
npm run setup                                # root + frontend node_modules
(cd backend && uv sync --python 3.12)        # backend .venv, includes pytest
```

The backend **exits at startup** unless `LLM_API_KEY` and `ZEP_API_KEY` are set. For UI work
without real keys, use placeholders (`.env` is gitignored):

```bash
cp .env.example .env
sed -i '/LLM_BOOST/d; s/^LLM_API_KEY=.*/LLM_API_KEY=placeholder-not-a-real-key/; s/^ZEP_API_KEY=.*/ZEP_API_KEY=placeholder-not-a-real-key/' .env
```

With placeholders the whole UI renders and the API answers, but any step that calls the LLM
(ontology generation, the first step after "Start Engine") fails with HTTP 401. Real keys:
an OpenAI-compatible LLM (default `.env` = Alibaba DashScope `qwen-plus`) and Zep Cloud
(`api.getzep.com`, sent as `Authorization: Api-Key <key>`).

## Run (agent path)

Start both servers in the background (Bash `run_in_background`, or `&`), then poll:

```bash
npm run dev </dev/null > /tmp/mirofish-dev.log 2>&1 &
timeout 120 bash -c 'until curl -sf localhost:5001/health >/dev/null && curl -sf localhost:3000 >/dev/null; do sleep 1; done'
```

Drive it:

```bash
node .claude/skills/run-mirofish/driver.mjs smoke
```

`smoke` checks `/health`, loads the home page, switches the language dropdown to English,
asserts "Start Engine" is disabled with no input, prints console errors, and writes
`home.png` + `home-en.png`.

```bash
printf '# City council vote\n\nThe council votes next week on banning cars from the old town. Shop owners oppose it; cyclists support it.\n' > /tmp/seed.md
LOCALE=en START_TIMEOUT_MS=90000 node .claude/skills/run-mirofish/driver.mjs start /tmp/seed.md "Predict how public opinion evolves over the week before the vote."
```

`start` is the real user flow: it uploads the seed file, types the prompt, clicks Start Engine,
waits for the `/process/...` page to reach `Building Graph` / `Ready` / `Error`, then prints
the status, the URL, and the last lines of the on-page SYSTEM DASHBOARD log. It saves
`start-filled.png` and `start-result.png`. With placeholder keys, the expected output is
`status: Error` and `LLM provider request failed (HTTP 401)`.

```bash
node .claude/skills/run-mirofish/driver.mjs shot /simulation/<id> sim
```

| command | what it does |
|---|---|
| `smoke` | health check, home page, switch to English, 2 screenshots |
| `start <seed> "<prompt>"` | upload → prompt → Start Engine → wait for status → screenshot |
| `shot <route> [name]` | screenshot any route (`/process/<proj_id>`, `/simulation/<id>`, `/report/<id>`, `/interaction/<id>`) |

Env: `SHOTS_DIR` (default `/tmp/mirofish-shots/`), `LOCALE=en|zh` (the app defaults to `zh`),
`SCALE=2` for screenshots a person will see, `UNTIL=ready` to make `start` wait through the
Zep graph build (needs real keys; raise `START_TIMEOUT_MS`), and `BASE_URL`/`API_URL`.

API without the browser: `curl -s localhost:5001/api/simulation/list` → `{"count":0,"data":[],"success":true}`.
Backend logs (both servers, prefixed `[backend]`/`[frontend]`) are in `/tmp/mirofish-dev.log`.

Stop both (concurrently `--kill-others` takes the other one down too):

```bash
fuser -k 3000/tcp 5001/tcp
```

## Run (human path)

`npm run dev` → open http://localhost:3000. Vite tries to open a browser itself (`open: true`).

## Test

```bash
(cd backend && uv run pytest -q)     # 130 passed in ~3s; offline, mocks LLM/Zep
```

Recent PRs mostly touch backend internals (ontology generation, Zep client, profile
normalization), so for those the tests in `backend/tests/` are the main check. Use the
driver when the change is in `frontend/` or in an API response shape the UI shows.

## Gotchas

- **`.env` beats your shell.** `backend/app/config.py` calls `load_dotenv(..., override=True)`,
  so `export LLM_API_KEY=...` does nothing while `.env` sets it. Edit `.env` instead.
- **`/process/:id` renders `views/MainView.vue`, not `views/Process.vue`.** `Process.vue` is
  not routed anywhere, so edits to it never show up.
- **A failed step keeps spinning.** After a 401, the "01 Ontology Generation" card still shows
  GENERATING with a spinner. The real state is the header `.status-indicator` (`Error`) and the
  SYSTEM DASHBOARD log. That indicator's text is hard-coded English in every locale, which is
  why the driver waits on it.
- **The browser calls the backend directly.** `frontend/src/api/index.js` uses
  `VITE_API_BASE_URL || http://localhost:5001`, not the Vite `/api` proxy. If the UI is served
  from another host, set `VITE_API_BASE_URL`.
- **Only two languages show.** `locales/languages.json` lists 7, but only `en.json` and
  `zh.json` exist, so the dropdown shows 中文 / English.
- **The seed `<input type=file>` is `display:none`.** Use Playwright `setInputFiles` on
  `input[type="file"]`. Clicking the drop zone opens an OS dialog you can't reach.
- **Ontology 401 surfaces as HTTP 502** from `POST /api/graph/ontology/generate`. The provider
  status is only in the backend log (`Ontology provider request failed: type=AuthenticationError status=401`).

## Troubleshooting

- **Backend prints `配置错误: - LLM_API_KEY 未配置 - ZEP_API_KEY 未配置` and exits**: there is no
  `.env`, or the keys in it are empty. Run the Setup `cp`/`sed` lines.
- **`[frontend] Error: spawn xdg-open ENOENT`**: Vite's `open: true` with no desktop. It's harmless,
  and the server keeps running.
- **Many `SyntaxWarning: invalid escape sequence` lines from `zep_cloud`** at backend start:
  noise from the pinned SDK. Ignore them.
