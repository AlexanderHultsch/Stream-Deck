(function () {
  "use strict";

  var BACKOFF = [500, 1000, 2000, 3000];
  var STARTING_MS = 10000;

  var panel = document.getElementById("panel-deck");
  var grid = document.getElementById("grid");

  var tabActive = !panel.hidden;
  var ws = null;
  var retryTimer = null;
  var attempt = 0;
  var running = new Map();
  var starting = new Map();

  /* ---------- dots ---------- */

  function paint() {
    var tiles = grid.querySelectorAll(".tile[data-id]");
    for (var i = 0; i < tiles.length; i++) {
      var tile = tiles[i];
      var id = tile.dataset.id;
      var isRunning = running.get(id) === true;
      var kind = isRunning ? "run" : starting.has(id) ? "starting" : "";
      var dot = tile.querySelector(".dot");
      if (kind) {
        if (!dot) {
          dot = document.createElement("span");
          dot.setAttribute("aria-hidden", "true");
          tile.appendChild(dot);
        }
        dot.className = "dot " + kind;
      } else if (dot) {
        dot.remove();
      }
      var label = tile.querySelector(".label");
      var name = label ? label.textContent : "";
      tile.setAttribute("aria-label", isRunning ? name + ", running" : name);
    }
  }

  function endStarting(id) {
    clearTimeout(starting.get(id));
    starting.delete(id);
  }

  function clearStarting() {
    starting.forEach(function (timer) { clearTimeout(timer); });
    starting.clear();
  }

  function markStarting(id) {
    endStarting(id);
    starting.set(id, setTimeout(function () {
      starting.delete(id);
      paint();
    }, STARTING_MS));
  }

  /* ---------- websocket ---------- */

  function wsUrl() {
    return (location.protocol === "https:" ? "wss" : "ws") + "://" + location.host + "/ws/status";
  }

  function wanted() {
    return tabActive && document.visibilityState === "visible";
  }

  function onMessage(msg, first) {
    if (!msg || msg.t !== "status" || !msg.running || typeof msg.running !== "object") return false;
    if (first) running.clear();
    Object.keys(msg.running).forEach(function (id) {
      var value = msg.running[id];
      if (typeof value !== "boolean") return;
      running.set(id, value);
      if (value) endStarting(id);
    });
    return true;
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
      scheduleRetry();
      return;
    }
    ws = sock;
    var first = true;
    sock.onopen = function () {
      if (ws !== sock) return;
      attempt = 0;
    };
    sock.onmessage = function (event) {
      if (ws !== sock) return;
      var msg = null;
      try {
        msg = JSON.parse(event.data);
      } catch (e) { /* malformed, ignored */ }
      if (onMessage(msg, first)) {
        first = false;
        paint();
      }
    };
    sock.onclose = function () {
      if (ws !== sock) return;
      ws = null;
      running.clear();
      paint();
      if (wanted()) scheduleRetry();
    };
  }

  function stop() {
    clearTimeout(retryTimer);
    retryTimer = null;
    attempt = 0;
    clearStarting();
    running.clear();
    var sock = ws;
    ws = null;
    if (sock) {
      sock.onopen = null;
      sock.onmessage = null;
      sock.onclose = null;
      try { sock.close(); } catch (e) { /* already closing */ }
    }
    paint();
  }

  function sync() {
    if (!wanted()) {
      stop();
      return;
    }
    if (!ws && retryTimer === null) connect();
  }

  /* ---------- lifecycle ---------- */

  document.addEventListener("deck:tab", function (e) {
    tabActive = !!e.detail && e.detail.tab === "deck";
    sync();
  });
  document.addEventListener("deck:rendered", paint);
  document.addEventListener("deck:pressed", function (e) {
    var d = e.detail;
    if (!wanted() || !d || d.ok !== true || d.action !== "launched") return;
    if (running.get(d.id) === true) return;
    markStarting(d.id);
    paint();
  });
  document.addEventListener("visibilitychange", sync);
  window.addEventListener("pagehide", stop);
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) sync();
  });

  sync();
})();
