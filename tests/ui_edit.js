const { spawn } = require('child_process');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const SHOT_DIR = process.env.SHOT_DIR || os.tmpdir();
const COPY_FILES = ['server.py', 'index.html', 'edit.html', 'app.js', 'edit.js', 'trackpad.js',
  'status.js', 'style.css', 'manifest.webmanifest'];

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

const slotIds = (list) => list.map((b) => (b ? b.id : null));
const slotNames = (list) => list.map((b) => (b ? b.name : null));

async function cardNames(page) {
  return page.$$eval('.card', (cards) => cards.map((card) => {
    const input = card.querySelector('.f-name');
    return input ? input.value : null;
  }));
}

async function statusState(page) {
  return page.$eval('#status', (s) => ({ text: s.textContent, cls: [...s.classList] }));
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
    check('8 slot cards: Steam, Discord, SimHub, SimMotion, then 4 empty',
      JSON.stringify(shown) === JSON.stringify(
        ['Steam', 'Discord', 'SimHub', 'SimMotion', null, null, null, null]),
      JSON.stringify(shown));
    check('4 filled cards and 4 empty cards',
      (await page.locator('.card:not(.slot-empty)').count()) === 4 &&
      (await page.locator('.card.slot-empty').count()) === 4);
    const titles = await page.$$eval('.card .slot-title', (list) => list.map((t) => t.textContent));
    check('cards are labelled Slot 1 ... Slot 8 in order',
      JSON.stringify(titles) === JSON.stringify(
        [1, 2, 3, 4, 5, 6, 7, 8].map((n) => 'Slot ' + n)), JSON.stringify(titles));
    check('empty cards say "Empty slot" and offer an Add button',
      (await page.locator('.card.slot-empty').first().textContent()).includes('Empty slot') &&
      (await page.locator('.card.slot-empty .btn-add').count()) === 4);
    check('the global Add button is gone', (await page.locator('#add').count()) === 0);
    await page.waitForFunction(() =>
      [...document.querySelectorAll('.card img.icon')].every((i) => i.complete));
    const icons = await page.$$eval('.card img.icon', (list) =>
      list.map((i) => ({ w: i.naturalWidth, h: i.getBoundingClientRect().height })));
    check('every filled card shows a loaded 56px icon',
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
    check('API still has 8 slots with 4 nulls after the save',
      api.length === 8 && api.slice(4).every((b) => b === null), JSON.stringify(api));
    await page.waitForFunction(() =>
      [...document.querySelectorAll('.card img.icon')].every((i) => i.complete));
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(SHOT_DIR, 'editor.png') });

    // 3
    const slot6 = page.locator('.card').nth(5);
    await slot6.locator('.btn-add').click();
    const focusedIsName = await page.evaluate(() =>
      document.activeElement && document.activeElement.classList.contains('f-name'));
    check('Add button on empty slot 6 fills it and focuses its name input',
      (await page.locator('.card').count()) === 8 && focusedIsName &&
      (await page.locator('.card').nth(5).locator('.f-name').count()) === 1);
    check('3 empty cards remain after filling slot 6',
      (await page.locator('.card.slot-empty').count()) === 3);
    const fresh = page.locator('.card').nth(5);
    check('new unsaved card shows a placeholder instead of an icon',
      (await fresh.locator('.icon-ph').count()) === 1 && (await fresh.locator('img').count()) === 0);
    check('new card starts blank with window normal and ifRunning focus',
      (await fresh.locator('.f-name').inputValue()) === '' &&
      (await fresh.locator('.f-target').inputValue()) === '' &&
      (await fresh.locator('.f-process').inputValue()) === '' &&
      (await fresh.locator('.f-window').inputValue()) === 'normal' &&
      (await fresh.locator('.f-ifrunning').inputValue()) === 'focus');
    check('"Unsaved changes" appears after adding a button', await dirty.isVisible());
    await fresh.locator('.f-name').fill('Test App');
    await fresh.locator('.f-target').fill('C:\\Tools\\test.exe');
    await save(page);
    state = await statusState(page);
    api = await apiButtons(base);
    const added = api[5];
    check('added button saved at index 5 with id "test-app" and derived process "test.exe"',
      state.text === 'Saved' && added && added.id === 'test-app' && added.name === 'Test App' &&
      added.process === 'test.exe' && added.target === 'C:\\Tools\\test.exe',
      JSON.stringify(state) + ' ' + JSON.stringify(added));
    check('slots 5, 7 and 8 are still null and the list has 8 entries',
      api.length === 8 && api[4] === null && api[6] === null && api[7] === null,
      JSON.stringify(api));
    await page.waitForFunction(() =>
      [...document.querySelectorAll('.card img.icon')].length === 5);
    check('re-render after save shows id "test-app" on slot 6',
      (await page.locator('.card').nth(5).locator('.id').textContent()) === 'test-app');

    // 4
    check('Move up is disabled on slot 1 and Move down on slot 8',
      (await page.locator('.card').first().locator('.btn-up').isDisabled()) &&
      (await page.locator('.card').last().locator('.btn-down').isDisabled()));
    await page.locator('.card').nth(5).locator('.btn-up').click();
    shown = await cardNames(page);
    check('Move up swaps Test App with the empty slot above it',
      shown[4] === 'Test App' && shown[5] === null, JSON.stringify(shown));
    check('"Unsaved changes" appears after a move', await dirty.isVisible());
    check('focus stays on the moved card\'s Move up button',
      await page.evaluate(() => document.activeElement &&
        document.activeElement.classList.contains('btn-up') &&
        document.activeElement.closest('.card') === document.querySelectorAll('.card')[4]));
    check('nothing persisted before Save',
      api[5].id === 'test-app' && (await apiButtons(base))[5].id === 'test-app');
    await save(page);
    api = await apiButtons(base);
    check('API has Test App at index 4 and null at index 5',
      api[4] && api[4].name === 'Test App' && api[5] === null, JSON.stringify(slotNames(api)));
    check('Steam X, Discord, SimHub, SimMotion did not move',
      JSON.stringify(slotIds(api.slice(0, 4))) ===
      JSON.stringify(['steam', 'discord', 'simhub', 'simmotion']));

    // 5
    await page.locator('.card').nth(1).locator('.btn-delete').click();
    shown = await cardNames(page);
    check('Delete empties slot 2 in place and nothing is persisted yet',
      shown[1] === null && shown[2] === 'SimHub' && shown[4] === 'Test App' &&
      (await apiButtons(base))[1].id === 'discord', JSON.stringify(shown));
    await save(page);
    api = await apiButtons(base);
    check('API has null at index 1 and no other slot shifted',
      JSON.stringify(slotNames(api)) === JSON.stringify(
        ['Steam X', null, 'SimHub', 'SimMotion', 'Test App', null, null, null]) &&
      api.length === 8, JSON.stringify(slotNames(api)));

    // 6
    const before = JSON.stringify(api);
    await page.locator('.card').nth(1).locator('.btn-add').click();
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
    await page.locator('.card').nth(1).locator('.btn-delete').click();
    check('emptying the invalid slot clears the unsaved indicator', !(await dirty.isVisible()));

    // 8
    await page.reload();
    await page.waitForSelector('.card');
    shown = await cardNames(page);
    check('reloading /edit shows the persisted state',
      JSON.stringify(shown) === JSON.stringify(slotNames(api)), JSON.stringify(shown));
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
    const emptyTiles = await phone.locator('.tile.empty').count();
    check('phone page shows 3 or more empty slots and 8 slots in total',
      emptyTiles >= 3 && (await phone.locator('#grid > *').count()) === 8, String(emptyTiles));
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
