const { spawn } = require('child_process');
const os = require('os');
const path = require('path');
const readline = require('readline');
const { chromium, devices } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const SHOT_DIR = process.env.SHOT_DIR || os.tmpdir();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function check(name, condition, detail) {
  if (!condition) throw new Error('FAIL: ' + name + (detail ? ' (' + detail + ')' : ''));
  console.log('PASS: ' + name);
}

async function until(fn, what, timeout = 4000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await fn()) return;
    await sleep(25);
  }
  throw new Error('FAIL: timed out waiting for ' + what);
}

function watch(page, problems) {
  page.on('console', (msg) => {
    if (msg.type() === 'error') problems.push('console error: ' + msg.text());
  });
  page.on('pageerror', (err) => problems.push('page error: ' + err.message));
  page.on('requestfailed', (req) => {
    problems.push('request failed: ' + req.url() + ' ' + (req.failure() && req.failure().errorText));
  });
  page.on('response', (res) => {
    if (res.status() >= 400) problems.push('HTTP ' + res.status() + ': ' + res.url());
  });
}

// Records the page's WebSockets: their URL, whether each is closed, and every data frame received.
function record(page) {
  const sockets = [];
  page.on('websocket', (ws) => {
    const sock = { url: ws.url(), closed: false, frames: [] };
    sockets.push(sock);
    ws.on('framereceived', (f) => sock.frames.push(f.payload));
    ws.on('close', () => { sock.closed = true; });
  });
  const isStatus = (s) => /\/ws\/status$/.test(s.url);
  const isInput = (s) => /\/ws$/.test(s.url);
  return {
    sockets,
    status: () => sockets.filter(isStatus),
    input: () => sockets.filter(isInput),
    openStatus: () => sockets.filter((s) => isStatus(s) && !s.closed),
    openInput: () => sockets.filter((s) => isInput(s) && !s.closed),
  };
}

// The launcher prints one line for "PORT <n>" at start and one per "state" command.
function launcher() {
  const proc = spawn('python3', ['tests/ui_status_server.py'], {
    cwd: ROOT,
    stdio: ['pipe', 'pipe', 'inherit'],
  });
  const lines = [];
  const waiters = [];
  readline.createInterface({ input: proc.stdout }).on('line', (line) => {
    const waiter = waiters.shift();
    if (waiter) waiter(line);
    else lines.push(line);
  });
  const next = () => new Promise((resolve, reject) => {
    if (lines.length) {
      resolve(lines.shift());
      return;
    }
    const timer = setTimeout(() => reject(new Error('launcher did not answer')), 5000);
    waiters.push((line) => {
      clearTimeout(timer);
      resolve(line);
    });
  });
  return {
    proc,
    send: (command) => proc.stdin.write(command + '\n'),
    port: async () => {
      const line = await next();
      const m = /^PORT (\d+)$/.exec(line);
      if (!m) throw new Error('unexpected launcher output: ' + line);
      return Number(m[1]);
    },
    state: async () => {
      proc.stdin.write('state\n');
      const m = /^WATCHER (\d) SUBS (\d+)$/.exec(await next());
      if (!m) throw new Error('unexpected launcher state output');
      return { watcher: Number(m[1]), subs: Number(m[2]) };
    },
  };
}

const tile = (id) => '.tile[data-id="' + id + '"]';
const dotClass = (page, id) => page.evaluate((sel) => {
  const dot = document.querySelector(sel + ' .dot');
  return dot ? dot.className : null;
}, tile(id));
const dotCount = (page) => page.$$eval('.tile .dot', (list) => list.length);
const ariaLabel = (page, id) => page.$eval(tile(id), (el) => el.getAttribute('aria-label'));
const runningIds = (page) => page.$$eval('.tile .dot.run', (list) =>
  list.map((el) => el.closest('.tile').dataset.id).sort());

async function run(base, ctl) {
  const browser = await chromium.launch();
  try {
    const problems = [];
    const context = await browser.newContext({ ...devices['iPhone 13'] });
    const page = await context.newPage();
    watch(page, problems);
    const rec = record(page);

    // ---- 1. connect on the Deck tab, full state first
    await page.goto(base + '/');
    await page.waitForSelector('.tile[data-id]');
    await until(() => rec.status().length > 0 && rec.status()[0].frames.length > 0, 'the first status frame');
    const first = JSON.parse(rec.status()[0].frames[0]);
    check('the Deck tab opens a websocket ending /ws/status',
      rec.status().length === 1 && /^ws:\/\/[^/]+\/ws\/status$/.test(rec.status()[0].url),
      JSON.stringify(rec.sockets.map((s) => s.url)));
    check('the first frame is the full state of every watched button',
      first.t === 'status' && JSON.stringify(Object.keys(first.running).sort()) ===
      JSON.stringify(['discord', 'simhub', 'steam']) && Object.values(first.running).every((v) => v === false),
      JSON.stringify(first));
    check('no tile has a dot at first', (await dotCount(page)) === 0);
    check('every filled tile carries its button id',
      JSON.stringify(await page.$$eval('#grid button.tile', (l) => l.map((el) => el.dataset.id))) ===
      JSON.stringify(['steam', 'discord', 'simhub', 'simmotion']));

    // ---- 2. a process starts
    ctl.send('start steam.exe');
    await until(async () => (await dotClass(page, 'steam')) === 'dot run', 'the Steam tile to get a green dot', 2000);
    check('start steam.exe turns the Steam dot green within 2 s', true);
    check('the Steam aria-label ends with ", running"', (await ariaLabel(page, 'steam')) === 'Steam, running',
      await ariaLabel(page, 'steam'));
    check('the other tiles have no dots and keep their plain labels',
      (await dotCount(page)) === 1 && (await ariaLabel(page, 'discord')) === 'Discord' &&
      (await ariaLabel(page, 'simhub')) === 'SimHub');
    const dot = await page.$eval(tile('steam') + ' .dot', (el) => {
      const r = el.getBoundingClientRect();
      const t = el.closest('.tile').getBoundingClientRect();
      const cs = getComputedStyle(el);
      return { w: r.width, h: r.height, top: r.top - t.top, right: t.right - r.right,
        color: cs.backgroundColor, hidden: el.getAttribute('aria-hidden') };
    });
    check('the dot is a 10 px green circle inset about 10 px from the top-right corner of the tile',
      dot.w === 10 && dot.h === 10 && Math.abs(dot.top - 11) <= 1.5 && Math.abs(dot.right - 11) <= 1.5 &&
      dot.color === 'rgb(46, 204, 113)' && dot.hidden === 'true', JSON.stringify(dot));

    // ---- 3. the process quits
    ctl.send('stop steam.exe');
    await until(async () => (await dotClass(page, 'steam')) === null, 'the Steam dot to disappear', 2000);
    check('stop steam.exe removes the Steam dot within 2 s and restores the label',
      (await dotCount(page)) === 0 && (await ariaLabel(page, 'steam')) === 'Steam');

    // ---- 4. tap a tile: amber until the process shows up
    ctl.send('start steam.exe');
    await until(async () => (await dotClass(page, 'steam')) === 'dot run', 'the Steam dot to be green again', 2000);
    await page.click(tile('simhub'));
    await until(async () => (await dotClass(page, 'simhub')) === 'dot starting', 'the amber starting dot', 500);
    check('tapping SimHub shows the amber starting dot within 500 ms', true);
    const amber = await page.$eval(tile('simhub') + ' .dot', (el) => getComputedStyle(el).backgroundColor);
    check('the starting dot is amber', amber === 'rgb(245, 166, 35)', amber);
    check('a starting tile keeps its plain aria-label', (await ariaLabel(page, 'simhub')) === 'SimHub');
    await sleep(300);
    await page.screenshot({ path: path.join(SHOT_DIR, 'deck-dots.png') });
    ctl.send('start simhubwpf.exe');
    await until(async () => (await dotClass(page, 'simhub')) === 'dot run', 'the SimHub dot to turn green', 2000);
    check('start simhubwpf.exe turns the SimHub dot green within 2 s and the amber is gone',
      (await page.$$eval(tile('simhub') + ' .dot', (l) => l.length)) === 1 &&
      (await ariaLabel(page, 'simhub')) === 'SimHub, running');

    // ---- 5. a tap that never leads to a running process
    await page.click(tile('discord'));
    await until(async () => (await dotClass(page, 'discord')) === 'dot starting', 'the Discord amber dot', 500);
    check('tapping Discord shows the amber starting dot', true);
    await sleep(8000);
    check('the amber dot is still there after 8 s', (await dotClass(page, 'discord')) === 'dot starting');
    await until(async () => (await dotClass(page, 'discord')) === null, 'the Discord amber dot to time out', 3000);
    check('the amber dot disappears after about 10 s without ever turning green',
      (await ariaLabel(page, 'discord')) === 'Discord' && (await dotCount(page)) === 2);

    // ---- 6. idle: nothing is sent
    const idleSock = rec.openStatus()[0];
    const mark = idleSock.frames.length;
    await sleep(2000);
    check('no data frames arrive on the status socket over 2 idle seconds',
      idleSock.frames.length === mark, String(idleSock.frames.length - mark));

    // ---- 7. leave and come back
    await page.click('#tab-pad');
    await until(() => rec.openStatus().length === 0, 'the status socket to close');
    await until(() => rec.openInput().length === 1, 'the input socket to open');
    check('switching to Trackpad closes /ws/status and opens /ws',
      rec.openStatus().length === 0 && rec.openInput().length === 1);
    check('no tile keeps a dot while the status socket is closed', (await dotCount(page)) === 0);
    let state = null;
    await until(async () => (state = await ctl.state()).watcher === 0, 'the watcher thread to stop', 2000);
    check('the server watcher stops with no subscribers', state.watcher === 0 && state.subs === 0,
      JSON.stringify(state));
    ctl.send('start discord.exe');
    await sleep(500);

    await page.click('#tab-deck');
    await until(() => rec.openInput().length === 0 && rec.openStatus().length === 1, 'the sockets to swap back');
    check('switching back to Deck closes /ws and reopens /ws/status',
      rec.openInput().length === 0 && rec.openStatus().length === 1 && rec.status().length === 2);
    await until(async () => JSON.stringify(await runningIds(page)) ===
      JSON.stringify(['discord', 'simhub', 'steam']), 'the dots to reflect the current process list', 1000);
    check('dots reflect the current process list immediately after returning (including a start made on Trackpad)',
      (await dotCount(page)) === 3);
    await until(async () => (state = await ctl.state()).watcher === 1, 'the watcher thread to run again', 2000);
    check('the server watcher runs again with one subscriber', state.watcher === 1 && state.subs === 1,
      JSON.stringify(state));

    // ---- 8. the two sockets are never open together
    await page.click('#tab-pad');
    await until(() => rec.openInput().length === 1 && rec.openStatus().length === 0, 'Trackpad sockets');
    await sleep(300);
    check('only /ws is open on Trackpad', rec.openInput().length === 1 && rec.openStatus().length === 0);
    await page.click('#tab-deck');
    await until(() => rec.openInput().length === 0 && rec.openStatus().length === 1, 'Deck sockets');
    await sleep(300);
    check('only /ws/status is open on Deck', rec.openInput().length === 0 && rec.openStatus().length === 1);

    // ---- 9. clean console
    check('no console errors, page errors or failed requests', problems.length === 0, problems.join('; '));
  } finally {
    await browser.close();
  }
}

async function main() {
  const ctl = launcher();
  try {
    const port = await ctl.port();
    await run('http://127.0.0.1:' + port, ctl);
  } finally {
    try { ctl.send('quit'); } catch (e) { /* already gone */ }
    ctl.proc.kill();
  }
}

main().then(
  () => process.exit(0),
  (err) => {
    console.error(err && err.message ? err.message : err);
    process.exit(1);
  }
);
