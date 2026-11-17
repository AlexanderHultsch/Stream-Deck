# Stream-Deck: Spec (v1 draft)

A minimal Stream Deck for the iPhone. A tiny Python server on a Windows PC serves an HTML page. Tapping a tile launches or focuses a program on the PC.

## Goals
- Very simple, effective and efficient.
- No build step, no pip installs, no dependencies.
- Four buttons: **Steam, Discord, SimHub, SimMotion** (racing setup), each with its original logo.

## Non-goals (v1)
Multiple pages or folders, hotkey or macro actions, running-status dots, auto-start with Windows, a firewall or QR helper, run-as-admin, remote access outside the home network, login or PIN.

## Architecture
| Part | Decision |
|---|---|
| Server | Python 3, **standard library only**, single file `server.py` (`http.server`). Started by `start.bat`. |
| Client | Static `index.html` + vanilla JS + CSS. No framework, no bundler. |
| Network | Home Wi-Fi only. iPhone opens `http://<PC-IP>:<PORT>` in Safari. No auth. |
| Config | `buttons.json` next to the server. Edited through the PC-side editor. Read on every page load. |

## Buttons
Each button in `buttons.json`:

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

## Editor
- Served at `http://localhost:<PORT>/edit` **on the PC only**. The server refuses all write endpoints from non-loopback addresses.
- Add, remove, reorder and edit buttons. Fields as in the table above.
- A real file picker is not possible in a browser, so the exe path is typed or pasted. Not included in v1.
- Because writes are localhost-only, the "no protection" choice stays safe in practice: the phone can only tap.

## Phone UI
- Dark theme, big square tiles in a fixed grid. Portrait and landscape. The grid wraps when more buttons are added.
- Installable: `apple-mobile-web-app-capable`, `apple-touch-icon`, web manifest, so it runs full screen from the home screen.
- Press feedback: tile flash and a short vibration where iOS allows it (iOS Safari does not support `navigator.vibrate`, so expect the visual flash only), plus a success or error toast from the server's response.
- No running indicator in v1.

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
index.html   edit.html   app.js   style.css   manifest.webmanifest
logos/       (optional overrides)
cache/       (extracted icons, git-ignored)
tests/       (unit tests, ui_smoke.js, ui_edit.js)
.gitignore
README.md    (setup: install Python, run start.bat, open firewall port, add to home screen)
```

## Open items for the build
- Windows firewall: no helper script in v1, so README documents the one-line `netsh` rule or the first-run allow prompt.
- SimMotion exe path and the exact SimHub path (needed for defaults and icon extraction).
- Pick a port (default `8765`).
- Testing: the sandbox is Linux, so Win32 pieces (focus, icon extraction) are stubbed and must be verified on the PC.
- Status: v1 built and verified on Linux only; see the Windows verification checklist in README.md.
