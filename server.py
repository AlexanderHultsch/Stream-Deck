import argparse
import base64
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
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DEFAULT_PORT = 8765
MAX_BODY = 64 * 1024
MAX_DRAIN = 1024 * 1024
SLOTS = 8
MAX_NAME = 40
CONFIG_NAME = "buttons.json"
SLUG_RE = re.compile(r"^[a-z0-9-]+$")
WINDOWS = ("normal", "minimized", "maximized")
IF_RUNNING = ("focus", "new", "nothing")
URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
SHOW_COMMANDS = {"normal": 1, "minimized": 2, "maximized": 3}
SW_RESTORE = 9
TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FALLBACK_SIZE = 128
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_PAYLOAD = 4096
WS_MAX_CONNECTIONS = 4
MAX_STATUS_CONNECTIONS = 4
WATCH_INTERVAL = 2.0
WATCH_BURST_INTERVAL = 0.5
WATCH_BURST_SECONDS = 10.0
WS_PING_INTERVAL = 15
WS_OP_TEXT = 0x1
WS_OP_CLOSE = 0x8
WS_OP_PING = 0x9
WS_OP_PONG = 0xA
WS_BUTTONS = ("left", "right")
WS_KEYS = ("backspace", "enter")
MAX_MOVE = 500
MAX_SCROLL = 400
MAX_TEXT = 64
WHEEL_STEP = 3
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_BACK = 0x08
VK_RETURN = 0x0D

STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "edit.html": "text/html; charset=utf-8",
    "app.js": "text/javascript",
    "edit.js": "text/javascript",
    "trackpad.js": "text/javascript",
    "status.js": "text/javascript",
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


def pad_slots(items):
    slots = [item if isinstance(item, dict) else None for item in items[:SLOTS]]
    return slots + [None] * (SLOTS - len(slots))


def stored_slots(config):
    items = config.get("buttons", [])
    return pad_slots(items if isinstance(items, list) else [])


def ensure_config(base_dir):
    if not os.path.exists(config_path(base_dir)):
        write_config(base_dir, {"port": DEFAULT_PORT, "buttons": pad_slots(DEFAULT_BUTTONS)})


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
    if len(buttons) > SLOTS:
        raise ValidationError("too many slots (max %d)" % SLOTS)
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
        if item is None:
            result.append(None)
            continue
        if not isinstance(item, dict):
            raise ValidationError("each slot must be an object or null")
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
    return pad_slots(result)


def is_uri(target):
    return URI_RE.match(target) is not None


def show_command(window):
    return SHOW_COMMANDS.get(window, 1)


class DryRunProcessSource:
    def __init__(self):
        self.processes = []

    def snapshot(self):
        return list(self.processes)


class WindowsProcessSource:
    def __init__(self):
        self.api = None
        self.load_lock = threading.Lock()

    def structures(self):
        import ctypes
        dword = ctypes.c_uint32

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", dword), ("cntUsage", dword), ("th32ProcessID", dword),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", dword),
                        ("cntThreads", dword), ("th32ParentProcessID", dword),
                        ("pcPriClassBase", ctypes.c_int32), ("dwFlags", dword),
                        ("szExeFile", ctypes.c_uint16 * MAX_PATH)]

        return PROCESSENTRY32W

    def load(self):
        with self.load_lock:
            if self.api is None:
                import ctypes
                entry_type = self.structures()
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
                kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
                for name in ("Process32FirstW", "Process32NextW"):
                    function = getattr(kernel32, name)
                    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(entry_type)]
                    function.restype = ctypes.c_int32
                kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel32.CloseHandle.restype = ctypes.c_int32
                self.api = (kernel32, entry_type)
            return self.api

    def snapshot(self):
        import ctypes
        kernel32, entry_type = self.load()
        handle = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = entry_type()
            entry.dwSize = ctypes.sizeof(entry_type)
            pointer = ctypes.pointer(entry)
            result = []
            found = kernel32.Process32FirstW(handle, pointer)
            while found:
                name = bytes(entry.szExeFile).decode("utf-16-le", "replace").split("\x00")[0]
                result.append((name.lower(), entry.th32ProcessID))
                found = kernel32.Process32NextW(handle, pointer)
            return result
        finally:
            kernel32.CloseHandle(handle)


def default_process_source():
    if sys.platform == "win32":
        return WindowsProcessSource()
    return DryRunProcessSource()


class ProcessWatcher:
    def __init__(self, source, get_watched, interval=WATCH_INTERVAL,
                 burst_interval=WATCH_BURST_INTERVAL, burst_seconds=WATCH_BURST_SECONDS,
                 clock=time.monotonic):
        self.source = source
        self.get_watched = get_watched
        self.interval = interval
        self.burst_interval = burst_interval
        self.burst_seconds = burst_seconds
        self.clock = clock
        self.lock = threading.Lock()
        self.subscribers = []
        self.state = {}
        self.thread = None
        self.wake = None
        self.burst_until = 0.0

    def compute(self):
        try:
            watched = self.get_watched()
            if not watched:
                return {}
            names = {name for name, pid in self.source.snapshot()}
        except Exception:
            return None
        return {bid: process in names for bid, process in watched.items()}

    def subscribe(self, callback):
        with self.lock:
            if self.thread is None:
                self.state = self.compute() or {}
                self.wake = threading.Event()
                self.thread = threading.Thread(target=self.run, args=(self.wake,), daemon=True)
                self.thread.start()
            self.subscribers.append(callback)
            return dict(self.state)

    def unsubscribe(self, callback):
        with self.lock:
            if callback in self.subscribers:
                self.subscribers.remove(callback)
            if not self.subscribers:
                self.stop_locked()

    def kick(self):
        with self.lock:
            if self.subscribers:
                self.burst_until = self.clock() + self.burst_seconds
                self.wake.set()

    def shutdown(self):
        with self.lock:
            del self.subscribers[:]
            self.stop_locked()

    def stop_locked(self):
        if self.wake is not None:
            self.wake.set()
        self.wake = None
        self.thread = None
        self.burst_until = 0.0

    def run(self, wake):
        last = self.clock()
        while True:
            with self.lock:
                if self.wake is not wake:
                    return
                bursting = self.clock() < self.burst_until
                cadence = self.burst_interval if bursting else self.interval
                wake.clear()
            delay = last + cadence - self.clock()
            if delay > 0 and wake.wait(delay):
                continue
            self.tick(wake)
            last = self.clock()

    def tick(self, wake):
        state = self.compute()
        if state is None:
            return
        with self.lock:
            if self.wake is not wake:
                return
            changes = {bid: running for bid, running in state.items()
                       if self.state.get(bid) != running}
            self.state = state
            callbacks = list(self.subscribers)
        if not changes:
            return
        for callback in callbacks:
            try:
                callback(dict(changes))
            except Exception:
                self.unsubscribe(callback)


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
    def __init__(self, source=None):
        self.source = source if source is not None else default_process_source()

    def pids(self, process):
        wanted = process.lower()
        return {pid for name, pid in self.source.snapshot() if name == wanted}

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


def default_platform(source=None):
    if sys.platform == "win32":
        return WindowsPlatform(source)
    return DryRunPlatform()


class DryRunInput:
    def __init__(self):
        self.events = []

    def move(self, dx, dy):
        self.events.append(("move", dx, dy))

    def button(self, name, down):
        self.events.append(("button", name, down))

    def scroll(self, dx, dy):
        self.events.append(("scroll", dx, dy))

    def text(self, s):
        self.events.append(("text", s))

    def key(self, name):
        self.events.append(("key", name))


class WindowsInput:
    BUTTON_FLAGS = {("left", True): MOUSEEVENTF_LEFTDOWN, ("left", False): MOUSEEVENTF_LEFTUP,
                    ("right", True): MOUSEEVENTF_RIGHTDOWN, ("right", False): MOUSEEVENTF_RIGHTUP}
    KEY_CODES = {"backspace": VK_BACK, "enter": VK_RETURN}

    def __init__(self):
        self.api = None
        self.load_lock = threading.Lock()

    def structures(self):
        import ctypes
        dword = ctypes.c_uint32
        word = ctypes.c_uint16
        ulong_ptr = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32), ("mouseData", dword),
                        ("dwFlags", dword), ("time", dword), ("dwExtraInfo", ulong_ptr)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", word), ("wScan", word), ("dwFlags", dword), ("time", dword),
                        ("dwExtraInfo", ulong_ptr)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", dword), ("wParamL", word), ("wParamH", word)]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", dword), ("u", INPUT_UNION)]

        return MOUSEINPUT, KEYBDINPUT, INPUT

    def load(self):
        with self.load_lock:
            if self.api is None:
                import ctypes
                from ctypes import wintypes
                INPUT = self.structures()[2]
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
                user32.SendInput.restype = wintypes.UINT
                self.api = (user32, INPUT)
            return self.api

    def send(self, events):
        import ctypes
        if not events:
            return
        user32, INPUT = self.load()
        array = (INPUT * len(events))()
        for slot, event in zip(array, events):
            if event[0] == "mouse":
                _, dx, dy, data, flags = event
                slot.type = INPUT_MOUSE
                slot.mi.dx = dx
                slot.mi.dy = dy
                slot.mi.mouseData = data & 0xFFFFFFFF
                slot.mi.dwFlags = flags
            else:
                _, vk, scan, flags = event
                slot.type = INPUT_KEYBOARD
                slot.ki.wVk = vk
                slot.ki.wScan = scan
                slot.ki.dwFlags = flags
        if user32.SendInput(len(events), array, ctypes.sizeof(INPUT)) == 0:
            raise ctypes.WinError(ctypes.get_last_error())

    def move(self, dx, dy):
        self.send([("mouse", dx, dy, 0, MOUSEEVENTF_MOVE)])

    def button(self, name, down):
        self.send([("mouse", 0, 0, 0, self.BUTTON_FLAGS[(name, bool(down))])])

    def scroll(self, dx, dy):
        events = []
        if dy:
            events.append(("mouse", 0, 0, -dy * WHEEL_STEP, MOUSEEVENTF_WHEEL))
        if dx:
            events.append(("mouse", 0, 0, dx * WHEEL_STEP, MOUSEEVENTF_HWHEEL))
        self.send(events)

    def text(self, s):
        data = s.encode("utf-16-le")
        events = []
        for unit in struct.unpack("<%dH" % (len(data) // 2), data):
            events.append(("key", 0, unit, KEYEVENTF_UNICODE))
            events.append(("key", 0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        self.send(events)

    def key(self, name):
        vk = self.KEY_CODES[name]
        self.send([("key", vk, 0, 0), ("key", vk, 0, KEYEVENTF_KEYUP)])


def default_input():
    if sys.platform == "win32":
        return WindowsInput()
    return DryRunInput()


class WsError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def ws_accept_key(key):
    digest = hashlib.sha1((key + WS_GUID).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def ws_unmask(data, mask):
    return bytes(b ^ mask[i & 3] for i, b in enumerate(data))


def ws_encode_frame(opcode, payload=b"", mask=None, fin=True):
    first = (0x80 if fin else 0) | opcode
    flag = 0x80 if mask is not None else 0
    size = len(payload)
    if size < 126:
        header = struct.pack(">BB", first, flag | size)
    elif size < 65536:
        header = struct.pack(">BBH", first, flag | 126, size)
    else:
        header = struct.pack(">BBQ", first, flag | 127, size)
    if mask is not None:
        return header + mask + ws_unmask(payload, mask)
    return header + payload


def ws_parse_header(buf):
    if len(buf) < 2:
        return None
    first, second = buf[0], buf[1]
    length = second & 0x7F
    size = 2
    if length == 126:
        if len(buf) < 4:
            return None
        length = struct.unpack(">H", bytes(buf[2:4]))[0]
        size = 4
    elif length == 127:
        if len(buf) < 10:
            return None
        length = struct.unpack(">Q", bytes(buf[2:10]))[0]
        size = 10
    mask = None
    if second & 0x80:
        if len(buf) < size + 4:
            return None
        mask = bytes(buf[size:size + 4])
        size += 4
    return bool(first & 0x80), first & 0x70, first & 0x0F, mask, length, size


class WsReader:
    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def fill(self):
        chunk = self.sock.recv(4096)
        if not chunk:
            raise ConnectionError("connection closed")
        self.buf += chunk

    def read_frame(self):
        while True:
            header = ws_parse_header(self.buf)
            if header is not None:
                break
            self.fill()
        fin, rsv, opcode, mask, length, size = header
        if mask is None or rsv:
            raise WsError(1002)
        if length > WS_MAX_PAYLOAD:
            raise WsError(1009)
        if not fin or opcode in (0x0, 0x2):
            raise WsError(1003)
        if opcode not in (WS_OP_TEXT, WS_OP_CLOSE, WS_OP_PING, WS_OP_PONG):
            raise WsError(1002)
        if opcode >= WS_OP_CLOSE and length > 125:
            raise WsError(1002)
        total = size + length
        while len(self.buf) < total:
            self.fill()
        payload = ws_unmask(self.buf[size:total], mask)
        del self.buf[:total]
        return opcode, payload


def set_nodelay(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


def status_frame(running):
    payload = json.dumps({"t": "status", "running": running}).encode("utf-8")
    return ws_encode_frame(WS_OP_TEXT, payload)


def ws_call(backend, name, *args):
    try:
        getattr(backend, name)(*args)
    except Exception:
        pass


def ws_number(msg, key, limit, default=None):
    value = msg.get(key, default)
    if type(value) is not int or abs(value) > limit:
        return None
    return value


def ws_clean_text(value):
    if not isinstance(value, str):
        return None
    cleaned = "".join(c for c in value if ord(c) >= 0x20 and ord(c) != 0x7F)
    return cleaned if 1 <= len(cleaned) <= MAX_TEXT else None


def ws_dispatch(backend, held, raw):
    try:
        msg = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return
    if not isinstance(msg, dict):
        return
    kind = msg.get("t")
    if kind == "move":
        dx = ws_number(msg, "dx", MAX_MOVE)
        dy = ws_number(msg, "dy", MAX_MOVE)
        if dx is not None and dy is not None:
            ws_call(backend, "move", dx, dy)
    elif kind == "btn":
        name, down = msg.get("b"), msg.get("d")
        if name in WS_BUTTONS and isinstance(down, bool):
            if down:
                held.add(name)
            ws_call(backend, "button", name, down)
            if not down:
                held.discard(name)
    elif kind == "click":
        name = msg.get("b")
        if name in WS_BUTTONS:
            ws_call(backend, "button", name, True)
            ws_call(backend, "button", name, False)
    elif kind == "scroll":
        dx = ws_number(msg, "dx", MAX_SCROLL, 0)
        dy = ws_number(msg, "dy", MAX_SCROLL, 0)
        if dx is not None and dy is not None:
            ws_call(backend, "scroll", dx, dy)
    elif kind == "text":
        cleaned = ws_clean_text(msg.get("s"))
        if cleaned is not None:
            ws_call(backend, "text", cleaned)
    elif kind == "key":
        name = msg.get("k")
        if name in WS_KEYS:
            ws_call(backend, "key", name)


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

    def __init__(self, address, handler, base_dir, loopback_check, platform, extractor,
                 input_backend=None, process_source=None, watcher=None):
        super().__init__(address, handler)
        self.base_dir = base_dir
        self.loopback_check = loopback_check
        self.platform = platform
        self.extractor = extractor
        self.input_backend = input_backend
        self.process_source = process_source
        self.watcher = watcher
        self.config_lock = threading.Lock()
        self.ws_ping_interval = WS_PING_INTERVAL
        self.ws_connections = 0
        self.status_connections = 0
        self.ws_lock = threading.Lock()

    def server_close(self):
        super().server_close()
        self.watcher.shutdown()


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
        elif path == "/ws":
            self.websocket("input")
        elif path == "/ws/status":
            self.websocket("status")
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
        self.send_json(200, {"buttons": stored_slots(config)})

    def get_icon(self, button_id):
        try:
            buttons = stored_slots(read_config(self.server.base_dir))
        except (OSError, ValueError):
            self.send_error_json(500, "cannot read " + CONFIG_NAME)
            return
        for button in buttons:
            if button is not None and button.get("id") == button_id:
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
            buttons = stored_slots(read_config(self.server.base_dir))
        except (OSError, ValueError):
            self.send_error_json(500, "cannot read " + CONFIG_NAME)
            return
        for button in buttons:
            if button is not None and button.get("id") == button_id:
                result = press_button(button, self.server.platform)
                if result["ok"]:
                    self.server.watcher.kick()
                self.send_json(200, result)
                return
        self.send_json(404, {"ok": False, "action": "error", "message": "unknown button"})

    def is_ws_upgrade(self):
        connection = [t.strip().lower() for t in self.headers.get("Connection", "").split(",")]
        return (self.headers.get("Upgrade", "").strip().lower() == "websocket"
                and "upgrade" in connection
                and self.headers.get("Sec-WebSocket-Version", "").strip() == "13"
                and bool(self.headers.get("Sec-WebSocket-Key", "").strip()))

    def ws_origin_allowed(self):
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            netloc = urlsplit(origin).netloc.lower()
        except ValueError:
            return False
        return netloc != "" and netloc == self.headers.get("Host", "").strip().lower()

    def ws_acquire(self, channel):
        server = self.server
        with server.ws_lock:
            if channel == "status":
                accepted = server.status_connections < MAX_STATUS_CONNECTIONS
                if accepted:
                    server.status_connections += 1
            else:
                accepted = server.ws_connections < WS_MAX_CONNECTIONS
                if accepted:
                    server.ws_connections += 1
        return accepted

    def ws_free(self, channel):
        server = self.server
        with server.ws_lock:
            if channel == "status":
                server.status_connections -= 1
            else:
                server.ws_connections -= 1

    def websocket(self, channel):
        if not self.is_ws_upgrade():
            self.send_error_json(400, "websocket upgrade required")
            return
        if not self.ws_origin_allowed():
            self.send_error_json(403, "forbidden")
            return
        if not self.ws_acquire(channel):
            self.send_error_json(503, "too many connections")
            return
        self.ws_held = set()
        self.ws_subscribed = False
        self.ws_closed = False
        self.ws_send_lock = threading.Lock()
        self.close_connection = True
        try:
            accept = ws_accept_key(self.headers["Sec-WebSocket-Key"].strip())
            self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\n"
                              "Upgrade: websocket\r\n"
                              "Connection: Upgrade\r\n"
                              "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode("ascii"))
            set_nodelay(self.connection)
            if channel == "status":
                self.status_start()
            self.ws_loop(channel)
        except OSError:
            pass
        finally:
            self.ws_release()
            self.ws_free(channel)

    def ws_send(self, frame, closing=False):
        with self.ws_send_lock:
            if self.ws_closed:
                return False
            if closing:
                self.ws_closed = True
            self.connection.sendall(frame)
            return True

    def status_start(self):
        with self.ws_send_lock:
            self.ws_subscribed = True
            running = self.server.watcher.subscribe(self.status_notify)
            self.connection.sendall(status_frame(running))

    def status_notify(self, changes):
        self.ws_send(status_frame(changes))

    def ws_release(self):
        if self.ws_subscribed:
            self.ws_subscribed = False
            self.server.watcher.unsubscribe(self.status_notify)
        held = sorted(self.ws_held)
        self.ws_held.clear()
        for name in held:
            ws_call(self.server.input_backend, "button", name, False)

    def ws_loop(self, channel):
        sock = self.connection
        sock.settimeout(self.server.ws_ping_interval)
        reader = WsReader(sock)
        backend = self.server.input_backend
        outstanding = False
        while True:
            try:
                opcode, payload = reader.read_frame()
            except socket.timeout:
                if outstanding:
                    return
                self.ws_send(ws_encode_frame(WS_OP_PING))
                outstanding = True
                continue
            except WsError as e:
                self.ws_close(struct.pack(">H", e.code))
                return
            outstanding = False
            if opcode == WS_OP_TEXT:
                if channel == "input":
                    ws_dispatch(backend, self.ws_held, payload)
            elif opcode == WS_OP_PING:
                self.ws_send(ws_encode_frame(WS_OP_PONG, payload))
            elif opcode == WS_OP_CLOSE:
                self.ws_close(payload[:2] if len(payload) >= 2 else b"")
                return

    def ws_close(self, payload):
        sock = self.connection
        self.ws_send(ws_encode_frame(WS_OP_CLOSE, payload), closing=True)
        self.ws_release()
        try:
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(0.5)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and sock.recv(4096):
                pass
        except OSError:
            pass


def watched_processes(base_dir):
    try:
        buttons = stored_slots(read_config(base_dir))
    except (OSError, ValueError):
        return {}
    result = {}
    for button in buttons:
        if button is None:
            continue
        bid, process = button.get("id"), button.get("process")
        if isinstance(bid, str) and isinstance(process, str) and process.strip():
            result[bid] = process.strip().lower()
    return result


def lower_priority(kernel32=None):
    try:
        if kernel32 is None:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.argtypes = []
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.SetPriorityClass.restype = ctypes.c_int32
        return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(),
                                              BELOW_NORMAL_PRIORITY_CLASS))
    except Exception:
        return False


def create_server(base_dir, host="127.0.0.1", port=0, loopback_check=is_loopback, platform=None,
                  extractor=None, input_backend=None, process_source=None):
    ensure_config(base_dir)
    if process_source is None:
        process_source = default_process_source()
    if platform is None:
        platform = default_platform(process_source)
    if extractor is None:
        extractor = default_extractor()
    if input_backend is None:
        input_backend = default_input()
    watcher = ProcessWatcher(process_source, lambda: watched_processes(base_dir))
    return DeckServer((host, port), Handler, base_dir, loopback_check, platform, extractor,
                      input_backend, process_source, watcher)


def build_parser():
    parser = argparse.ArgumentParser(description="Stream Deck server")
    parser.add_argument("--port", type=int, help="port to listen on (default: from buttons.json)")
    parser.add_argument("--normal-priority", action="store_true",
                        help="do not lower the server's process priority (Windows)")
    return parser


def main():
    args = build_parser().parse_args()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    ensure_config(base_dir)
    port = args.port or read_config(base_dir).get("port", DEFAULT_PORT)
    server = create_server(base_dir, "0.0.0.0", port)
    print("Stream Deck running")
    print("  PC:    http://localhost:%d" % port)
    print("  Phone: http://%s:%d" % (lan_ip(), port))
    if sys.platform == "win32" and not args.normal_priority and lower_priority():
        print("  Priority: below normal (start with --normal-priority to disable)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
