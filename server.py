import argparse
import csv
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DEFAULT_PORT = 8765
MAX_BODY = 64 * 1024
MAX_DRAIN = 1024 * 1024
MAX_BUTTONS = 40
MAX_NAME = 40
CONFIG_NAME = "buttons.json"
SLUG_RE = re.compile(r"^[a-z0-9-]+$")
WINDOWS = ("normal", "minimized", "maximized")
IF_RUNNING = ("focus", "new", "nothing")
URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
SHOW_COMMANDS = {"normal": 1, "minimized": 2, "maximized": 3}
SW_RESTORE = 9
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "edit.html": "text/html; charset=utf-8",
    "app.js": "text/javascript",
    "edit.js": "text/javascript",
    "style.css": "text/css",
    "manifest.webmanifest": "application/manifest+json",
}

DEFAULT_BUTTONS = [
    {"id": "steam", "name": "Steam", "target": "steam://open/main",
     "process": "steam.exe", "window": "normal", "ifRunning": "focus"},
    {"id": "discord", "name": "Discord", "target": "discord://",
     "process": "Discord.exe", "window": "normal", "ifRunning": "focus"},
    {"id": "simhub", "name": "SimHub",
     "target": "C:\\Program Files (x86)\\SimHub\\SimHubWPF.exe",
     "process": "SimHubWPF.exe", "window": "normal", "ifRunning": "focus"},
    {"id": "simmotion", "name": "SimMotion", "target": "",
     "process": "", "window": "normal", "ifRunning": "focus"},
]


class ValidationError(Exception):
    pass


def is_loopback(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def config_path(base_dir):
    return os.path.join(base_dir, CONFIG_NAME)


def read_config(base_dir):
    with open(config_path(base_dir), "r", encoding="utf-8") as f:
        return json.load(f)


def write_config(base_dir, config):
    fd, tmp = tempfile.mkstemp(dir=base_dir, prefix=".buttons-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, config_path(base_dir))
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def ensure_config(base_dir):
    if not os.path.exists(config_path(base_dir)):
        write_config(base_dir, {"port": DEFAULT_PORT, "buttons": DEFAULT_BUTTONS})


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "button"


def unique_id(base, used):
    candidate = base
    n = 2
    while candidate in used:
        candidate = "%s-%d" % (base, n)
        n += 1
    return candidate


def derive_process(target):
    if target.lower().endswith(".exe"):
        return re.split(r"[\\/]", target)[-1]
    return ""


def text_field(item, key):
    value = item.get(key, "")
    if not isinstance(value, str):
        raise ValidationError("%s must be a string" % key)
    return value.strip()


def choice_field(item, key, allowed):
    value = item.get(key)
    if value not in allowed:
        raise ValidationError("%s must be one of: %s" % (key, ", ".join(allowed)))
    return value


def validate_buttons(buttons):
    if not isinstance(buttons, list):
        raise ValidationError("buttons must be a list")
    if len(buttons) > MAX_BUTTONS:
        raise ValidationError("too many buttons (max %d)" % MAX_BUTTONS)
    used = set()
    for item in buttons:
        if isinstance(item, dict) and item.get("id"):
            bid = item["id"]
            if not isinstance(bid, str) or not SLUG_RE.match(bid):
                raise ValidationError("invalid id: use a-z, 0-9 and -")
            if bid in used:
                raise ValidationError("duplicate id: %s" % bid)
            used.add(bid)
    result = []
    for item in buttons:
        if not isinstance(item, dict):
            raise ValidationError("each button must be an object")
        name = text_field(item, "name")
        if not name:
            raise ValidationError("name must not be empty")
        if len(name) > MAX_NAME:
            raise ValidationError("name too long (max %d)" % MAX_NAME)
        bid = item.get("id")
        if not bid:
            bid = unique_id(slugify(name), used)
            used.add(bid)
        target = text_field(item, "target")
        process = text_field(item, "process") or derive_process(target)
        result.append({
            "id": bid,
            "name": name,
            "target": target,
            "process": process,
            "window": choice_field(item, "window", WINDOWS),
            "ifRunning": choice_field(item, "ifRunning", IF_RUNNING),
        })
    return result


def parse_tasklist(text):
    result = []
    for row in csv.reader(text.splitlines()):
        if len(row) < 2:
            continue
        try:
            pid = int(row[1])
        except ValueError:
            continue
        result.append((row[0].lower(), pid))
    return result


def is_uri(target):
    return URI_RE.match(target) is not None


def show_command(window):
    return SHOW_COMMANDS.get(window, 1)


class DryRunPlatform:
    def __init__(self):
        self.launched = []

    def is_running(self, process):
        return False

    def focus(self, process):
        return False

    def launch(self, target, window):
        self.launched.append((target, window))


class WindowsPlatform:
    def pids(self, process):
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                text=True, errors="replace", creationflags=CREATE_NO_WINDOW)
        wanted = process.lower()
        return {pid for name, pid in parse_tasklist(result.stdout) if name == wanted}

    def is_running(self, process):
        return bool(self.pids(process))

    def find_window(self, user32, pids):
        import ctypes
        from ctypes import wintypes
        found = []
        pid = wintypes.DWORD()

        def visit(hwnd, lparam):
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pids:
                    found.append(hwnd)
                    return False
            return True

        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.EnumWindows(callback_type(visit), 0)
        return found[0] if found else None

    def focus(self, process):
        import ctypes
        from ctypes import wintypes
        pids = self.pids(process)
        if not pids:
            return False
        user32 = ctypes.WinDLL("user32")
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD,
                                       ctypes.c_size_t]
        user32.keybd_event.restype = None
        hwnd = self.find_window(user32, pids)
        if hwnd is None:
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.keybd_event(0x12, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)
        return True

    def launch(self, target, window):
        if is_uri(target) or not target.lower().endswith(".exe"):
            os.startfile(target)
            return
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = show_command(window)
        subprocess.Popen([target], cwd=os.path.dirname(target) or None, startupinfo=startup,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def default_platform():
    if sys.platform == "win32":
        return WindowsPlatform()
    return DryRunPlatform()


def launch_result(button, platform):
    name = button["name"]
    try:
        platform.launch(button.get("target", ""), button.get("window", "normal"))
    except OSError as e:
        return {"ok": False, "action": "error",
                "message": "Could not start %s: %s" % (name, e)}
    return {"ok": True, "action": "launched", "message": "Launched " + name}


def press_button(button, platform):
    name = button["name"]
    if not button.get("target"):
        return {"ok": False, "action": "error", "message": "%s is not configured" % name}
    process = button.get("process", "")
    try:
        if process and platform.is_running(process):
            mode = button.get("ifRunning", "focus")
            if mode == "nothing":
                return {"ok": True, "action": "skipped",
                        "message": "%s is already running" % name}
            if mode == "focus" and platform.focus(process):
                return {"ok": True, "action": "focused", "message": "Focused " + name}
    except OSError as e:
        return {"ok": False, "action": "error",
                "message": "Could not check %s: %s" % (name, e)}
    return launch_result(button, platform)


class DeckServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, base_dir, loopback_check, platform):
        super().__init__(address, handler)
        self.base_dir = base_dir
        self.loopback_check = loopback_check
        self.platform = platform
        self.config_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_body(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_body(status, "application/json; charset=utf-8", body)

    def send_error_json(self, status, message):
        self.send_json(status, {"ok": False, "message": message})

    def is_local(self):
        return self.server.loopback_check(self.client_address[0])

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/buttons":
            self.get_buttons()
        elif path == "/":
            self.send_static("index.html")
        elif path == "/edit":
            if self.is_local():
                self.send_static("edit.html")
            else:
                self.send_error_json(403, "forbidden")
        else:
            self.send_static(path.lstrip("/"))

    def do_PUT(self):
        if urlsplit(self.path).path != "/api/buttons":
            self.send_error_json(404, "not found")
        elif not self.is_local():
            self.send_error_json(403, "forbidden")
        else:
            self.put_buttons()

    def do_POST(self):
        path = urlsplit(self.path).path
        prefix = "/api/press/"
        if path.startswith(prefix):
            self.press(path[len(prefix):])
        else:
            self.send_error_json(404, "not found")

    def send_static(self, name):
        content_type = STATIC_TYPES.get(name)
        file_path = os.path.join(self.server.base_dir, name)
        if content_type is None or not os.path.isfile(file_path):
            self.send_error_json(404, "not found")
            return
        with open(file_path, "rb") as f:
            self.send_body(200, content_type, f.read())

    def get_buttons(self):
        try:
            config = read_config(self.server.base_dir)
        except (OSError, ValueError):
            self.send_error_json(500, "cannot read " + CONFIG_NAME)
            return
        self.send_json(200, {"buttons": config.get("buttons", [])})

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ValidationError("Content-Length required")
        if length < 0:
            raise ValidationError("Content-Length required")
        if length > MAX_BODY:
            self.close_connection = True
            if length <= MAX_DRAIN:
                self.rfile.read(length)
            raise ValidationError("body too large (max %d bytes)" % MAX_BODY)
        return self.rfile.read(length)

    def put_buttons(self):
        try:
            raw = self.read_body()
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                raise ValidationError("body is not valid JSON")
            if not isinstance(data, dict) or "buttons" not in data:
                raise ValidationError("body must be an object with buttons")
            buttons = validate_buttons(data["buttons"])
        except ValidationError as e:
            self.send_error_json(400, str(e))
            return
        base_dir = self.server.base_dir
        try:
            with self.server.config_lock:
                try:
                    port = read_config(base_dir).get("port", DEFAULT_PORT)
                except (OSError, ValueError):
                    port = DEFAULT_PORT
                write_config(base_dir, {"port": port, "buttons": buttons})
        except OSError:
            self.send_error_json(500, "cannot write " + CONFIG_NAME)
            return
        self.send_json(200, {"ok": True, "buttons": buttons})

    def press(self, button_id):
        try:
            buttons = read_config(self.server.base_dir).get("buttons", [])
        except (OSError, ValueError):
            self.send_error_json(500, "cannot read " + CONFIG_NAME)
            return
        for button in buttons:
            if button.get("id") == button_id:
                self.send_json(200, press_button(button, self.server.platform))
                return
        self.send_json(404, {"ok": False, "action": "error", "message": "unknown button"})


def create_server(base_dir, host="127.0.0.1", port=0, loopback_check=is_loopback, platform=None):
    ensure_config(base_dir)
    if platform is None:
        platform = default_platform()
    return DeckServer((host, port), Handler, base_dir, loopback_check, platform)


def main():
    parser = argparse.ArgumentParser(description="Stream Deck server")
    parser.add_argument("--port", type=int, help="port to listen on (default: from buttons.json)")
    args = parser.parse_args()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    ensure_config(base_dir)
    port = args.port or read_config(base_dir).get("port", DEFAULT_PORT)
    server = create_server(base_dir, "0.0.0.0", port)
    print("Stream Deck running")
    print("  PC:    http://localhost:%d" % port)
    print("  Phone: http://%s:%d" % (lan_ip(), port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
