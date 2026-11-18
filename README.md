# Stream Deck

A phone "Stream Deck". A tiny Python server on your Windows PC serves a dark page to your iPhone on the same Wi-Fi. Tap a tile to launch or focus a program on the PC (Steam, Discord, SimHub, SimMotion by default).

## Requirements
- Windows PC with Python 3.8+ (standard library only, no pip installs).
- iPhone on the same Wi-Fi as the PC.

## Run
Double-click `start.bat`. It prints two URLs:

```
Stream Deck running
  PC:    http://localhost:8765
  Phone: http://<PC-IP>:8765
```

Use `--port` to pick another port (`start.bat --port 9000`). The default port is stored in `buttons.json` (`"port": 8765`).

## Firewall
The first start triggers a Windows Firewall prompt. Allow it on **Private** networks. The PC's Wi-Fi network must be set to Private, otherwise the phone cannot connect.

Alternatively, run this once in an administrator command prompt:

```
netsh advfirewall firewall add rule name="Stream Deck" dir=in action=allow protocol=TCP localport=8765 profile=private
```

Change `localport` if you use another port.

## Open on the iPhone
Open the Phone URL in Safari. For a full-screen app, tap Share, then Add to Home Screen.

## Trackpad and keyboard
The phone page has two tabs at the bottom (left side in landscape): **Deck** and **Trackpad**. The Trackpad tab turns the phone into a mouse and keyboard for the PC. The page remembers the last tab per phone and starts on Deck on a fresh load. The connection to the PC is only open while the Trackpad tab is shown and the page is visible. The chip at the top left shows "Connecting...", "Connected" or "Offline".

- **Move**: drag one finger on the pad.
- **Left click**: tap with one finger.
- **Right click**: tap with two fingers.
- **Scroll**: drag two fingers up or down (natural direction: fingers up scrolls down) or sideways.
- **L / R buttons** (mouse icons, left half and right half highlighted): press and hold for click-and-drag, e.g. hold L and drag a finger on the pad. They are released when you leave the tab, lock the phone or lose the connection.
- **Keyboard icon**: opens the phone's system keyboard. What you type is sent to the PC (letters, digits, umlauts, symbols, Backspace, Enter). The typed text is never shown on the phone; the chip shows "Keyboard on · N keys sent" instead. Tap the icon again to close the keyboard.

## Configure buttons
Open http://localhost:8765/edit **on the PC**. The editor and saving only work from the PC itself. The deck has 8 slots, shown as "Slot 1" to "Slot 8". Empty slots appear as blank tiles on the phone. Use "Add button" on an empty slot to fill it, "Delete" to empty a slot (the other slots stay where they are), and "Move up" / "Move down" to swap a slot with its neighbour. Nothing is saved until you press Save. Fields:

- **Name**: label under the logo.
- **Target**: an `.exe` path or a URI such as `steam://open/main` or `discord://`.
- **Process**: image name used to check whether the app already runs. Derived automatically from an `.exe` path; set it manually for URI targets (for example `steam.exe`, `Discord.exe`).
- **Window**: normal / minimized / maximized. Applies to `.exe` targets only, ignored for URIs.
- **If already running**: Focus window / Start another / Do nothing.

Defaults ship for Steam, Discord, SimHub (`C:\Program Files (x86)\SimHub\SimHubWPF.exe`, adjust if yours is different) and SimMotion (no path yet, set it in the editor).

## Logos
Logos are extracted automatically from the installed exe. URI targets are resolved through the registry handler of the URI scheme. To use another image, put `logos/<id>.png` or `logos/<id>.svg` next to `server.py`; the id is shown in the editor. Extracted icons are cached in `cache/`, keyed by the target.

## Security
There is no login, by design. Anyone on your Wi-Fi can tap the buttons, and with the trackpad **any device on the same Wi-Fi can move the mouse and type on the PC**. Keep the PC on a network you trust. The Origin check on the trackpad connection only stops other websites from reaching it; it does not stop another device that opens the page. Editing and saving are refused for any non-local client.

Windows ignores injected mouse and keyboard input for elevated (administrator) windows, for example a program started as administrator or the UAC prompt. To control those, start the server as administrator (right-click `start.bat`, Run as administrator).

## How "focus" works and its limits
If the app's process is running, the server looks for its visible window and brings it to the front (restoring it if minimized). Steam and Discord are tray apps and often have no visible window; if none is found, the target is started again, which raises the app.

## Tests
```
python3 -m unittest discover tests
node tests/ui_smoke.js
node tests/ui_edit.js
node tests/ui_pad.js
```

The three `.js` tests need Node and the `playwright` package with Chromium. They run a server on a free port; `ui_edit.js` works on a temp copy.

## File layout
```
server.py        server (standard library only)
start.bat        Windows launcher
buttons.json     port and buttons
index.html  app.js  trackpad.js  style.css  manifest.webmanifest   phone page
edit.html  edit.js                                    PC-only editor
logos/           optional logo overrides
cache/           extracted icons (git-ignored)
tests/           unit tests, ui_smoke.js, ui_edit.js, ui_pad.js
```

## Windows verification checklist (never run on Windows yet)
Everything was built and tested on Linux. The Windows-only code paths have not been run.

- [ ] `start.bat` starts the server and prints both URLs
- [ ] The iPhone can open the Phone URL (firewall)
- [ ] Steam tile starts/raises Steam
- [ ] Discord tile starts/raises Discord (tray fallback)
- [ ] SimHub tile starts SimHub, a second tap focuses/restores it, including when minimized
- [ ] Window = minimized/maximized is honoured for an `.exe` target
- [ ] Icons are real logos and not the coloured fallback squares for Steam, Discord (the registry handler may be `Update.exe`), SimHub, SimMotion; check edges for dark halos/transparency
- [ ] `tasklist` decoding with a non-ASCII process name does not error
- [ ] Editor saves and the phone page shows the change after reopening
- [ ] Add to Home Screen launches full screen with the deck icon
- [ ] Trackpad: the pointer moves with a sensible speed (tunable with `GAIN` in `trackpad.js`)
- [ ] Trackpad: one-finger tap clicks, two-finger tap right-clicks
- [ ] Trackpad: two-finger scroll goes in the right direction at a sensible speed (tunable with `WHEEL_STEP` in `server.py`)
- [ ] Trackpad: hold L and drag a finger selects text or drags a window
- [ ] Keyboard: letters, digits, umlauts and symbols arrive correctly in Notepad
- [ ] Keyboard: Backspace and Enter work, also on an empty field
- [ ] Keyboard: the system keyboard opens and closes with the keyboard icon on the iPhone and on an Android phone
- [ ] Buttons held on the phone are released on the PC when the phone disconnects (lock the phone or switch to the Deck tab while holding L)
- [ ] Input into an elevated (administrator) window only works when the server runs as administrator
