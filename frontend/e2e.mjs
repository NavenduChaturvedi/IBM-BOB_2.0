// End-to-end check of the dashboard in headless Chrome, replaying the QA-notes bugs.
//
//   node frontend/e2e.mjs            # deterministic checks (no Bob calls)
//   node frontend/e2e.mjs --with-bob # also one live Bob run (needs BOB_API_KEY for the backend)
//
// Starts the backend (backend/demo_repo), puts a proxy in front that delays /api/meta
// to simulate a cold start and counts requests, then opens frontend/index.html from
// file:// with ?api= pointing at the proxy — the same cross-origin setup as the
// Vercel frontend talking to Render. Uses only Node built-ins and the Chrome DevTools
// Protocol. Set CHROME to override the browser path.
import { spawn } from "node:child_process";
import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const WITH_BOB = process.argv.includes("--with-bob");
const BACKEND_PORT = 8811, PROXY_PORT = 8812, CDP_PORT = 9333, COLD_START_MS = 6000;
const CHROME = process.env.CHROME || [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome", "/usr/bin/chromium",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].find(p => fs.existsSync(p));
const PYTHON = [path.join(ROOT, ".venv/Scripts/python.exe"), path.join(ROOT, ".venv/bin/python")].find(p => fs.existsSync(p)) || "python";

const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${!ok && detail ? `\n      ${detail}` : ""}`);
}

// ── backend + cold-start proxy ───────────────────────────────────────────────
const procs = [];
function startBackend() {
  const repo = path.join(ROOT, "backend/demo_repo");
  if (!fs.existsSync(path.join(repo, ".git"))) {
    const seed = spawn(PYTHON, [path.join(ROOT, "backend/demo/seed_demo.py"), "--force"], { stdio: "ignore" });
    procs.push(seed);
  }
  const env = { ...process.env, PORT: String(BACKEND_PORT) };
  if (!WITH_BOB) env.BLASTRADIUS_NO_DOTENV = "1", delete env.BOB_API_KEY;
  const p = spawn(PYTHON, [path.join(ROOT, "backend/main.py"), "--repo", repo, "--serve", "--no-browser"],
                  { env, stdio: "ignore" });
  procs.push(p);
}

const requests = [];
let metaDelay = COLD_START_MS;
const proxy = http.createServer((req, res) => {
  requests.push(req.url);
  const forward = () => {
    const up = http.request({ host: "127.0.0.1", port: BACKEND_PORT, path: req.url, method: req.method, headers: req.headers }, upRes => {
      res.writeHead(upRes.statusCode, upRes.headers);
      upRes.pipe(res);
    });
    up.on("error", () => { res.writeHead(502); res.end(); });
    req.pipe(up);
  };
  if (req.url.startsWith("/api/meta") && metaDelay) { const d = metaDelay; metaDelay = 0; setTimeout(forward, d); }
  else forward();
});

async function waitFor(fn, timeout = 30000, step = 200) {
  const end = Date.now() + timeout;
  for (;;) {
    try { const v = await fn(); if (v) return v; } catch {}
    if (Date.now() > end) return null;
    await sleep(step);
  }
}

// ── minimal CDP client ───────────────────────────────────────────────────────
let ws, seq = 0;
const pending = new Map(), exceptions = [];
function cdp(method, params = {}) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}
async function js(expr) {
  const r = await cdp("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "eval failed");
  return r.result.value;
}
const text = sel => js(`document.querySelector(${JSON.stringify(sel)})?.innerText ?? ""`);
const visible = sel => js(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); return !!e && e.offsetParent !== null && !e.classList.contains("hidden"); })()`);
const click = sel => js(`document.querySelector(${JSON.stringify(sel)}).click()`);
const nav = page => click(`#nav a[data-page="${page}"]`);
const select = (sel, value) => js(`(() => { const s = document.querySelector(${JSON.stringify(sel)}); s.value = ${JSON.stringify(value)}; s.dispatchEvent(new Event("change")); })()`);
async function shot(name) {
  if (!process.env.SHOTS) return;
  const { data } = await cdp("Page.captureScreenshot", { format: "png" });
  fs.mkdirSync(process.env.SHOTS, { recursive: true });
  fs.writeFileSync(path.join(process.env.SHOTS, name + ".png"), Buffer.from(data, "base64"));
}
async function open(url) {
  await cdp("Page.navigate", { url });
  await waitFor(() => js("document.readyState === 'complete'"), 15000);
}

async function main() {
  if (!CHROME) throw new Error("No Chrome/Edge found; set CHROME=/path/to/chrome");
  startBackend();
  proxy.listen(PROXY_PORT);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "br-e2e-"));
  procs.push(spawn(CHROME, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${CDP_PORT}`,
                            `--user-data-dir=${profile}`, "--window-size=1440,1000", "about:blank"], { stdio: "ignore" }));
  const target = await waitFor(async () => {
    const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
    return list.find(t => t.type === "page");
  }, 20000);
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener("open", r));
  ws.addEventListener("message", e => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)[m.error ? "reject" : "resolve"](m.error || m.result); pending.delete(m.id); }
    if (m.method === "Runtime.exceptionThrown") exceptions.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  });
  await cdp("Runtime.enable"); await cdp("Page.enable");
  await waitFor(async () => (await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/meta`)).ok, 30000);

  const page = pathToFileURL(process.env.INDEX || path.join(ROOT, "frontend/index.html")).href;
  const api = `http://127.0.0.1:${PROXY_PORT}`;

  // P0-1/P0-3: cold start — controls wait, a countdown shows, a scenario clicked early is queued
  await open(`${page}?api=${encodeURIComponent(api)}`);
  await sleep(2600);
  await shot("1-cold-start");
  check("cold start: wake banner with countdown", /Waking the server… \d+s/.test(await text("#banner")), await text("#banner"));
  check("cold start: Analyze disabled and labelled", await js(`document.querySelector("#btn-analyze").disabled`) && /Connecting/.test(await text("#btn-analyze")));
  await nav("pr1");
  check("cold start: early scenario click shows a waiting state (not a silent no-op)", /Waking up the server/.test(await text("#scenario-pr1")));
  const pr1Done = await waitFor(async () => (await text("#scenario-pr1")).includes("Breaking calls"), 30000);
  check("cold start: queued scenario runs once the server answers", !!pr1Done);
  check("no duplicate request for PR1", requests.filter(u => u.includes("head=pr1%2Fpagination-fix")).length === 1,
        requests.filter(u => u.includes("analyze")).join(", "));
  check("wake banner cleared after connecting", !(await visible("#banner")));

  // P0 stale-data bug: sidebar pages follow the scenario you just ran
  await nav("pr2");
  await waitFor(async () => (await text("#scenario-pr2")).includes("REFUND_WEBHOOK_URL"), 20000);
  await nav("checklist");
  await shot("2-checklist-pr2");
  check("Checklist shows PR2 after running PR2 (not the previous run)",
        (await text("#checklist-sub")).includes("pr2/refund-status") && (await text("#rows-area")).includes("REFUND_WEBHOOK_URL"),
        await text("#checklist-sub"));
  await nav("runbook");
  check("Runbook shows PR2's migration downgrade", (await text("#rb-body")).includes("alembic downgrade 4f1a2b3c5d6e"));
  check("branch selectors follow the scenario", (await js(`document.querySelector("#sel-head").value`)) === "pr2/refund-status");
  await nav("pr1");
  await nav("blast");
  check("revisiting a cached scenario re-syncs the other pages", (await text("#blast-sub")).includes("pr1/pagination-fix"), await text("#blast-sub"));
  check("revisit didn't refetch", requests.filter(u => u.includes("head=pr1%2Fpagination-fix")).length === 1);

  // P0-5: errors persist inline (no 2.2s toast), and say which result is on screen
  await nav("overview");
  await select("#sel-base", "main"); await select("#sel-head", "main");
  await click("#btn-analyze");
  await sleep(3500);
  check("same-branch error is inline and still visible after 3.5s", /Pick two different branches/.test(await text("#banner")));
  await select("#sel-base", "pr3/discount-tier"); await select("#sel-head", "main");
  await click("#btn-analyze");
  await waitFor(async () => /swapped/.test(await text("#banner")), 15000);
  await shot("3-swapped-error");
  const b = await text("#banner");
  check("reversed range: server explains the swap (no empty 'successful' run)", /swapped/.test(b), b);
  check("error says the results on screen are from the earlier run", /still from the earlier run/.test(b), b);

  check("Rejection demo button hidden for a branch without a demo draft",
        !(await visible('#modes [data-mode="sample"]')));
  // rejection demo + copy button that used to throw
  await nav("rejection");
  await waitFor(async () => (await text("#scenario-rejection")).includes("draft was rejected"), 20000);
  check("rejection demo shows the validator's reasons", /deployment `?checkout`? not found/i.test(await text("#scenario-rejection")));
  await js(`document.querySelector("#scenario-rejection [data-copy-md]").click()`);
  await sleep(300);

  // P1: Back button restores page, branches, and result
  await nav("pr3");
  await waitFor(async () => (await text("#scenario-pr3")).includes("Breaking calls"), 20000);
  await nav("checklist");
  await js("history.back()");
  await sleep(600);
  check("Back returns to the previous page", await visible("#page-pr3"));
  await js("history.back()");
  await sleep(600);
  check("Back again restores the rejection run", await visible("#page-rejection") && (await js(`document.querySelector('#modes .on').dataset.mode`)) === "sample");

  // P1: layout at ~894px keeps Analyze on screen with no horizontal scroll
  await cdp("Emulation.setDeviceMetricsOverride", { width: 894, height: 900, deviceScaleFactor: 1, mobile: false });
  await sleep(300);
  const layout = await js(`(() => { const r = document.querySelector("#btn-analyze").getBoundingClientRect();
    return { right: r.right, scroll: document.documentElement.scrollWidth, width: innerWidth }; })()`);
  check("894px: Analyze fully visible, no horizontal scroll", layout.right <= layout.width && layout.scroll <= layout.width, JSON.stringify(layout));
  await cdp("Emulation.clearDeviceMetricsOverride");

  // deep link to a branch that doesn't exist
  await open(`${page}?api=${encodeURIComponent(api)}&base=main&head=nope`);
  await waitFor(async () => /doesn't exist/.test(await text("#banner")), 15000);
  check("bad deep link: explains the unknown branch and falls back to defaults",
        /nope/.test(await text("#banner")) && (await js(`document.querySelector("#sel-head").value`)) !== "");

  if (WITH_BOB) {
    await select("#sel-base", "main"); await select("#sel-head", "pr3/discount-tier");
    await click('#modes [data-mode="bob"]'); await click("#btn-analyze");
    const partial = await waitFor(async () => /Bob is improving/.test(await text("#banner")) && (await text("#sc-broken")) === "2", 20000);
    await shot("4-bob-pending");
    check("live Bob: facts render before Bob finishes", !!partial);
    const done = await waitFor(async () => !/Bob is improving|taking longer/.test(await text("#banner")) && !/improving this runbook/.test(await text("#rb-status-pill")), 120000);
    check("live Bob: runbook resolves (validated or rejected with reasons)", !!done, await text("#rb-status-pill"));
  }

  // unreachable server: user-facing error with Retry, never the dev instructions
  await open(`${page}?api=${encodeURIComponent("http://127.0.0.1:9")}`);
  await waitFor(() => visible("#page-error"), 20000);
  await shot("5-unreachable");
  check("unreachable server: dedicated error page with Retry", /Can't reach the analysis server/.test(await text("#page-error")) && await visible("#err-retry"));
  check("dev-only instructions never shown", !(await js("document.body.innerText")).includes("blastradius.py --serve"));

  check("hidden elements are hidden (empty badges, error detail)",
        !(await visible("#err-detail")) && !(await visible("#nav-badge-overview")));
  check("no uncaught JavaScript errors", exceptions.length === 0, exceptions.join(" | "));
}

main()
  .catch(e => { console.error(e); results.push({ name: "harness", ok: false }); })
  .finally(() => {
    proxy.close();
    procs.forEach(p => { try { p.kill(); } catch {} });
    const failed = results.filter(r => !r.ok).length;
    console.log(`\n${results.length - failed}/${results.length} checks passed`);
    process.exit(failed ? 1 : 0);
  });
