# Stream-Deck: Spec (v1 draft)

A minimal Stream Deck for the iPhone. A tiny Python server on a Windows PC serves an HTML page. Tapping a tile launches or focuses a program on the PC.

## Goals
- Very simple, effective and efficient.
- No build step, no pip installs, no dependencies.
- A deck of 8 configurable slots, by default **Steam, Discord, SimHub, SimMotion** (racing setup), each with its original logo, plus a **Trackpad tab** that turns the phone into a mouse and keyboard for the PC.

## Non-goals (v1)
Multiple pages or folders, hotkey or macro actions, auto-start with Windows, a firewall or QR helper, run-as-admin, remote access outside the home network, login or PIN. The trackpad and keyboard are in scope (see "Trackpad tab"), still without any login: any device on the Wi-Fi can control the PC.

## Architecture
| Part | Decision |
|---|---|
| Server | Python 3, **standard library only**, single file `server.py` (`http.server`). Started by `start.bat`. |
| Client | Static `index.html` + vanilla JS + CSS. No framework, no bundler. |
| Network | Home Wi-Fi only. iPhone opens `http://<PC-IP>:<PORT>` in Safari. No auth. |
| Config | `buttons.json` next to the server. Edited through the PC-side editor. Read on every page load. |
| Status | `ws(s)://<host>/ws/status` pushes which buttons' processes are running (see "Status dots"). Demand-driven: the open connection is the visible Deck tab. |

## Buttons
The deck has exactly **8 slots**. `buttons.json` holds `{"port": 8765, "buttons": [...]}` where `buttons` is the slot list in order: each entry is a button object or `null` (empty slot). The server always serves and stores 8 entries: a shorter list is padded with `null`, a longer one is cut to the first 8.

Each button object in `buttons.json`:

| Field | Meaning |
|---|---|
| `name` | Label under the logo |
| `target` | `.exe` path **or** URI (`steam://open/main`, `discord://`) |
| `process` | Process name used for the "already running" check. Derived from the exe path, and set manually for URI targets. |
| `window` | `normal` / `minimized` / `maximized` on launch |
| `ifRunning` | `focus` / `launch another` / `nothing` |
| `icon` | Auto-extracted from the exe. Overridden if a file exists in `logos/<id>.png` or `.svg`. |

Defaults:
- Steam: `steam://open/main`.
- Discord: `discord://`. This avoids the versioned `app-x.y.z` folder and the `Update.exe --processStart` arguments.
- SimHub and SimMotion: exe paths, set in the editor.
- Slots 1 to 4 hold these four buttons, slots 5 to 8 are empty.

## Editor
- Served at `http://localhost:<PORT>/edit` **on the PC only**. The server refuses all write endpoints from non-loopback addresses.
- Shows the 8 slots as cards "Slot 1" to "Slot 8". Edit the fields of a filled slot as in the table above. An empty slot has an "Add button" action that turns it into a new blank button.
- Move up / Move down swap a slot with its neighbour (empty slots included). Delete empties the slot in place; the other slots do not shift. Nothing is saved until Save, which sends all 8 entries (`null` for empty slots).
- A real file picker is not possible in a browser, so the exe path is typed or pasted. Not included in v1.
- Because writes are localhost-only, the "no protection" choice stays safe in practice: the phone can only tap.

## Phone UI
- Dark theme, 8 tiles in a fixed grid that fills the screen without scrolling: 2 columns x 4 rows in portrait, 4 columns x 2 rows in landscape. Empty slots show as dim dashed blank tiles that do nothing.
- Installable: `apple-mobile-web-app-capable`, `apple-touch-icon`, web manifest, so it runs full screen from the home screen.
- Press feedback: tile flash and a short vibration where iOS allows it (iOS Safari does not support `navigator.vibrate`, so expect the visual flash only), plus a success or error toast from the server's response.
- Running indicator: a status dot on each filled tile (see "Status dots").

## Status dots
`status.js` shows whether each button's process runs. Only buttons with a non-empty `process` are watched.

- Connection: `ws(s)://<host>/ws/status`, opened only while the Deck tab is active and the page is visible, closed otherwise (it listens to `deck:tab`, `visibilitychange`, `pagehide` and `pageshow`). Reconnects after 500 ms, 1 s, 2 s, then every 3 s. The page sends nothing. The trackpad socket is closed when its tab is left, so the two sockets are never open together once a tab switch has settled.
- Demand-driven rule: the open connection is the visible tab. The server runs one watcher thread only while at least one status connection exists; with none it has no thread and no timer, and it takes no process snapshot when no button is watched.
- Cadence: the watcher checks the process list every 2 s (an in-process snapshot, no `tasklist`), and every 0.5 s for 10 s after a successful press (`POST /api/press/<id>`). It sends a message only when something changed.
- State on the phone: a map of id to running. The first message after every (re)connect is the full state and replaces the map; later messages are merged. When the socket closes, all states are cleared (unknown shows no dot). Malformed messages are ignored.
- Dots: running = green dot (`.dot.run`), starting = pulsing amber dot (`.dot.starting`, no animation with `prefers-reduced-motion`), otherwise none. A running tile's `aria-label` is "<name>, running". Starting begins when a press returns `ok` with `action` `launched` for a button that is not running (`app.js` fires `deck:pressed` with `{id, ok, action}`) and ends when the button is running or after 10 s. `app.js` marks each filled tile with `data-id` and fires `deck:rendered` after each render.

| Message (JSON, PC to phone) | Meaning |
|---|---|
| `{"t":"status","running":{"<id>":bool,...}}` | first message: every watched id. Later messages: only ids whose state changed |

## Trackpad tab
The phone page has two tabs: **Deck** and **Trackpad** (tab bar at the bottom in portrait, left rail in landscape). The first tab on a fresh load is Deck; the last used tab is kept per phone in `localStorage` (`deck-tab`), and the page works without storage. Tab switching is in `app.js` and fires `deck:tab` (`detail.tab` = `deck` or `pad`) on `document`; `trackpad.js` listens to it.

- Header: a status chip on the left ("Connecting..." amber, "Connected" green, "Offline" red; "Keyboard on · N keys sent" while the keyboard is open) and an icon-only keyboard button on the right. Typed text is never displayed.
- Pad: one finger moves the cursor (delta x 1.6, fractional remainder carried, sent at most once per animation frame). Tap (up within 250 ms, under 10 px of movement) = left click. A second finger enters two-finger mode: tap (all fingers up within 350 ms, none moved over 12 px) = right click; moving = scroll (average of both fingers, natural direction). After a two-finger gesture the remaining finger does not move the cursor until all fingers are up.
- L and R: icon-only mouse buttons (left half and right half highlighted). Press sends button down, release sends button up; they work together with a finger on the pad, so hold-and-drag works. They are released when the tab is left, the page is hidden or the connection closes.
- Keyboard button: toggles focus on a hidden 16 px input so the system keyboard opens or closes. Text, Backspace and Enter are sent as they are typed.
- Connection: `ws(s)://<host>/ws`, opened only while the Trackpad tab is active and the page is visible, closed otherwise. Reconnects after 500 ms, 1 s, 2 s, then every 3 s. Messages are dropped while the socket is not open (no queue). The server releases any held button when a connection ends.

| Message (JSON, phone to PC) | Meaning |
|---|---|
| `{"t":"move","dx":int,"dy":int}` | relative cursor move, each in [-500, 500] |
| `{"t":"btn","b":"left"\|"right","d":bool}` | button down (`true`) or up (`false`) |
| `{"t":"click","b":"left"\|"right"}` | button down and up |
| `{"t":"scroll","dx":int,"dy":int}` | wheel, each in [-400, 400]; `dy` > 0 scrolls down |
| `{"t":"text","s":str}` | type text, at most 64 characters per message |
| `{"t":"key","k":"backspace"\|"enter"}` | press a key |

The server never replies on this socket. Windows ignores injected input for elevated windows unless the server runs as administrator. Scroll speed is tunable with `WHEEL_STEP` in `server.py`, cursor speed with `GAIN` in `trackpad.js`.

## Launch behavior (Windows)
1. `POST /api/press/<id>` runs the button's action.
2. Process running and `ifRunning = focus`: bring its window to the front with Win32 through `ctypes`. Restore it first if minimized.
3. Not running, or `launch another`: start it via `os.startfile` for URIs, or `subprocess` with `STARTUPINFO.wShowWindow` for the window state.
4. `nothing`: return a "skipped" result.
5. The response is `{ok, message}` for the toast.

Known caveat: Steam and Discord live in the tray and keep no normal window. "Focus" can fail there, so the fallback is to re-run the URI or exe, which raises the existing window.

## Icons / logos
- The server extracts the icon from each target exe through `ctypes`: `PrivateExtractIconsW` at 256 px, falling back to `ExtractIconExW`. URI targets resolve the exe via the registry (`HKCR\<scheme>\shell\open\command`). The result is converted to PNG with a stdlib encoder and cached in `cache/`, keyed by id and a hash of the target.
- URI-only buttons need `process` or an exe path to find an icon. Otherwise they use `logos/<id>.png`.
- Risk: exe icons are often only 32 to 256 px and lower quality than official brand SVGs. `logos/` override files are the escape hatch.

## File layout
```
server.py
start.bat
buttons.json
index.html   edit.html   app.js   trackpad.js   status.js   edit.js   style.css   manifest.webmanifest
logos/       (optional overrides)
cache/       (extracted icons, git-ignored)
tests/       (unit tests incl. test_ws.py, test_status.py, ui_smoke.js, ui_edit.js, ui_pad.js, ui_status.js, ui_status_server.py)
.gitignore
README.md    (setup: install Python, run start.bat, open firewall port, add to home screen)
```

## Open items for the build
- Windows firewall: no helper script in v1, so README documents the one-line `netsh` rule or the first-run allow prompt.
- SimMotion exe path and the exact SimHub path (needed for defaults and icon extraction).
- Pick a port (default `8765`).
- Testing: the sandbox is Linux, so Win32 pieces (focus, icon extraction) are stubbed and must be verified on the PC.
- Status: v1 built and verified on Linux only; see the Windows verification checklist in README.md.
