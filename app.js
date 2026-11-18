(function () {
  "use strict";

  var SLOTS = 8;
  var grid = document.getElementById("grid");
  var toast = document.getElementById("toast");
  var toastTimer = null;
  var loadToken = 0;
  var TAB_KEY = "deck-tab";
  var TABS = ["deck", "pad"];
  var tabButtons = TABS.map(function (name) { return document.getElementById("tab-" + name); });
  var tabPanels = TABS.map(function (name) { return document.getElementById("panel-" + name); });

  function showToast(text, ok) {
    toast.textContent = text;
    toast.className = "show " + (ok ? "ok" : "err");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toast.classList.remove("show");
    }, 2000);
  }

  function showMessage(text) {
    var p = document.createElement("p");
    p.className = "message";
    p.textContent = text;
    grid.replaceChildren(p);
  }

  function press(tile, id) {
    if (tile.dataset.busy) return;
    tile.dataset.busy = "1";
    tile.classList.remove("flash");
    void tile.offsetWidth;
    tile.classList.add("flash");
    if (navigator.vibrate) navigator.vibrate(30);
    fetch("/api/press/" + encodeURIComponent(id), { method: "POST" })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        showToast(String(data.message), data.ok === true);
      })
      .catch(function () {
        showToast("Can't reach the PC", false);
      })
      .then(function () {
        delete tile.dataset.busy;
      });
  }

  function makeTile(button) {
    var tile = document.createElement("button");
    tile.type = "button";
    tile.className = "tile";
    tile.setAttribute("aria-label", button.name);
    var img = document.createElement("img");
    img.src = "/api/icon/" + encodeURIComponent(button.id);
    img.alt = "";
    img.draggable = false;
    var label = document.createElement("span");
    label.className = "label";
    label.textContent = button.name;
    tile.append(img, label);
    tile.addEventListener("animationend", function () {
      tile.classList.remove("flash");
    });
    tile.addEventListener("click", function () {
      press(tile, button.id);
    });
    return tile;
  }

  function makeEmpty() {
    var tile = document.createElement("div");
    tile.className = "tile empty";
    tile.setAttribute("aria-hidden", "true");
    return tile;
  }

  function render(buttons) {
    var tiles = [];
    for (var i = 0; i < SLOTS; i++) {
      var button = buttons[i];
      tiles.push(button && typeof button === "object" ? makeTile(button) : makeEmpty());
    }
    grid.replaceChildren.apply(grid, tiles);
  }

  function load() {
    var token = ++loadToken;
    fetch("/api/buttons", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("bad status");
        return res.json();
      })
      .then(function (data) {
        if (token !== loadToken) return;
        render(Array.isArray(data.buttons) ? data.buttons : []);
      })
      .catch(function () {
        if (token !== loadToken) return;
        showMessage("Can't reach the PC");
      });
  }

  function storedTab() {
    try {
      return localStorage.getItem(TAB_KEY) === "pad" ? "pad" : "deck";
    } catch (e) {
      return "deck";
    }
  }

  function selectTab(name, options) {
    options = options || {};
    TABS.forEach(function (tab, i) {
      var on = tab === name;
      tabButtons[i].setAttribute("aria-selected", on ? "true" : "false");
      tabButtons[i].tabIndex = on ? 0 : -1;
      tabPanels[i].hidden = !on;
      if (on && options.focus) tabButtons[i].focus();
    });
    if (options.save) {
      try {
        localStorage.setItem(TAB_KEY, name);
      } catch (e) { /* no storage, the choice just is not remembered */ }
    }
    document.dispatchEvent(new CustomEvent("deck:tab", { detail: { tab: name } }));
  }

  tabButtons.forEach(function (button, i) {
    button.addEventListener("click", function () {
      selectTab(TABS[i], { save: true });
    });
    button.addEventListener("keydown", function (event) {
      var next = -1;
      if (event.key === "ArrowRight") next = (i + 1) % TABS.length;
      else if (event.key === "ArrowLeft") next = (i + TABS.length - 1) % TABS.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = TABS.length - 1;
      if (next < 0) return;
      event.preventDefault();
      selectTab(TABS[next], { save: true, focus: true });
    });
  });

  if (window.visualViewport) {
    var viewport = window.visualViewport;
    var syncViewport = function () {
      document.documentElement.style.setProperty("--app-h", viewport.height + "px");
      if (window.scrollY) window.scrollTo(0, 0);
    };
    viewport.addEventListener("resize", syncViewport);
    viewport.addEventListener("scroll", syncViewport);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") load();
  });
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) load();
  });
  selectTab(storedTab());
  load();
})();
