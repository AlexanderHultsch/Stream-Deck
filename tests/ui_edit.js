const { spawn } = require('child_process');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const SHOT_DIR = process.env.SHOT_DIR || os.tmpdir();
const COPY_FILES = ['server.py', 'index.html', 'edit.html', 'app.js', 'edit.js', 'style.css',
  'manifest.webmanifest'];

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

async function apiButtons(base) {
  const res = await fetch(base + '/api/buttons');
  return (await res.json()).buttons;
}

const names = (list) => list.map((b) => b.name);

async function cardNames(page) {
  return page.$$eval('.card .f-name', (inputs) => inputs.map((i) => i.value));
}

async function statusState(page) {
  return page.$eval('#status', (s) => ({ text: s.textContent, cls: [...s.classList] }));
}

async function cardIndexByName(page, name) {
  const all = await cardNames(page);
  const index = all.indexOf(name);
  if (index < 0) throw new Error('no card named ' + name + ' in ' + JSON.stringify(all));
  return index;
}

async function save(page) {
  await page.click('#save');
  await page.waitForFunction(() => document.getElementById('status').textContent !== '');
}

async function run(base) {
  const browser = await chromium.launch();
  try {
    const problems = [];
    let expectedBadRequests = 0;

    const context = await browser.newContext({ viewport: { width: 1100, height: 800 } });
    const page = await context.newPage();
    page.on('dialog', (dialog) => dialog.accept());
    page.on('console', (msg) => {
      if (msg.type() !== 'error') return;
      if (/status of 400/.test(msg.text()) && expectedBadRequests > 0) return;
      problems.push('console error: ' + msg.text());
    });
    page.on('pageerror', (err) => problems.push('page error: ' + err.message));
    page.on('requestfailed', (req) => {
      problems.push('request failed: ' + req.url() + ' ' + (req.failure() && req.failure().errorText));
    });
    page.on('response', (res) => {
      if (res.status() >= 400 && !(res.status() === 400 && expectedBadRequests > 0)) {
        problems.push('HTTP ' + res.status() + ': ' + res.url());
      }
    });

    await page.goto(base + '/edit');
    await page.waitForSelector('.card');

    // 1
    let shown = await cardNames(page);
    check('4 cards shown with names Steam, Discord, SimHub, SimMotion',
      JSON.stringify(shown) === JSON.stringify(['Steam', 'Discord', 'SimHub', 'SimMotion']),
      JSON.stringify(shown));
    await page.waitForFunction(() =>
      [...document.querySelectorAll('.card img.icon')].every((i) => i.complete));
    const icons = await page.$$eval('.card img.icon', (list) =>
      list.map((i) => ({ w: i.naturalWidth, h: i.getBoundingClientRect().height })));
    check('every card shows a loaded 56px icon',
      icons.length === 4 && icons.every((i) => i.w > 0 && Math.round(i.h) === 56),
      JSON.stringify(icons));

    // 7 (dirty indicator hidden on a clean page, shown after typing)
    const dirty = page.locator('#dirty');
    check('"Unsaved changes" is hidden on a clean page', !(await dirty.isVisible()));

    // 2
    const steam = page.locator('.card').nth(0);
    await steam.locator('.f-name').fill('Steam X');
    check('"Unsaved changes" appears after typing', await dirty.isVisible());
    await steam.locator('.f-window').selectOption('minimized');
    await steam.locator('.f-ifrunning').selectOption('nothing');
    await save(page);
    let state = await statusState(page);
    check('save of edited Steam card shows status "Saved" in ok colour',
      state.text === 'Saved' && state.cls.includes('ok') && !state.cls.includes('err'),
      JSON.stringify(state));
    check('"Unsaved changes" disappears after a successful save', !(await dirty.isVisible()));
    let api = await apiButtons(base);
    check('API shows Steam X, minimized, nothing, id still "steam"',
      api[0].name === 'Steam X' && api[0].window === 'minimized' &&
      api[0].ifRunning === 'nothing' && api[0].id === 'steam', JSON.stringify(api[0]));
    await page.waitForFunction(() =>
      [...document.querySelectorAll('.card img.icon')].every((i) => i.complete));
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(SHOT_DIR, 'editor.png') });

    // 3
    await page.click('#add');
    const focusedIsName = await page.evaluate(() =>
      document.activeElement && document.activeElement.classList.contains('f-name'));
    check('Add button appends a card and focuses its name input',
      (await page.locator('.card').count()) === 5 && focusedIsName);
    const fresh = page.locator('.card').nth(4);
    const placeholder = await fresh.locator('.icon-ph').count();
    check('new unsaved card shows a placeholder instead of an icon',
      placeholder === 1 && (await fresh.locator('img').count()) === 0);
    await fresh.locator('.f-name').fill('Test App');
    await fresh.locator('.f-target').fill('C:\\Tools\\test.exe');
    await save(page);
    state = await statusState(page);
    api = await apiButtons(base);
    const added = api.find((b) => b.id === 'test-app');
    check('added button saved with id "test-app" and derived process "test.exe"',
      state.text === 'Saved' && added && added.name === 'Test App' &&
      added.process === 'test.exe' && added.target === 'C:\\Tools\\test.exe',
      JSON.stringify(state) + ' ' + JSON.stringify(added));
    await page.waitForFunction(() =>
      [...document.querySelectorAll('.card img.icon')].length === 5);
    check('re-render after save shows id "test-app" on the new card',
      (await page.locator('.card').nth(4).locator('.id').textContent()) === 'test-app');

    // 4
    const testIndex = await cardIndexByName(page, 'Test App');
    await page.locator('.card').nth(testIndex).locator('.btn-up').click();
    shown = await cardNames(page);
    check('Move up reorders the card in the editor',
      shown[testIndex - 1] === 'Test App', JSON.stringify(shown));
    await save(page);
    api = await apiButtons(base);
    check('API order reflects the moved "Test App" card',
      JSON.stringify(names(api)) === JSON.stringify(shown) &&
      names(api).indexOf('Test App') === testIndex - 1, JSON.stringify(names(api)));
    check('Move up is disabled on the first card and Move down on the last',
      (await page.locator('.card').first().locator('.btn-up').isDisabled()) &&
      (await page.locator('.card').last().locator('.btn-down').isDisabled()));

    // 5
    await page.locator('.card').nth(await cardIndexByName(page, 'Discord')).locator('.btn-delete').click();
    check('Delete removes the card immediately and nothing is persisted yet',
      !(await cardNames(page)).includes('Discord') &&
      names(await apiButtons(base)).includes('Discord'));
    await save(page);
    api = await apiButtons(base);
    check('API no longer has Discord and 4 buttons remain',
      !names(api).includes('Discord') && api.length === 4, JSON.stringify(names(api)));

    // 6
    const before = JSON.stringify(api);
    await page.click('#add');
    expectedBadRequests = 1;
    await save(page);
    state = await statusState(page);
    check('empty name shows the server error in the status area in error colour',
      state.text === 'name must not be empty' && state.cls.includes('err') &&
      !state.cls.includes('ok'), JSON.stringify(state));
    check('failed save leaves the API list unchanged',
      JSON.stringify(await apiButtons(base)) === before);
    check('"Unsaved changes" is still shown after a failed save', await dirty.isVisible());
    expectedBadRequests = 0;
    await page.locator('.card').last().locator('.btn-delete').click();
    check('removing the invalid card clears the unsaved indicator', !(await dirty.isVisible()));

    // 8
    await page.reload();
    await page.waitForSelector('.card');
    shown = await cardNames(page);
    check('reloading /edit shows the persisted state',
      JSON.stringify(shown) === JSON.stringify(names(api)), JSON.stringify(shown));
    const persisted = page.locator('.card').first();
    check('reloaded card keeps the persisted window and ifRunning values',
      (await persisted.locator('.f-window').inputValue()) === 'minimized' &&
      (await persisted.locator('.f-ifrunning').inputValue()) === 'nothing');

    const phone = await context.newPage();
    phone.on('pageerror', (err) => problems.push('page error: ' + err.message));
    await phone.goto(base + '/');
    await phone.waitForSelector('.tile');
    const labels = await phone.$$eval('.tile .label', (list) => list.map((l) => l.textContent));
    check('phone page shows a tile labelled "Steam X"', labels.includes('Steam X'),
      JSON.stringify(labels));
    await phone.close();

    // 9
    await page.setViewportSize({ width: 390, height: 800 });
    await page.waitForFunction(() => document.documentElement.clientWidth <= 390);
    const over = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      inner: window.innerWidth,
    }));
    check('no horizontal overflow at 390px width', over.scroll <= over.inner,
      over.scroll + ' > ' + over.inner);

    // 10
    check('no console errors, page errors or failed requests', problems.length === 0,
      problems.join('; '));
  } finally {
    await browser.close();
  }
}

async function main() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'streamdeck-edit-'));
  let proc = null;
  try {
    for (const file of COPY_FILES) fs.copyFileSync(path.join(ROOT, file), path.join(dir, file));
    const port = await freePort();
    const base = 'http://127.0.0.1:' + port;
    proc = spawn('python3', ['server.py', '--port', String(port)], {
      cwd: dir,
      stdio: ['ignore', 'ignore', 'inherit'],
    });
    await waitForServer(base, proc);
    await run(base);
  } finally {
    if (proc) proc.kill();
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

main().then(
  () => process.exit(0),
  (err) => {
    console.error(err && err.message ? err.message : err);
    process.exit(1);
  }
);
