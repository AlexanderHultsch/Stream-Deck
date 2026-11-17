const { spawn } = require('child_process');
const net = require('net');
const os = require('os');
const path = require('path');
const { chromium, devices } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const SHOT_DIR = process.env.SHOT_DIR || os.tmpdir();

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForServer(base, proc) {
  for (let i = 0; i < 100; i++) {
    if (proc.exitCode !== null) throw new Error('server exited early with code ' + proc.exitCode);
    try {
      const res = await fetch(base + '/api/buttons');
      if (res.ok) return;
    } catch (e) { /* not up yet */ }
    await sleep(100);
  }
  throw new Error('server did not answer on ' + base);
}

function check(name, condition, detail) {
  if (!condition) throw new Error('FAIL: ' + name + (detail ? ' (' + detail + ')' : ''));
  console.log('PASS: ' + name);
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

async function noHorizontalOverflow(page) {
  return page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }));
}

async function run(base) {
  const browser = await chromium.launch();
  try {
    const problems = [];

    const portrait = await browser.newContext({ ...devices['iPhone 13'] });
    const page = await portrait.newPage();
    watch(page, problems);
    await page.goto(base + '/');
    await page.waitForSelector('.tile');

    const labels = await page.$$eval('.tile', (tiles) =>
      tiles.map((t) => t.querySelector('.label').textContent));
    check('4 tiles in order Steam, Discord, SimHub, SimMotion',
      JSON.stringify(labels) === JSON.stringify(['Steam', 'Discord', 'SimHub', 'SimMotion']),
      JSON.stringify(labels));

    await page.waitForFunction(() =>
      [...document.querySelectorAll('.tile img')].every((i) => i.complete));
    const imgs = await page.$$eval('.tile img', (list) =>
      list.map((i) => ({ complete: i.complete, w: i.naturalWidth })));
    check('every tile icon loaded',
      imgs.length === 4 && imgs.every((i) => i.complete && i.w > 0), JSON.stringify(imgs));

    const xs = await page.$$eval('.tile', (tiles) =>
      tiles.map((t) => Math.round(t.getBoundingClientRect().x)));
    const columns = new Set(xs).size;
    check('portrait grid has 2 columns', columns === 2, 'x positions ' + xs.join(','));
    const pOver = await noHorizontalOverflow(page);
    check('portrait has no horizontal overflow', pOver.scroll <= pOver.inner,
      pOver.scroll + ' > ' + pOver.inner);
    await page.screenshot({ path: path.join(SHOT_DIR, 'portrait.png') });

    await page.click('.tile:nth-child(1)');
    await page.waitForFunction(() => document.getElementById('toast').textContent === 'Launched Steam');
    let state = await page.$eval('#toast', (t) => ({ text: t.textContent, cls: [...t.classList] }));
    check('tap Steam shows ok toast "Launched Steam"',
      state.cls.includes('show') && state.cls.includes('ok') && !state.cls.includes('err'),
      JSON.stringify(state));

    await page.click('.tile:nth-child(4)');
    await page.waitForFunction(() =>
      document.getElementById('toast').textContent === 'SimMotion is not configured');
    state = await page.$eval('#toast', (t) => ({ text: t.textContent, cls: [...t.classList] }));
    check('tap SimMotion shows err toast "SimMotion is not configured"',
      state.cls.includes('show') && state.cls.includes('err') && !state.cls.includes('ok'),
      JSON.stringify(state));

    await page.waitForFunction(() =>
      !document.getElementById('toast').classList.contains('show'), null, { timeout: 3000 });
    check('toast hides again within 3 s', true);

    const landscape = await browser.newContext({ ...devices['iPhone 13 landscape'] });
    const lpage = await landscape.newPage();
    watch(lpage, problems);
    await lpage.goto(base + '/');
    await lpage.waitForSelector('.tile');
    await lpage.waitForFunction(() =>
      [...document.querySelectorAll('.tile img')].every((i) => i.complete));
    const lOver = await noHorizontalOverflow(lpage);
    check('landscape has no horizontal overflow', lOver.scroll <= lOver.inner,
      lOver.scroll + ' > ' + lOver.inner);
    await lpage.screenshot({ path: path.join(SHOT_DIR, 'landscape.png') });

    const hrefs = await page.evaluate(() => ({
      manifest: document.querySelector('link[rel=manifest]').getAttribute('href'),
      touch: document.querySelector('link[rel=apple-touch-icon]').getAttribute('href'),
    }));
    const manifestRes = await fetch(new URL(hrefs.manifest, base));
    const touchRes = await fetch(new URL(hrefs.touch, base));
    const manifestType = manifestRes.headers.get('content-type') || '';
    const touchType = touchRes.headers.get('content-type') || '';
    const manifest = await manifestRes.json();
    check('manifest and apple-touch-icon links are served',
      manifestRes.status === 200 && manifestType.startsWith('application/manifest+json') &&
      touchRes.status === 200 && touchType === 'image/png',
      manifestRes.status + ' ' + manifestType + ', ' + touchRes.status + ' ' + touchType);
    const icons = manifest.icons || [];
    let iconsOk = icons.length === 2;
    for (const icon of icons) {
      const res = await fetch(new URL(icon.src, base));
      iconsOk = iconsOk && res.status === 200 && res.headers.get('content-type') === 'image/png';
    }
    check('manifest lists 2 icons that are served', iconsOk, JSON.stringify(icons));

    check('no failed requests or console errors', problems.length === 0, problems.join('; '));
  } finally {
    await browser.close();
  }
}

async function main() {
  const port = await freePort();
  const base = 'http://127.0.0.1:' + port;
  const proc = spawn('python3', ['server.py', '--port', String(port)], {
    cwd: ROOT,
    stdio: ['ignore', 'ignore', 'inherit'],
  });
  try {
    await waitForServer(base, proc);
    await run(base);
  } finally {
    proc.kill();
  }
}

main().then(
  () => process.exit(0),
  (err) => {
    console.error(err && err.message ? err.message : err);
    process.exit(1);
  }
);
