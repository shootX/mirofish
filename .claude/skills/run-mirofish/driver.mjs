#!/usr/bin/env node
// MiroFish headless driver: drives the running Vue frontend with Playwright.
// Usage (from the repo root, with `npm run dev` already serving :3000/:5001):
//   node .claude/skills/run-mirofish/driver.mjs smoke
//   node .claude/skills/run-mirofish/driver.mjs start <seed.md|.txt|.pdf> "<prediction prompt>"
//   node .claude/skills/run-mirofish/driver.mjs shot <route> [name]
// Env: BASE_URL (http://localhost:3000), API_URL (http://localhost:5001),
//      SHOTS_DIR (/tmp/mirofish-shots), LOCALE (zh|en, default: app default zh),
//      SCALE (deviceScaleFactor, default 1; use 2 for screenshots a person will see).
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

// Playwright is preinstalled globally (NODE_PATH); ESM `import` ignores NODE_PATH,
// a CJS require does not.
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';
const API_URL = process.env.API_URL || 'http://localhost:5001';
const SHOTS_DIR = process.env.SHOTS_DIR || '/tmp/mirofish-shots';
const SCALE = Number(process.env.SCALE || 1);
fs.mkdirSync(SHOTS_DIR, { recursive: true });

async function openPage() {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: SCALE });
  if (process.env.LOCALE) {
    // The app reads its locale from localStorage('locale') at boot.
    await ctx.addInitScript((l) => localStorage.setItem('locale', l), process.env.LOCALE);
  }
  const page = await ctx.newPage();
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push(String(e)));
  return { browser, page, errors };
}

async function shot(page, name) {
  const file = path.join(SHOTS_DIR, `${name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  console.log(`screenshot: ${file}`);
  return file;
}

async function health() {
  const r = await fetch(`${API_URL}/health`).catch((e) => ({ ok: false, statusText: e.message }));
  const body = r.ok ? await r.json() : r.statusText;
  console.log(`backend /health: ${JSON.stringify(body)}`);
  if (!r.ok) throw new Error('backend not reachable - is `npm run dev` running?');
}

async function smoke() {
  await health();
  const { browser, page, errors } = await openPage();
  await page.goto(BASE_URL + '/', { waitUntil: 'networkidle' });
  await page.locator('.start-engine-btn').waitFor();
  console.log(`title: ${await page.title()}`);
  await shot(page, 'home');
  // Real interaction: switch language via the header dropdown.
  await page.click('.switcher-trigger');
  await page.click('.switcher-option:has-text("English")');
  await page.waitForFunction(() => localStorage.getItem('locale') === 'en');
  console.log(`start button after switch: "${(await page.innerText('.start-engine-btn')).trim()}"`);
  await shot(page, 'home-en');
  // Start button must stay disabled until a seed file + prompt are given.
  console.log(`start button disabled (no input): ${await page.isDisabled('.start-engine-btn')}`);
  console.log(`console errors: ${JSON.stringify(errors)}`);
  await browser.close();
}

async function start(seed, prompt) {
  if (!seed || !prompt) throw new Error('usage: start <seed-file> "<prompt>"');
  await health();
  const { browser, page, errors } = await openPage();
  await page.goto(BASE_URL + '/', { waitUntil: 'networkidle' });
  // The <input type=file> is display:none; setInputFiles works on hidden inputs.
  await page.setInputFiles('input[type="file"]', path.resolve(seed));
  await page.fill('textarea.code-input', prompt);
  await shot(page, 'start-filled');
  await page.click('.start-engine-btn');
  await page.waitForURL(/\/process\/new/);
  // /process/:id renders MainView.vue. Ontology generation calls the LLM; on success the URL
  // is rewritten to /process/proj_<id> and Zep graph build starts. The header
  // `.status-indicator` text is hard-coded English in every locale:
  // Generating Ontology -> Building Graph -> Ready, or Error.
  const timeout = Number(process.env.START_TIMEOUT_MS || 180000);
  const untilReady = process.env.UNTIL === 'ready';
  const done = untilReady ? /^(Ready|Error)$/ : /^(Building Graph|Ready|Error)$/;
  await page.waitForFunction(
    (src) => new RegExp(src).test(document.querySelector('.status-indicator')?.innerText.trim() || ''),
    done.source, { timeout, polling: 500 },
  ).catch(() => console.log(`timed out after ${timeout} ms`));
  await page.waitForTimeout(1000);
  console.log(`status: ${(await page.innerText('.status-indicator')).trim()}  url: ${page.url()}`);
  const logs = await page.$$eval('.log-line', (els) => els.map((e) => e.innerText.replace(/\s+/g, ' ')));
  console.log('system dashboard (last 6):\n  ' + logs.slice(-6).join('\n  '));
  await shot(page, 'start-result');
  console.log(`console errors: ${JSON.stringify(errors.slice(0, 5))}`);
  await browser.close();
}

async function shotRoute(route = '/', name = 'route') {
  const { browser, page, errors } = await openPage();
  await page.goto(BASE_URL + route, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);
  await shot(page, name);
  console.log(`console errors: ${JSON.stringify(errors.slice(0, 5))}`);
  await browser.close();
}

const [cmd, ...args] = process.argv.slice(2);
const cmds = { smoke, start: () => start(...args), shot: () => shotRoute(...args) };
if (!cmds[cmd]) {
  console.error('commands: smoke | start <seed-file> "<prompt>" | shot <route> [name]');
  process.exit(2);
}
cmds[cmd]().catch((e) => { console.error(e); process.exit(1); });
