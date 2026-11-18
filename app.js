(function () {
  "use strict";

  var SLOTS = 8;
  var grid = document.getElementById("grid");
  var toast = document.getElementById("toast");
  var toastTimer = null;
  var loadToken = 0;

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

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") load();
  });
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) load();
  });
  load();
})();
