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

// Records the page's WebSockets: every sent JSON frame, and whether each socket has closed.
function record(page) {
  const rec = { sockets: [], frames: [] };
  page.on('websocket', (ws) => {
    const sock = { url: ws.url(), closed: false };
    rec.sockets.push(sock);
    ws.on('framesent', (f) => rec.frames.push(JSON.parse(f.payload)));
    ws.on('close', () => { sock.closed = true; });
  });
  return rec;
}

const sum = (frames, type, key) => frames.filter((m) => m.t === type).reduce((a, m) => a + m[key], 0);
const ofType = (frames, type) => frames.filter((m) => m.t === type);
const chipText = (page) => page.$eval('#chipText', (el) => el.textContent);
const tabState = (page) => page.evaluate(() => ({
  deckHidden: document.getElementById('panel-deck').hidden,
  padHidden: document.getElementById('panel-pad').hidden,
  deckSelected: document.getElementById('tab-deck').getAttribute('aria-selected'),
  padSelected: document.getElementById('tab-pad').getAttribute('aria-selected'),
}));

async function waitConnected(page) {
  await page.waitForFunction(() => document.getElementById('chipText').textContent === 'Connected');
}

async function center(page, selector) {
  return page.$eval(selector, (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  });
}

function fingers(client) {
  const send = (type, pts) => client.send('Input.dispatchTouchEvent', {
    type, touchPoints: pts.map(([x, y, id]) => ({ x, y, id })),
  });
  return {
    down: (pts) => send('touchStart', pts),
    move: (pts) => send('touchMove', pts),
    // touchEnd releases the listed points; an empty list releases every active point
    up: (released) => send('touchEnd', released || []),
  };
}

async function drag(f, starts, dx, dy, steps, held = []) {
  await f.down(held.concat(starts));
  for (let i = 1; i <= steps; i++) {
    await f.move(held.concat(starts.map(([x, y, id]) => [x + (dx * i) / steps, y + (dy * i) / steps, id])));
    await sleep(20);
  }
  await f.up(starts.map(([x, y, id]) => [x + dx, y + dy, id]));
  await sleep(150);
}

async function metrics(page) {
  return page.evaluate(() => {
    const box = (el) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height, right: r.right, bottom: r.bottom };
    };
    return {
      scrollW: document.documentElement.scrollWidth,
      scrollH: document.documentElement.scrollHeight,
      innerW: window.innerWidth,
      innerH: window.innerHeight,
      bar: box(document.querySelector('.tabbar')),
      tiles: [...document.querySelectorAll('#grid > *')].map(box),
      pad: document.getElementById('pad') ? box(document.getElementById('pad')) : null,
      kb: box(document.getElementById('kbBtn')),
      left: box(document.querySelector('.click[data-btn=left]')),
      right: box(document.querySelector('.click[data-btn=right]')),
      chip: box(document.getElementById('chip')),
    };
  });
}

async function layoutChecks(page, label, portrait) {
  await page.click('#tab-deck');
  await sleep(100);
  let m = await metrics(page);
  check(label + ' deck: no horizontal overflow and no vertical page scroll with the tab bar visible',
    m.scrollW <= m.innerW && m.scrollH <= m.innerH && m.bar.w > 0 && m.bar.h > 0,
    JSON.stringify({ sw: m.scrollW, iw: m.innerW, sh: m.scrollH, ih: m.innerH }));
  const cols = new Set(m.tiles.map((r) => Math.round(r.x))).size;
  const rows = new Set(m.tiles.map((r) => Math.round(r.y))).size;
  const area = portrait
    ? (r) => r.bottom <= m.bar.y + 0.5 && r.x >= 0 && r.right <= m.innerW && r.y >= 0
    : (r) => r.x >= m.bar.right - 0.5 && r.bottom <= m.innerH && r.right <= m.innerW && r.y >= 0;
  check(label + ' deck: 8 slots in a ' + (portrait ? '2x4' : '4x2') + ' grid, all fully visible next to the tab bar',
    m.tiles.length === 8 && cols === (portrait ? 2 : 4) && rows === (portrait ? 4 : 2) &&
    m.tiles.every(area), JSON.stringify({ cols, rows, bar: m.bar, tiles: m.tiles }));

  await page.click('#tab-pad');
  await waitConnected(page);
  await sleep(100);
  m = await metrics(page);
  check(label + ' trackpad: no horizontal overflow and no vertical page scroll',
    m.scrollW <= m.innerW && m.scrollH <= m.innerH,
    JSON.stringify({ sw: m.scrollW, iw: m.innerW, sh: m.scrollH, ih: m.innerH }));
  const inside = (r) => r.x >= 0 && r.y >= 0 && r.right <= m.innerW && r.bottom <= m.innerH;
  check(label + ' trackpad: pad, chip, keyboard and L/R buttons are fully on screen',
    [m.pad, m.chip, m.kb, m.left, m.right].every(inside), JSON.stringify(m));
  const padArea = m.pad.w * m.pad.h;
  const rest = [m.left, m.right, m.kb, m.chip].reduce((a, r) => a + r.w * r.h, 0);
  check(label + ' trackpad: the pad is the dominant area',
    padArea > 0.4 * m.innerW * m.innerH && padArea > 2 * rest,
    Math.round(padArea) + ' of ' + m.innerW * m.innerH + ', rest ' + Math.round(rest));
  check(label + ' trackpad: L, R and keyboard buttons are at least 44x44',
    [m.left, m.right, m.kb].every((r) => r.w >= 44 && r.h >= 44),
    JSON.stringify([m.left, m.right, m.kb]));
  check(label + ' trackpad: nothing overlaps the pad',
    [m.left, m.right, m.kb, m.chip].every((r) =>
      r.right <= m.pad.x + 0.5 || r.x >= m.pad.right - 0.5 || r.bottom <= m.pad.y + 0.5 || r.y >= m.pad.bottom - 0.5),
    JSON.stringify(m));
}

async function run(base) {
  const browser = await chromium.launch();
  try {
    const problems = [];

    // ---- 1. tabs and persistence
    const context = await browser.newContext({ ...devices['iPhone 13'] });
    const page = await context.newPage();
    watch(page, problems);
    const rec = record(page);
    const client = await context.newCDPSession(page);
    const f = fingers(client);
    await page.goto(base + '/');
    await page.waitForSelector('.tile');

    let st = await tabState(page);
    check('fresh load shows Deck and hides the Trackpad panel',
      !st.deckHidden && st.padHidden && st.deckSelected === 'true' && st.padSelected === 'false',
      JSON.stringify(st));
    await sleep(300);
    check('no websocket is opened while Deck is shown', rec.sockets.length === 0, String(rec.sockets.length));
    await page.screenshot({ path: path.join(SHOT_DIR, 'deck-portrait.png') });

    // ---- 2. websocket lifecycle
    await page.click('#tab-pad');
    st = await tabState(page);
    check('switching to Trackpad shows its panel and hides the deck',
      st.deckHidden && !st.padHidden && st.padSelected === 'true' && st.deckSelected === 'false',
      JSON.stringify(st));
    await waitConnected(page);
    check('a websocket to /ws opens when Trackpad is shown and the chip says "Connected"',
      rec.sockets.length === 1 && /^ws:\/\/[^/]+\/ws$/.test(rec.sockets[0].url) && (await chipText(page)) === 'Connected',
      JSON.stringify(rec.sockets));
    check('chip dot is green when connected',
      (await page.$eval('#chip', (el) => getComputedStyle(el, '::before').backgroundColor)) === 'rgb(46, 204, 113)');

    await page.reload();
    await waitConnected(page);
    st = await tabState(page);
    check('the Trackpad choice survives a reload', !st.padHidden && st.deckHidden && st.padSelected === 'true',
      JSON.stringify(st));
    check('reload closed the old socket and opened one new socket',
      rec.sockets.length === 2 && rec.sockets[1].closed === false, JSON.stringify(rec.sockets));

    await page.click('#tab-deck');
    await until(() => rec.sockets[1].closed, 'the websocket to close after switching to Deck');
    check('the websocket closes when switching back to Deck', true);
    await page.reload();
    await page.waitForSelector('.tile');
    st = await tabState(page);
    check('the Deck choice is remembered the same way', !st.deckHidden && st.padHidden, JSON.stringify(st));
    await sleep(200);
    check('reloading on Deck opens no websocket', rec.sockets.length === 2, String(rec.sockets.length));

    await page.click('#tab-pad');
    await waitConnected(page);
    check('switching to Trackpad again opens a new socket', rec.sockets.length === 3 && !rec.sockets[2].closed);
    await page.screenshot({ path: path.join(SHOT_DIR, 'pad-portrait.png') });

    // ---- 3. one finger drag
    const pad = await page.$eval('#pad', (el) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height };
    });
    const cx = pad.x + pad.w / 2;
    const cy = pad.y + pad.h / 2;
    let mark = rec.frames.length;
    await drag(f, [[cx - 50, cy - 25, 1]], 100, 50, 10);
    let got = rec.frames.slice(mark);
    const moves = ofType(got, 'move');
    const dxSum = sum(got, 'move', 'dx');
    const dySum = sum(got, 'move', 'dy');
    check('one-finger drag of (+100,+50) yields moves summing to about (160, 80)',
      moves.length > 0 && Math.abs(dxSum - 160) <= 2 && Math.abs(dySum - 80) <= 2,
      dxSum + ',' + dySum);
    check('the drag sends only integer moves in range and no click or scroll',
      got.every((m) => m.t === 'move' && Number.isInteger(m.dx) && Number.isInteger(m.dy) &&
        Math.abs(m.dx) <= 500 && Math.abs(m.dy) <= 500), JSON.stringify(got));

    // ---- 4. tap = left click
    mark = rec.frames.length;
    await f.down([[cx, cy, 1]]);
    await f.up([]);
    await sleep(300);
    got = rec.frames.slice(mark);
    check('a quick one-finger tap yields exactly one left click and no moves',
      JSON.stringify(got) === JSON.stringify([{ t: 'click', b: 'left' }]), JSON.stringify(got));

    // a slow press (over 250 ms) is not a click
    mark = rec.frames.length;
    await f.down([[cx, cy, 1]]);
    await sleep(400);
    await f.up([]);
    await sleep(200);
    check('a long press without movement sends no click',
      ofType(rec.frames.slice(mark), 'click').length === 0, JSON.stringify(rec.frames.slice(mark)));

    // ---- 5. two-finger tap = right click
    mark = rec.frames.length;
    await f.down([[cx - 40, cy, 1], [cx + 40, cy, 2]]);
    await f.up([]);
    await sleep(300);
    got = rec.frames.slice(mark);
    check('a quick two-finger tap yields a right click and no left click',
      JSON.stringify(got) === JSON.stringify([{ t: 'click', b: 'right' }]), JSON.stringify(got));

    // ---- 6. two-finger scroll
    mark = rec.frames.length;
    await drag(f, [[cx - 40, cy + 50, 1], [cx + 40, cy + 50, 2]], 0, -100, 10);
    got = rec.frames.slice(mark);
    const sy = sum(got, 'scroll', 'dy');
    const sx = sum(got, 'scroll', 'dx');
    check('two fingers dragging up 100 px yield scroll messages summing to dy about +100',
      Math.abs(sy - 100) <= 3 && Math.abs(sx) <= 1, sx + ',' + sy);
    check('the scroll gesture sends only scrolls (no click, no cursor move)',
      got.length > 0 && got.every((m) => m.t === 'scroll'), JSON.stringify(got));

    // after a two-finger gesture the remaining finger must not move the cursor
    mark = rec.frames.length;
    await f.down([[cx - 40, cy, 1], [cx + 40, cy, 2]]);
    await f.up([[cx - 40, cy, 1]]);
    for (let i = 1; i <= 5; i++) {
      await f.move([[cx + 40 + i * 10, cy + i * 10, 2]]);
      await sleep(20);
    }
    await f.up([]);
    await sleep(200);
    check('the remaining finger does not move the cursor after a two-finger gesture',
      ofType(rec.frames.slice(mark), 'move').length === 0, JSON.stringify(rec.frames.slice(mark)));

    // touch dots and scroll chip while two touches are held
    await f.down([[cx - 60, cy - 40, 1], [cx + 60, cy + 40, 2]]);
    await f.move([[cx - 55, cy - 38, 1], [cx + 65, cy + 42, 2]]);
    await sleep(250);
    const live = await page.evaluate(() => ({
      dots: document.querySelectorAll('#pad .dot').length,
      touched: document.getElementById('pad').classList.contains('touched'),
      chipHidden: document.getElementById('scrollChip').hidden,
      chipText: document.getElementById('scrollChip').textContent.trim(),
    }));
    check('two touches show two dots, the pad glow and the "Scroll" chip',
      live.dots === 2 && live.touched && !live.chipHidden && live.chipText === 'Scroll', JSON.stringify(live));
    await page.screenshot({ path: path.join(SHOT_DIR, 'pad-touch.png') });
    await f.up([]);
    await sleep(200);
    const after = await page.evaluate(() => ({
      dots: document.querySelectorAll('#pad .dot').length,
      touched: document.getElementById('pad').classList.contains('touched'),
      chipHidden: document.getElementById('scrollChip').hidden,
    }));
    check('releasing removes the dots, glow and chip',
      after.dots === 0 && !after.touched && after.chipHidden, JSON.stringify(after));

    // ---- 7. L / R buttons
    const info = await page.evaluate(() => ['.click[data-btn=left]', '.click[data-btn=right]', '#kbBtn'].map((s) => {
      const el = document.querySelector(s);
      const half = el.querySelector('.half');
      const bb = half ? half.getBBox() : null;
      return {
        text: el.textContent.trim(), label: el.getAttribute('aria-label'), tag: el.tagName,
        halfX: bb && bb.x, halfRight: bb && bb.x + bb.width,
        halfFill: half && getComputedStyle(half).fill, halfOpacity: half && getComputedStyle(half).opacity,
        svgs: el.querySelectorAll('svg').length,
      };
    }));
    check('L, R and Keyboard buttons are real <button>s with no visible text and the right aria-labels',
      info.every((i) => i.tag === 'BUTTON' && i.text === '' && i.svgs === 1) &&
      info[0].label === 'Left click' && info[1].label === 'Right click' && info[2].label === 'Keyboard',
      JSON.stringify(info));
    check('the Left icon has its left half highlighted and the Right icon its right half, even at rest',
      info[0].halfRight <= 12.01 && info[1].halfX >= 11.99 &&
      info.slice(0, 2).every((i) => i.halfFill !== 'none' && parseFloat(i.halfOpacity) > 0.3),
      JSON.stringify(info.slice(0, 2)));
    check('there is no "Trackpad" title or "Phone as mouse" subtitle on the page',
      !(await page.evaluate(() => /Phone as mouse/.test(document.body.textContent) || !!document.querySelector('h1'))));

    for (const name of ['left', 'right']) {
      const c = await center(page, '.click[data-btn=' + name + ']');
      mark = rec.frames.length;
      await f.down([[c.x, c.y, 1]]);
      await sleep(60);
      const pressed = await page.$eval('.click[data-btn=' + name + ']', (el) => el.classList.contains('pressed'));
      await f.up([]);
      await sleep(100);
      got = rec.frames.slice(mark);
      const stillPressed = await page.$eval('.click[data-btn=' + name + ']', (el) => el.classList.contains('pressed'));
      check(name + ' button: press then release sends btn d:true then d:false once each',
        pressed && !stillPressed && JSON.stringify(got) === JSON.stringify([
          { t: 'btn', b: name, d: true }, { t: 'btn', b: name, d: false }]), JSON.stringify(got));
    }

    // hold-and-drag: left held while a finger drags on the pad
    const lc = await center(page, '.click[data-btn=left]');
    mark = rec.frames.length;
    await f.down([[lc.x, lc.y, 5]]);
    await sleep(40);
    await drag(f, [[cx - 40, cy, 1]], 80, 0, 8, [[lc.x, lc.y, 5]]);
    await sleep(50);
    got = rec.frames.slice(mark);
    check('holding Left while dragging on the pad: btn down, moves, and Left stays held',
      got[0] && got[0].t === 'btn' && got[0].d === true && ofType(got, 'move').length > 0 &&
      !got.some((m) => m.t === 'btn' && m.d === false), JSON.stringify(got.slice(0, 4)));

    // ---- 8. held button released when the tab is left
    mark = rec.frames.length;
    await page.click('#tab-deck');
    await until(() => rec.sockets[rec.sockets.length - 1].closed, 'the socket to close');
    got = rec.frames.slice(mark);
    check('leaving the Trackpad tab with Left held sends btn left d:false exactly once',
      got.filter((m) => m.t === 'btn' && m.b === 'left' && m.d === false).length === 1, JSON.stringify(got));
    await f.up([]);
    await sleep(100);
    check('letting go afterwards sends nothing more',
      rec.frames.slice(mark).filter((m) => m.t === 'btn').length === 1, JSON.stringify(rec.frames.slice(mark)));

    // ---- 9. keyboard
    await page.click('#tab-pad');
    await waitConnected(page);
    await page.tap('#kbBtn');
    await until(async () => (await page.evaluate(() => document.activeElement.id)) === 'kbInput', 'the input to get focus');
    check('tapping the keyboard button focuses the hidden input and shows "Keyboard on · 0 keys sent"',
      (await chipText(page)) === 'Keyboard on · 0 keys sent' &&
      (await page.$eval('#kbBtn', (el) => el.getAttribute('aria-pressed'))) === 'true', await chipText(page));
    const inputProps = await page.$eval('#kbInput', (el) => ({
      fs: getComputedStyle(el).fontSize, op: getComputedStyle(el).opacity,
      cap: el.getAttribute('autocapitalize'), corr: el.getAttribute('autocorrect'),
      comp: el.getAttribute('autocomplete'), spell: el.getAttribute('spellcheck'),
      mode: el.getAttribute('inputmode'), enter: el.getAttribute('enterkeyhint'),
    }));
    check('the hidden input is 16px, invisible and has the IME attributes',
      inputProps.fs === '16px' && inputProps.op === '0' && inputProps.cap === 'off' &&
      inputProps.corr === 'off' && inputProps.comp === 'off' && inputProps.spell === 'false' &&
      inputProps.mode === 'text' && inputProps.enter === 'enter', JSON.stringify(inputProps));
    await page.screenshot({ path: path.join(SHOT_DIR, 'pad-keyboard.png') });

    mark = rec.frames.length;
    await page.keyboard.type('hi');
    await sleep(100);
    got = rec.frames.slice(mark);
    check('typing "hi" sends text messages that add up to "hi"',
      got.length > 0 && got.every((m) => m.t === 'text') && got.map((m) => m.s).join('') === 'hi', JSON.stringify(got));
    check('the chip counts 2 keys and the input is empty again',
      (await chipText(page)) === 'Keyboard on · 2 keys sent' &&
      (await page.$eval('#kbInput', (el) => el.value)) === '', await chipText(page));
    check('the typed letters are never displayed',
      !(await page.evaluate(() => document.body.innerText.includes('hi'))));

    mark = rec.frames.length;
    await page.keyboard.press('Backspace');
    await sleep(60);
    check('Backspace sends one key backspace message',
      JSON.stringify(rec.frames.slice(mark)) === JSON.stringify([{ t: 'key', k: 'backspace' }]),
      JSON.stringify(rec.frames.slice(mark)));
    mark = rec.frames.length;
    await page.keyboard.press('Enter');
    await sleep(60);
    check('Enter sends one key enter message',
      JSON.stringify(rec.frames.slice(mark)) === JSON.stringify([{ t: 'key', k: 'enter' }]),
      JSON.stringify(rec.frames.slice(mark)));
    check('the input value is empty after Backspace and Enter and the chip says 4 keys',
      (await page.$eval('#kbInput', (el) => el.value)) === '' &&
      (await chipText(page)) === 'Keyboard on · 4 keys sent', await chipText(page));

    mark = rec.frames.length;
    await page.keyboard.insertText('äß!');
    await sleep(60);
    got = rec.frames.slice(mark);
    check('inserted text (umlaut, sharp s, symbol) is sent unchanged',
      got.map((m) => m.s).join('') === 'äß!' && got.every((m) => m.t === 'text'), JSON.stringify(got));

    mark = rec.frames.length;
    await page.evaluate(() => { document.getElementById('kbInput').dispatchEvent(new CompositionEvent('compositionstart')); });
    await page.keyboard.insertText('abc');
    await sleep(60);
    const midComposition = rec.frames.slice(mark).length;
    await page.evaluate(() => {
      const i = document.getElementById('kbInput');
      i.dispatchEvent(new CompositionEvent('compositionend', { data: 'abc' }));
    });
    await sleep(60);
    got = rec.frames.slice(mark);
    check('text typed during an IME composition is sent once, when the composition ends',
      midComposition === 0 && got.length === 1 && got[0].t === 'text' && got[0].s === 'abc' &&
      (await page.$eval('#kbInput', (el) => el.value)) === '', JSON.stringify(got));

    mark = rec.frames.length;
    await page.evaluate(() => {
      const i = document.getElementById('kbInput');
      i.value = 'x'.repeat(150);
      i.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await sleep(60);
    got = rec.frames.slice(mark);
    check('a 150 character paste is split into text messages of at most 64 characters',
      got.length === 3 && got.every((m) => m.t === 'text' && m.s.length <= 64) &&
      got.map((m) => m.s).join('') === 'x'.repeat(150), JSON.stringify(got.map((m) => m.s.length)));

    await page.tap('#kbBtn');
    await until(async () => (await page.evaluate(() => document.activeElement.id)) !== 'kbInput', 'the input to blur');
    check('tapping the keyboard button again blurs the input and restores the connection chip',
      (await chipText(page)) === 'Connected' &&
      (await page.$eval('#kbBtn', (el) => el.getAttribute('aria-pressed'))) === 'false', await chipText(page));

    await page.tap('#kbBtn');
    await until(async () => (await page.evaluate(() => document.activeElement.id)) === 'kbInput', 'the input to get focus again');
    await page.click('#tab-deck');
    await until(async () => (await page.evaluate(() => document.activeElement.id)) !== 'kbInput', 'the input to blur on tab switch');
    check('switching tab blurs the keyboard input', true);

    // ---- back on the Trackpad tab after leaving it
    await page.click('#tab-pad');
    await waitConnected(page);
    check('chip shows "Connected" again after switching tabs', (await chipText(page)) === 'Connected');

    // ---- 10. layouts
    await layoutChecks(page, 'portrait', true);

    // --app-h follows the visual viewport (keeps the page above the on-screen keyboard)
    await page.setViewportSize({ width: 390, height: 520 });
    await sleep(200);
    const vp = await page.evaluate(() => ({
      appH: Math.round(document.querySelector('.app').getBoundingClientRect().height),
      innerH: window.innerHeight,
      scrollH: document.documentElement.scrollHeight,
      prop: document.documentElement.style.getPropertyValue('--app-h'),
    }));
    check('--app-h follows the visual viewport and the page does not scroll when it shrinks',
      Math.abs(vp.appH - vp.innerH) <= 1 && vp.scrollH <= vp.innerH && vp.prop !== '', JSON.stringify(vp));
    await page.setViewportSize({ width: 390, height: 664 });
    await sleep(100);

    const landscape = await browser.newContext({ ...devices['iPhone 13 landscape'] });
    const lpage = await landscape.newPage();
    watch(lpage, problems);
    await lpage.goto(base + '/');
    await lpage.waitForSelector('.tile');
    await lpage.waitForFunction(() => [...document.querySelectorAll('.tile img')].every((i) => i.complete));
    await layoutChecks(lpage, 'landscape', false);
    await lpage.screenshot({ path: path.join(SHOT_DIR, 'pad-landscape.png') });
    const lm = await metrics(lpage);
    check('landscape: the pad is on the left and the keyboard and L/R buttons are in the right column',
      [lm.kb, lm.left, lm.right].every((r) => r.x >= lm.pad.right - 0.5) &&
      lm.kb.y < lm.left.y && lm.left.y < lm.right.y, JSON.stringify(lm));

    // ---- storage unavailable: page still works
    const noStore = await browser.newContext({ ...devices['iPhone 13'] });
    const npage = await noStore.newPage();
    watch(npage, problems);
    await npage.addInitScript(() => {
      const fail = () => { throw new Error('storage blocked'); };
      Object.defineProperty(window, 'localStorage', { get: fail });
    });
    await npage.goto(base + '/');
    await npage.waitForSelector('.tile');
    await npage.click('#tab-pad');
    await waitConnected(npage);
    const nst = await tabState(npage);
    check('without storage the page still loads on Deck and switches tabs',
      !nst.padHidden && nst.deckHidden);
    await npage.reload();
    await npage.waitForSelector('.tile');
    check('without storage a reload falls back to Deck', (await tabState(npage)).padHidden);
    await noStore.close();

    // ---- 11. no errors
    await landscape.close();
    check('no console errors, page errors or failed requests', problems.length === 0, problems.join('; '));
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
