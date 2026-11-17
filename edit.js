(function () {
  "use strict";

  var list = document.getElementById("list");
  var addButton = document.getElementById("add");
  var saveButton = document.getElementById("save");
  var dirtyNote = document.getElementById("dirty");
  var status = document.getElementById("status");

  var WINDOWS = [["normal", "Normal"], ["minimized", "Minimized"], ["maximized", "Maximized"]];
  var IF_RUNNING = [["focus", "Focus window"], ["new", "Start another"], ["nothing", "Do nothing"]];

  var buttons = [];
  var savedSnapshot = "[]";
  var iconStamp = Date.now();
  var nextUid = 1;
  var loaded = false;
  var saving = false;

  function pick(value, options, fallback) {
    for (var i = 0; i < options.length; i++) {
      if (options[i][0] === value) return value;
    }
    return fallback;
  }

  function fromServer(item) {
    item = item || {};
    return {
      uid: nextUid++,
      id: typeof item.id === "string" ? item.id : "",
      name: typeof item.name === "string" ? item.name : "",
      target: typeof item.target === "string" ? item.target : "",
      process: typeof item.process === "string" ? item.process : "",
      window: pick(item.window, WINDOWS, "normal"),
      ifRunning: pick(item.ifRunning, IF_RUNNING, "focus")
    };
  }

  function payloadOf(button) {
    var out = {};
    if (button.id) out.id = button.id;
    out.name = button.name;
    out.target = button.target;
    out.process = button.process;
    out.window = button.window;
    out.ifRunning = button.ifRunning;
    return out;
  }

  function snapshot() {
    return JSON.stringify(buttons.map(payloadOf));
  }

  function isDirty() {
    return loaded && snapshot() !== savedSnapshot;
  }

  function setStatus(text, kind) {
    status.textContent = text;
    status.className = kind || "";
  }

  function refreshDirty() {
    dirtyNote.hidden = !isDirty();
  }

  function touch() {
    if (status.className === "ok") setStatus("", "");
    refreshDirty();
  }

  function setButtons(items) {
    buttons = items.map(fromServer);
    savedSnapshot = snapshot();
    iconStamp = Date.now();
  }

  function makeField(card, key, labelText, control, wide, help) {
    var field = document.createElement("div");
    field.className = "field" + (wide ? " wide" : "");
    var inputId = key + "-" + card.uid;
    var label = document.createElement("label");
    label.htmlFor = inputId;
    label.textContent = labelText;
    control.id = inputId;
    control.classList.add("f-" + key.toLowerCase());
    field.append(label, control);
    if (help) {
      var note = document.createElement("span");
      note.className = "help muted";
      note.id = inputId + "-help";
      note.textContent = help;
      control.setAttribute("aria-describedby", note.id);
      field.append(note);
    }
    return field;
  }

  function makeInput(button, key, attrs) {
    var input = document.createElement("input");
    input.type = "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    for (var name in attrs) input.setAttribute(name, attrs[name]);
    input.value = button[key];
    input.addEventListener("input", function () {
      button[key] = input.value;
      touch();
    });
    return input;
  }

  function makeSelect(button, key, options) {
    var select = document.createElement("select");
    options.forEach(function (option) {
      var el = document.createElement("option");
      el.value = option[0];
      el.textContent = option[1];
      select.append(el);
    });
    select.value = button[key];
    select.addEventListener("change", function () {
      button[key] = select.value;
      touch();
    });
    return select;
  }

  function makeAction(text, className, onClick, disabled) {
    var el = document.createElement("button");
    el.type = "button";
    el.className = "btn " + className;
    el.textContent = text;
    el.disabled = disabled;
    el.addEventListener("click", onClick);
    return el;
  }

  function makeCard(button, index) {
    var card = document.createElement("li");
    card.className = "card";

    var side = document.createElement("div");
    side.className = "side";
    var icon;
    if (button.id) {
      icon = document.createElement("img");
      icon.src = "/api/icon/" + encodeURIComponent(button.id) + "?t=" + iconStamp;
    } else {
      icon = document.createElement("div");
      icon.classList.add("icon-ph");
    }
    icon.classList.add("icon");
    icon.setAttribute("aria-hidden", "true");
    if (button.id) icon.alt = "";
    side.append(icon);
    if (button.id) {
      var idLine = document.createElement("div");
      idLine.className = "id muted";
      idLine.title = "Logo: drop logos/" + button.id + ".png to override";
      var code = document.createElement("code");
      code.textContent = button.id;
      idLine.append(code);
      side.append(idLine);
    }

    var fields = document.createElement("div");
    fields.className = "fields";
    fields.append(
      makeField(button, "name", "Name",
        makeInput(button, "name", { maxlength: "40" }), true),
      makeField(button, "target", "Target",
        makeInput(button, "target", {
          placeholder: "C:\\Program Files\\App\\App.exe or steam://open/main"
        }), true, "Program path or URI"),
      makeField(button, "process", "Process",
        makeInput(button, "process", { placeholder: "auto from .exe path" }), true,
        "Process name used to check if the app is running; needed for URI targets"),
      makeField(button, "window", "Window", makeSelect(button, "window", WINDOWS), false),
      makeField(button, "ifRunning", "If already running",
        makeSelect(button, "ifRunning", IF_RUNNING), false)
    );

    var actions = document.createElement("div");
    actions.className = "actions";
    var up = makeAction("Move up", "btn-up", function () { move(button, -1, "btn-up"); }, index === 0);
    var down = makeAction("Move down", "btn-down", function () { move(button, 1, "btn-down"); },
      index === buttons.length - 1);
    var del = makeAction("Delete", "btn-delete danger", function () { remove(button); }, false);
    var label = button.name || "button " + (index + 1);
    up.setAttribute("aria-label", "Move " + label + " up");
    down.setAttribute("aria-label", "Move " + label + " down");
    del.setAttribute("aria-label", "Delete " + label);
    actions.append(up, down, del);

    card.append(side, fields, actions);
    return card;
  }

  function render() {
    if (!buttons.length) {
      var empty = document.createElement("li");
      empty.className = "empty muted";
      empty.textContent = loaded ? "No buttons yet. Use Add button to create one." : "Loading…";
      list.replaceChildren(empty);
    } else {
      list.replaceChildren.apply(list, buttons.map(makeCard));
    }
    refreshDirty();
  }

  function focusIn(button, selector) {
    var index = buttons.indexOf(button);
    var card = list.children[index];
    var target = card && card.querySelector(selector);
    if (target && target.disabled) target = null;
    if (target) target.focus();
    return !!target;
  }

  function move(button, delta, selector) {
    var from = buttons.indexOf(button);
    var to = from + delta;
    if (to < 0 || to >= buttons.length) return;
    buttons.splice(from, 1);
    buttons.splice(to, 0, button);
    render();
    touch();
    if (!focusIn(button, "." + selector)) focusIn(button, delta < 0 ? ".btn-down" : ".btn-up");
  }

  function remove(button) {
    var index = buttons.indexOf(button);
    buttons.splice(index, 1);
    render();
    touch();
    var next = buttons[index] || buttons[index - 1];
    if (next) focusIn(next, ".btn-delete");
    else addButton.focus();
  }

  function add() {
    if (!loaded || saving) return;
    var button = fromServer();
    buttons.push(button);
    render();
    touch();
    focusIn(button, ".f-name");
  }

  function save() {
    if (!loaded || saving) return;
    saving = true;
    list.inert = true;
    setStatus("", "");
    var failed = function (message) {
      setStatus(message, "err");
    };
    fetch("/api/buttons", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ buttons: buttons.map(payloadOf) })
    })
      .then(function (res) {
        return res.json().catch(function () { return null; }).then(function (data) {
          if (res.status === 403) {
            failed("Editing is only possible on the PC itself (localhost).");
          } else if (res.ok && data && data.ok === true && Array.isArray(data.buttons)) {
            setButtons(data.buttons);
            render();
            setStatus("Saved", "ok");
          } else {
            failed(data && data.message ? String(data.message) : "Save failed (HTTP " + res.status + ")");
          }
        });
      })
      .catch(function () {
        failed("Can't reach the server");
      })
      .then(function () {
        saving = false;
        list.inert = false;
        refreshDirty();
      });
  }

  function load() {
    fetch("/api/buttons", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("bad status");
        return res.json();
      })
      .then(function (data) {
        setButtons(Array.isArray(data.buttons) ? data.buttons : []);
        loaded = true;
        addButton.disabled = false;
        saveButton.disabled = false;
        render();
      })
      .catch(function () {
        list.replaceChildren();
        setStatus("Can't reach the server", "err");
      });
  }

  addButton.addEventListener("click", add);
  saveButton.addEventListener("click", save);
  window.addEventListener("beforeunload", function (event) {
    if (!isDirty()) return;
    event.preventDefault();
    event.returnValue = "";
  });
  render();
  load();
})();
