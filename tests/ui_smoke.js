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

async function layout(page) {
  return page.evaluate(() => {
    const rects = [...document.querySelectorAll('#grid > *')].map((el) => {
      const r = el.getBoundingClientRect();
      return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
    });
    return {
      rects,
      columns: new Set(rects.map((r) => r.x)).size,
      rows: new Set(rects.map((r) => r.y)).size,
      scrollW: document.documentElement.scrollWidth,
      scrollH: document.documentElement.scrollHeight,
      innerW: window.innerWidth,
      innerH: window.innerHeight,
    };
  });
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

    const kinds = await page.$$eval('#grid > *', (els) => els.map((el) => ({
      tag: el.tagName, tile: el.classList.contains('tile'), empty: el.classList.contains('empty'),
      label: el.querySelector('.label') ? el.querySelector('.label').textContent : null,
      text: el.textContent, imgs: el.querySelectorAll('img').length,
      ariaHidden: el.getAttribute('aria-hidden'),
    })));
    check('grid has exactly 8 children', kinds.length === 8, String(kinds.length));
    const labels = kinds.slice(0, 4).map((k) => k.label);
    check('first 4 slots are .tile buttons in order Steam, Discord, SimHub, SimMotion',
      kinds.slice(0, 4).every((k) => k.tag === 'BUTTON' && k.tile && !k.empty) &&
      JSON.stringify(labels) === JSON.stringify(['Steam', 'Discord', 'SimHub', 'SimMotion']),
      JSON.stringify(kinds.slice(0, 4)));
    check('last 4 slots are .tile.empty, not buttons, with no label, icon or text',
      kinds.slice(4).every((k) => k.tag === 'DIV' && k.tile && k.empty && k.label === null &&
        k.imgs === 0 && k.text === '' && k.ariaHidden === 'true'),
      JSON.stringify(kinds.slice(4)));
    check('only the 4 filled slots are buttons', (await page.locator('#grid button').count()) === 4);

    await page.waitForFunction(() =>
      [...document.querySelectorAll('.tile img')].every((i) => i.complete));
    const imgs = await page.$$eval('.tile img', (list) =>
      list.map((i) => ({ complete: i.complete, w: i.naturalWidth })));
    check('icons loaded for the 4 filled tiles',
      imgs.length === 4 && imgs.every((i) => i.complete && i.w > 0), JSON.stringify(imgs));

    const pLayout = await layout(page);
    check('portrait grid is 2 columns x 4 rows',
      pLayout.columns === 2 && pLayout.rows === 4,
      pLayout.columns + ' x ' + pLayout.rows);
    check('portrait tiles are laid out in slot order (row by row)',
      pLayout.rects.every((r, i) => i === 0 || r.y > pLayout.rects[i - 1].y ||
        (r.y === pLayout.rects[i - 1].y && r.x > pLayout.rects[i - 1].x)),
      JSON.stringify(pLayout.rects));
    check('portrait has no horizontal overflow', pLayout.scrollW <= pLayout.innerW,
      pLayout.scrollW + ' > ' + pLayout.innerW);
    check('portrait does not scroll vertically', pLayout.scrollH <= pLayout.innerH,
      pLayout.scrollH + ' > ' + pLayout.innerH);
    const pIcon = await page.$eval('.tile img', (img) => {
      const t = img.closest('.tile').getBoundingClientRect();
      const r = img.getBoundingClientRect();
      return { iw: r.width, ih: r.height, tw: t.width, th: t.height };
    });
    check('portrait icon is at most 50% of the tile height and fits inside the tile',
      pIcon.ih <= pIcon.th * 0.5 + 1 && pIcon.iw <= pIcon.tw, JSON.stringify(pIcon));
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
    check('landscape grid has 8 children',
      (await lpage.locator('#grid > *').count()) === 8);
    const lLayout = await layout(lpage);
    check('landscape grid is 4 columns x 2 rows',
      lLayout.columns === 4 && lLayout.rows === 2, lLayout.columns + ' x ' + lLayout.rows);
    check('landscape has no horizontal overflow', lLayout.scrollW <= lLayout.innerW,
      lLayout.scrollW + ' > ' + lLayout.innerW);
    check('landscape does not scroll vertically', lLayout.scrollH <= lLayout.innerH,
      lLayout.scrollH + ' > ' + lLayout.innerH);
    const lIcon = await lpage.$eval('.tile img', (img) => {
      const tile = img.closest('.tile');
      const t = tile.getBoundingClientRect();
      const r = img.getBoundingClientRect();
      const label = tile.querySelector('.label').getBoundingClientRect();
      return { iw: r.width, ih: r.height, tw: t.width, th: t.height,
        iconTop: r.top, labelBottom: label.bottom, tileTop: t.top, tileBottom: t.bottom };
    });
    check('landscape icon is at most 50% of the tile height and icon + label fit in the tile',
      lIcon.ih <= lIcon.th * 0.5 + 1 && lIcon.iconTop >= lIcon.tileTop &&
      lIcon.labelBottom <= lIcon.tileBottom, JSON.stringify(lIcon));
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
