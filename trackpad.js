(function () {
  "use strict";

  var GAIN = 1.6;
  var TAP_MS = 250;
  var TAP_SLOP = 10;
  var TWO_TAP_MS = 350;
  var TWO_SLOP = 12;
  var MAX_MOVE = 500;
  var MAX_SCROLL = 400;
  var MAX_TEXT = 64;
  var BACKOFF = [500, 1000, 2000, 3000];
  var CONN_TEXT = { connecting: "Connecting…", connected: "Connected", offline: "Offline" };

  var panel = document.getElementById("panel-pad");
  var pad = document.getElementById("pad");
  var chip = document.getElementById("chip");
  var chipText = document.getElementById("chipText");
  var scrollChip = document.getElementById("scrollChip");
  var kbBtn = document.getElementById("kbBtn");
  var kbInput = document.getElementById("kbInput");
  var clickButtons = Array.prototype.slice.call(document.querySelectorAll(".click"));

  var tabActive = !panel.hidden;
  var ws = null;
  var retryTimer = null;
  var attempt = 0;
  var connState = "connecting";
  var held = { left: false, right: false };
  var keys = 0;

  /* ---------- websocket ---------- */

  function wsUrl() {
    return (location.protocol === "https:" ? "wss" : "ws") + "://" + location.host + "/ws";
  }

  function send(msg) {
    if (!ws || ws.readyState !== 1) return false;
    ws.send(JSON.stringify(msg));
    return true;
  }

  function renderChip() {
    var typing = document.activeElement === kbInput;
    chip.dataset.state = typing ? "keyboard" : connState;
    chipText.textContent = typing
      ? "Keyboard on · " + keys + (keys === 1 ? " key" : " keys") + " sent"
      : CONN_TEXT[connState];
    kbBtn.setAttribute("aria-pressed", typing ? "true" : "false");
  }

  function setConn(state) {
    connState = state;
    renderChip();
  }

  function wanted() {
    return tabActive && document.visibilityState === "visible";
  }

  function scheduleRetry() {
    var delay = BACKOFF[Math.min(attempt, BACKOFF.length - 1)];
    attempt += 1;
    retryTimer = setTimeout(connect, delay);
  }

  function connect() {
    retryTimer = null;
    if (!wanted()) return;
    var sock;
    try {
      sock = new WebSocket(wsUrl());
    } catch (e) {
      setConn("offline");
      scheduleRetry();
      return;
    }
    ws = sock;
    sock.onopen = function () {
      if (ws !== sock) return;
      attempt = 0;
      setConn("connected");
    };
    sock.onclose = function () {
      if (ws !== sock) return;
      ws = null;
      markReleased();
      setConn("offline");
      if (wanted()) scheduleRetry();
    };
  }

  function stop() {
    clearTimeout(retryTimer);
    retryTimer = null;
    attempt = 0;
    releaseAll();
    resetPad();
    kbInput.blur();
    var sock = ws;
    ws = null;
    if (sock) {
      sock.onopen = null;
      sock.onclose = null;
      try { sock.close(); } catch (e) { /* already closing */ }
    }
  }

  function sync() {
    if (!wanted()) {
      stop();
      return;
    }
    if (!ws && retryTimer === null) {
      setConn("connecting");
      connect();
    }
  }

  /* ---------- pad ---------- */

  var pointers = new Map();
  var gesture = null;
  var acc = { mx: 0, my: 0, sx: 0, sy: 0 };
  var rem = { mx: 0, my: 0, sx: 0, sy: 0 };
  var raf = 0;

  function clamp(v, limit) {
    return Math.max(-limit, Math.min(limit, v));
  }

  function sendDelta(type, dx, dy, limit) {
    while (dx !== 0 || dy !== 0) {
      var cx = clamp(dx, limit);
      var cy = clamp(dy, limit);
      send({ t: type, dx: cx, dy: cy });
      dx -= cx;
      dy -= cy;
    }
  }

  function flush() {
    if (raf) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
    var mx = acc.mx, my = acc.my, sx = acc.sx, sy = acc.sy;
    acc.mx = acc.my = acc.sx = acc.sy = 0;
    sendDelta("move", mx, my, MAX_MOVE);
    sendDelta("scroll", sx, sy, MAX_SCROLL);
  }

  function schedule() {
    if (!raf) raf = requestAnimationFrame(flush);
  }

  function addDelta(kx, ky, dx, dy) {
    var fx = dx + rem[kx];
    var fy = dy + rem[ky];
    var ix = Math.trunc(fx);
    var iy = Math.trunc(fy);
    rem[kx] = fx - ix;
    rem[ky] = fy - iy;
    acc[kx] += ix;
    acc[ky] += iy;
    if (ix !== 0 || iy !== 0) schedule();
  }

  function padPoint(e) {
    var r = pad.getBoundingClientRect();
    return { x: e.clientX - r.left - pad.clientLeft, y: e.clientY - r.top - pad.clientTop };
  }

  function place(el, p) {
    el.style.transform = "translate(" + p.x + "px," + p.y + "px)";
  }

  function syncPad() {
    pad.classList.toggle("touched", pointers.size > 0);
    scrollChip.hidden = pointers.size < 2;
  }

  function ripple(p) {
    var r = document.createElement("div");
    r.className = "ripple";
    r.style.left = p.x + "px";
    r.style.top = p.y + "px";
    pad.appendChild(r);
    r.addEventListener("animationend", function () { r.remove(); });
    setTimeout(function () { r.remove(); }, 600);
  }

  function resetRemainders() {
    rem.mx = rem.my = rem.sx = rem.sy = 0;
  }

  function resetPad() {
    if (raf) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
    pointers.forEach(function (p) { p.dot.remove(); });
    pointers.clear();
    gesture = null;
    acc.mx = acc.my = acc.sx = acc.sy = 0;
    resetRemainders();
    syncPad();
  }

  function finishGesture(now) {
    flush();
    resetRemainders();
    var g = gesture;
    gesture = null;
    if (!g || g.cancelled) return;
    if (!g.multi) {
      if (now - g.t0 <= TAP_MS && g.travel < TAP_SLOP) send({ t: "click", b: "left" });
    } else if (now - g.twoT <= TWO_TAP_MS && g.twoDist <= TWO_SLOP) {
      send({ t: "click", b: "right" });
    }
  }

  function release(e, cancelled) {
    var p = pointers.get(e.pointerId);
    if (!p) return;
    var now = performance.now();
    if (cancelled) gesture.cancelled = true;
    else if (now - p.t0 <= TWO_TAP_MS && Math.hypot(e.clientX - p.x0, e.clientY - p.y0) < TWO_SLOP) {
      ripple(padPoint(e));
    }
    p.dot.remove();
    pointers.delete(e.pointerId);
    if (pointers.size === 0) finishGesture(now);
    syncPad();
  }

  pad.addEventListener("pointerdown", function (e) {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    if (pointers.has(e.pointerId) || pointers.size >= 2) return;
    e.preventDefault();
    try { pad.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
    var now = performance.now();
    var dot = document.createElement("div");
    dot.className = "dot";
    place(dot, padPoint(e));
    pad.appendChild(dot);
    pointers.set(e.pointerId, {
      x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY, t0: now, dot: dot
    });
    if (!gesture) gesture = { multi: false, cancelled: false, travel: 0, t0: now, twoT: 0, twoDist: 0 };
    if (pointers.size === 2) {
      gesture.multi = true;
      gesture.twoT = now;
      gesture.twoDist = 0;
      pointers.forEach(function (q) { q.x0 = q.x; q.y0 = q.y; });
      resetRemainders();
    }
    syncPad();
  });

  pad.addEventListener("pointermove", function (e) {
    var p = pointers.get(e.pointerId);
    if (!p) return;
    var dx = e.clientX - p.x;
    var dy = e.clientY - p.y;
    p.x = e.clientX;
    p.y = e.clientY;
    place(p.dot, padPoint(e));
    if (!gesture.multi) {
      gesture.travel += Math.hypot(dx, dy);
      addDelta("mx", "my", dx * GAIN, dy * GAIN);
    } else {
      gesture.twoDist = Math.max(gesture.twoDist, Math.hypot(p.x - p.x0, p.y - p.y0));
      if (pointers.size === 2) addDelta("sx", "sy", -dx / 2, -dy / 2);
    }
  });

  pad.addEventListener("pointerup", function (e) { release(e, false); });
  pad.addEventListener("pointercancel", function (e) { release(e, true); });
  pad.addEventListener("lostpointercapture", function (e) { release(e, true); });
  pad.addEventListener("contextmenu", function (e) { e.preventDefault(); });
  pad.addEventListener("mousedown", function (e) { e.preventDefault(); });

  /* ---------- left / right buttons ---------- */

  function markReleased() {
    clickButtons.forEach(function (btn) {
      held[btn.dataset.btn] = false;
      btn.classList.remove("pressed");
    });
  }

  function releaseAll() {
    clickButtons.forEach(function (btn) {
      var name = btn.dataset.btn;
      if (held[name]) send({ t: "btn", b: name, d: false });
      held[name] = false;
      btn.classList.remove("pressed");
    });
  }

  clickButtons.forEach(function (btn) {
    var name = btn.dataset.btn;
    var activeId = null;

    function up(e) {
      if (activeId === null || e.pointerId !== activeId) return;
      activeId = null;
      btn.classList.remove("pressed");
      if (held[name]) {
        held[name] = false;
        send({ t: "btn", b: name, d: false });
      }
    }

    btn.addEventListener("pointerdown", function (e) {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      if (activeId !== null) return;
      e.preventDefault();
      activeId = e.pointerId;
      try { btn.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
      btn.classList.add("pressed");
      if (!held[name] && send({ t: "btn", b: name, d: true })) held[name] = true;
    });
    btn.addEventListener("pointerup", up);
    btn.addEventListener("pointercancel", up);
    btn.addEventListener("lostpointercapture", up);
    btn.addEventListener("contextmenu", function (e) { e.preventDefault(); });
    btn.addEventListener("mousedown", function (e) { e.preventDefault(); });
  });

  /* ---------- keyboard ---------- */

  var composing = false;
  var wasOpen = false;

  function sendKey(name) {
    if (send({ t: "key", k: name })) {
      keys += 1;
      renderChip();
    }
  }

  function sendText(text) {
    var chars = Array.from(text);
    for (var i = 0; i < chars.length; i += MAX_TEXT) {
      var chunk = chars.slice(i, i + MAX_TEXT);
      if (send({ t: "text", s: chunk.join("") })) keys += chunk.length;
    }
    renderChip();
  }

  function flushInput(text) {
    kbInput.value = "";
    if (text) sendText(text);
  }

  kbBtn.addEventListener("pointerdown", function () {
    wasOpen = document.activeElement === kbInput;
  });
  kbBtn.addEventListener("mousedown", function (e) { e.preventDefault(); });
  kbBtn.addEventListener("click", function (e) {
    var open = e.detail === 0 ? document.activeElement === kbInput : wasOpen;
    if (open) kbInput.blur();
    else kbInput.focus({ preventScroll: true });
  });

  kbInput.addEventListener("focus", renderChip);
  kbInput.addEventListener("blur", renderChip);

  kbInput.addEventListener("keydown", function (e) {
    if (e.isComposing) return;
    if (e.key === "Backspace" || e.key === "Enter") {
      e.preventDefault();
      sendKey(e.key === "Enter" ? "enter" : "backspace");
    }
  });

  kbInput.addEventListener("beforeinput", function (e) {
    if (e.inputType === "deleteContentBackward") {
      e.preventDefault();
      sendKey("backspace");
    } else if (e.inputType === "insertLineBreak" || e.inputType === "insertParagraph") {
      e.preventDefault();
      sendKey("enter");
    }
  });

  kbInput.addEventListener("compositionstart", function () { composing = true; });
  kbInput.addEventListener("compositionend", function (e) {
    composing = false;
    flushInput(e.data || kbInput.value);
  });
  kbInput.addEventListener("input", function (e) {
    if (composing || e.isComposing) return;
    flushInput(kbInput.value);
  });

  /* ---------- lifecycle ---------- */

  document.addEventListener("deck:tab", function (e) {
    tabActive = !!e.detail && e.detail.tab === "pad";
    sync();
  });
  document.addEventListener("visibilitychange", sync);
  window.addEventListener("pagehide", stop);
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) sync();
  });

  renderChip();
  sync();
})();
