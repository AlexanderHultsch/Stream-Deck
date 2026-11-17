import argparse
import csv
import hashlib
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import zlib
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
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FALLBACK_SIZE = 128

STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "edit.html": "text/html; charset=utf-8",
    "app.js": "text/javascript",
    "edit.js": "text/javascript",
    "style.css": "text/css",
    "manifest.webmanifest": "application/manifest+json",
}

DECK_ICON_SIZES = {
    "/apple-touch-icon.png": 180,
    "/icon-192.png": 192,
    "/icon-512.png": 512,
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


def png_chunk(kind, data):
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def encode_png(width, height, rgba):
    if len(rgba) != width * height * 4:
        raise ValueError("expected %d bytes of RGBA data" % (width * height * 4))
    stride = width * 4
    raw = b"".join(b"\x00" + bytes(rgba[y * stride:(y + 1) * stride]) for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (PNG_SIGNATURE + png_chunk(b"IHDR", header)
            + png_chunk(b"IDAT", zlib.compress(raw)) + png_chunk(b"IEND", b""))


def bgra_to_rgba(buf):
    out = bytearray(buf)
    out[0::4] = buf[2::4]
    out[2::4] = buf[0::4]
    return bytes(out)


def apply_mask_alpha(rgba, mask):
    out = bytearray(rgba)
    out[3::4] = bytes(0 if m else 255 for m in mask)
    return bytes(out)


def parse_command_exe(command):
    command = command.strip()
    if '"' in command:
        start = command.index('"') + 1
        end = command.find('"', start)
        path = command[start:end] if end != -1 else ""
    else:
        end = command.lower().find(".exe")
        path = command[:end + 4] if end != -1 else ""
    path = path.strip()
    return path if path.lower().endswith(".exe") else None


def fallback_png(button_id):
    digest = hashlib.sha1(button_id.encode("utf-8")).digest()
    pixel = bytes(60 + digest[i] % 141 for i in range(3)) + b"\xff"
    return encode_png(FALLBACK_SIZE, FALLBACK_SIZE, pixel * (FALLBACK_SIZE * FALLBACK_SIZE))


def deck_icon_png(size):
    background = (0x0b, 0x0d, 0x10, 0xff)
    colours = ((0x1e, 0x90, 0xff, 0xff), (0x2e, 0xcc, 0x71, 0xff),
               (0xff, 0x9f, 0x1a, 0xff), (0xe7, 0x4c, 0x3c, 0xff))
    margin = int(size * 0.18)
    gap = int(size * 0.08)
    cell = (size - 2 * margin - gap) // 2
    starts = (margin, margin + cell + gap)
    rows = []
    for y in range(size):
        row_index = [i for i, s in enumerate(starts) if s <= y < s + cell]
        row = bytearray(background * size)
        if row_index:
            for col, start in enumerate(starts):
                colour = colours[row_index[0] * 2 + col]
                row[start * 4:(start + cell) * 4] = colour * cell
        rows.append(bytes(row))
    return encode_png(size, size, b"".join(rows))


def read_file(path):
    with open(path, "rb") as f:
        return f.read()


def cache_icon(cache_dir, cache_path, data):
    os.makedirs(cache_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".icon-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, cache_path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def icon_response(base_dir, button, extractor):
    bid = button["id"]
    if not isinstance(bid, str) or not SLUG_RE.match(bid):
        return "image/png", fallback_png(str(bid))
    logos = os.path.join(base_dir, "logos")
    for ext, content_type in ((".png", "image/png"), (".svg", "image/svg+xml")):
        logo = os.path.join(logos, bid + ext)
        if os.path.isfile(logo):
            return content_type, read_file(logo)
    target = button.get("target", "")
    cache_dir = os.path.join(base_dir, "cache")
    key = hashlib.sha1(target.encode("utf-8")).hexdigest()[:10]
    cache_path = os.path.join(cache_dir, "%s-%s.png" % (bid, key))
    if os.path.isfile(cache_path):
        return "image/png", read_file(cache_path)
    try:
        extracted = extractor.extract(target)
        data = encode_png(*extracted) if extracted is not None else None
    except Exception:
        data = None
    if data is None:
        return "image/png", fallback_png(bid)
    try:
        cache_icon(cache_dir, cache_path, data)
    except OSError:
        pass
    return "image/png", data


class NullExtractor:
    def extract(self, target):
        return None


class WindowsIconExtractor:
    def extract(self, target):
        import ctypes
        path = self.exe_path(target)
        if path is None:
            return None
        user32, gdi32, shell32 = self.load_dlls()
        hicon = self.load_icon(user32, shell32, path)
        if not hicon:
            return None
        ICONINFO, BITMAP, BITMAPINFOHEADER = self.structures()
        info = ICONINFO()
        hdc = user32.GetDC(None)
        try:
            if not hdc or not user32.GetIconInfo(hicon, ctypes.byref(info)) or not info.hbmColor:
                return None
            bm = BITMAP()
            if not gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm)):
                return None
            width, height = bm.bmWidth, bm.bmHeight
            if width <= 0 or height <= 0:
                return None
            bgra = self.read_bits(gdi32, hdc, info.hbmColor, width, height, BITMAPINFOHEADER)
            if bgra is None:
                return None
            rgba = bgra_to_rgba(bgra)
            if not any(rgba[3::4]) and info.hbmMask:
                bits = self.read_bits(gdi32, hdc, info.hbmMask, width, height, BITMAPINFOHEADER)
                if bits is not None:
                    rgba = apply_mask_alpha(rgba, bits[0::4])
            return width, height, rgba
        finally:
            if info.hbmColor:
                gdi32.DeleteObject(info.hbmColor)
            if info.hbmMask:
                gdi32.DeleteObject(info.hbmMask)
            if hdc:
                user32.ReleaseDC(None, hdc)
            user32.DestroyIcon(hicon)

    def exe_path(self, target):
        if is_uri(target):
            import winreg
            scheme = target.split("://")[0]
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                    scheme + "\\shell\\open\\command") as key:
                    command = winreg.QueryValueEx(key, "")[0]
            except OSError:
                return None
            path = parse_command_exe(command) if isinstance(command, str) else None
        elif target.lower().endswith(".exe"):
            path = target
        else:
            return None
        return path if path and os.path.isfile(path) else None

    def load_dlls(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        gdi32 = ctypes.WinDLL("gdi32")
        shell32 = ctypes.WinDLL("shell32")
        handle_ptr = ctypes.POINTER(wintypes.HANDLE)
        user32.PrivateExtractIconsW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, handle_ptr,
            ctypes.POINTER(wintypes.UINT), wintypes.UINT, wintypes.UINT]
        user32.PrivateExtractIconsW.restype = wintypes.UINT
        shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, handle_ptr,
                                           handle_ptr, wintypes.UINT]
        shell32.ExtractIconExW.restype = wintypes.UINT
        user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        user32.GetIconInfo.restype = wintypes.BOOL
        user32.GetDC.argtypes = [wintypes.HANDLE]
        user32.GetDC.restype = wintypes.HANDLE
        user32.ReleaseDC.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.DestroyIcon.argtypes = [wintypes.HANDLE]
        user32.DestroyIcon.restype = wintypes.BOOL
        gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetObjectW.restype = ctypes.c_int
        gdi32.GetDIBits.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.UINT,
                                    wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.UINT]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.DeleteObject.restype = wintypes.BOOL
        return user32, gdi32, shell32

    def structures(self):
        import ctypes
        from ctypes import wintypes

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                        ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HANDLE),
                        ("hbmColor", wintypes.HANDLE)]

        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                        ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                        ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                        ("bmBits", ctypes.c_void_p)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        return ICONINFO, BITMAP, BITMAPINFOHEADER

    def load_icon(self, user32, shell32, path):
        import ctypes
        from ctypes import wintypes
        hicon = wintypes.HANDLE()
        count = user32.PrivateExtractIconsW(path, 0, 256, 256, ctypes.byref(hicon), None, 1, 0)
        if count not in (0, 0xFFFFFFFF) and hicon.value:
            return hicon.value
        large = wintypes.HANDLE()
        if shell32.ExtractIconExW(path, 0, ctypes.byref(large), None, 1) and large.value:
            return large.value
        return None

    def read_bits(self, gdi32, hdc, hbitmap, width, height, header_type):
        import ctypes
        header = header_type()
        header.biSize = ctypes.sizeof(header)
        header.biWidth = width
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(hdc, hbitmap, 0, height, buffer, ctypes.byref(header), 0):
            return None
        return buffer.raw


def default_extractor():
    if sys.platform == "win32":
        return WindowsIconExtractor()
    return NullExtractor()


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

    def __init__(self, address, handler, base_dir, loopback_check, platform, extractor):
        super().__init__(address, handler)
        self.base_dir = base_dir
        self.loopback_check = loopback_check
        self.platform = platform
        self.extractor = extractor
        self.config_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_body(self, status, content_type, body, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
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
        prefix = "/api/icon/"
        if path == "/api/buttons":
            self.get_buttons()
        elif path.startswith(prefix):
            self.get_icon(path[len(prefix):])
        elif path in DECK_ICON_SIZES:
            self.send_body(200, "image/png", deck_icon_png(DECK_ICON_SIZES[path]))
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

    def get_icon(self, button_id):
        try:
            buttons = read_config(self.server.base_dir).get("buttons", [])
        except (OSError, ValueError):
            self.send_error_json(500, "cannot read " + CONFIG_NAME)
            return
        for button in buttons:
            if button.get("id") == button_id:
                content_type, body = icon_response(self.server.base_dir, button,
                                                   self.server.extractor)
                self.send_body(200, content_type, body, (("Cache-Control", "no-cache"),))
                return
        self.send_error_json(404, "unknown button")

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


def create_server(base_dir, host="127.0.0.1", port=0, loopback_check=is_loopback, platform=None,
                  extractor=None):
    ensure_config(base_dir)
    if platform is None:
        platform = default_platform()
    if extractor is None:
        extractor = default_extractor()
    return DeckServer((host, port), Handler, base_dir, loopback_check, platform, extractor)


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
