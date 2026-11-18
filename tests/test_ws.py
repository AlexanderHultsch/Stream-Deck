import base64
import ctypes
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

RFC_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
RFC_ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
TIMEOUT = 5


def wait_for(predicate, timeout=TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class WsClient:
    def __init__(self, port, headers=None):
        self.port = port
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT)
        self.buf = bytearray()
        self.key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = {
            "Host": "127.0.0.1:%d" % port,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": self.key,
            "Sec-WebSocket-Version": "13",
        }
        request.update(headers or {})
        lines = ["GET /ws HTTP/1.1"]
        lines += ["%s: %s" % (k, v) for k, v in request.items() if v is not None]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        self.status, self.headers, self.body = self.read_response()

    def read_until(self, marker):
        while marker not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            self.buf += chunk
        head, _, rest = bytes(self.buf).partition(marker)
        self.buf = bytearray(rest)
        return head

    def read_response(self):
        head = self.read_until(b"\r\n\r\n").decode("iso-8859-1").split("\r\n")
        status = int(head[0].split()[1])
        headers = {}
        for line in head[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        body = b""
        length = int(headers.get("content-length", "0"))
        if status != 101:
            while len(self.buf) < length:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                self.buf += chunk
            body, self.buf = bytes(self.buf[:length]), self.buf[length:]
        return status, headers, body

    def send_raw(self, data):
        self.sock.sendall(data)

    def send_frame(self, opcode, payload=b"", fin=True):
        self.send_raw(server.ws_encode_frame(opcode, payload, os.urandom(4), fin))

    def send_json(self, message):
        self.send_frame(server.WS_OP_TEXT, json.dumps(message).encode("utf-8"))

    def read_frame(self, timeout=TIMEOUT):
        self.sock.settimeout(timeout)
        while True:
            header = server.ws_parse_header(self.buf)
            if header is not None:
                fin, rsv, opcode, mask, length, size = header
                if len(self.buf) >= size + length:
                    payload = bytes(self.buf[size:size + length])
                    del self.buf[:size + length]
                    return opcode, payload
            try:
                chunk = self.sock.recv(4096)
            except ConnectionResetError:
                return None
            if not chunk:
                return None
            self.buf += chunk

    def read_close_code(self):
        frame = self.read_frame()
        assert frame is not None and frame[0] == server.WS_OP_CLOSE, frame
        return struct.unpack(">H", frame[1][:2])[0]

    def sync(self):
        payload = os.urandom(8)
        self.send_frame(server.WS_OP_PING, payload)
        while True:
            frame = self.read_frame()
            if frame == (server.WS_OP_PONG, payload):
                return

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class FailingInput(server.DryRunInput):
    def move(self, dx, dy):
        raise RuntimeError("boom")


class WsTestCase(unittest.TestCase):
    loopback_check = staticmethod(server.is_loopback)
    input_backend_class = server.DryRunInput

    def setUp(self):
        self.base_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base_dir, True)
        self.input = self.input_backend_class()
        self.httpd = server.create_server(self.base_dir, loopback_check=self.loopback_check,
                                          input_backend=self.input)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.clients = []

    def stop_server(self):
        for client in self.clients:
            client.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def connect(self, headers=None):
        client = WsClient(self.port, headers)
        self.clients.append(client)
        return client

    def open(self, headers=None):
        client = self.connect(headers)
        self.assertEqual(client.status, 101)
        return client


class HelpersTest(unittest.TestCase):
    def test_accept_key_rfc_example(self):
        self.assertEqual(server.ws_accept_key(RFC_KEY), RFC_ACCEPT)

    def test_unmask(self):
        mask = b"\x01\x02\x03\x04"
        self.assertEqual(server.ws_unmask(b"\x01\x02\x03\x04\x05", mask), b"\x00\x00\x00\x00\x04")

    def test_encode_unmasked_lengths(self):
        self.assertEqual(server.ws_encode_frame(server.WS_OP_TEXT, b"hi"), b"\x81\x02hi")
        self.assertEqual(server.ws_encode_frame(server.WS_OP_PING), b"\x89\x00")
        medium = server.ws_encode_frame(server.WS_OP_TEXT, b"x" * 300)
        self.assertEqual(medium[:4], b"\x81\x7e\x01\x2c")
        self.assertEqual(len(medium), 304)
        large = server.ws_encode_frame(server.WS_OP_TEXT, b"x" * 70000)
        self.assertEqual(large[:10], b"\x81\x7f" + struct.pack(">Q", 70000))
        self.assertEqual(len(large), 70010)

    def test_encode_masked_and_fin(self):
        mask = b"\x0a\x0b\x0c\x0d"
        frame = server.ws_encode_frame(server.WS_OP_TEXT, b"ab", mask, fin=False)
        self.assertEqual(frame, b"\x01\x82" + mask + bytes([0x61 ^ 0x0a, 0x62 ^ 0x0b]))

    def test_parse_header_roundtrip(self):
        mask = b"\x01\x02\x03\x04"
        for size in (0, 5, 125, 126, 4096, 65535, 65536, 70000):
            frame = server.ws_encode_frame(server.WS_OP_TEXT, b"x" * size, mask)
            fin, rsv, opcode, got_mask, length, header = server.ws_parse_header(frame)
            self.assertEqual((fin, rsv, opcode, got_mask, length), (True, 0, 1, mask, size))
            self.assertEqual(server.ws_unmask(frame[header:], mask), b"x" * size)
        fin, rsv, opcode, got_mask, length, header = server.ws_parse_header(
            server.ws_encode_frame(server.WS_OP_PING, b"", None, fin=False))
        self.assertEqual((fin, opcode, got_mask, length, header), (False, 9, None, 0, 2))

    def test_parse_header_incomplete(self):
        frame = server.ws_encode_frame(server.WS_OP_TEXT, b"x" * 70000, b"\x01\x02\x03\x04")
        for cut in (0, 1, 2, 3, 9, 13):
            self.assertIsNone(server.ws_parse_header(frame[:cut]), cut)
        self.assertIsNotNone(server.ws_parse_header(frame[:14]))

    def test_parse_header_reserved_bits(self):
        self.assertEqual(server.ws_parse_header(b"\xc1\x00")[1], 0x40)


class DryRunInputTest(unittest.TestCase):
    def test_records_calls(self):
        backend = server.DryRunInput()
        backend.move(1, -2)
        backend.button("left", True)
        backend.scroll(3, 4)
        backend.text("hi")
        backend.key("enter")
        self.assertEqual(backend.events, [("move", 1, -2), ("button", "left", True),
                                          ("scroll", 3, 4), ("text", "hi"), ("key", "enter")])

    def test_default_input_off_windows(self):
        if sys.platform != "win32":
            self.assertIsInstance(server.default_input(), server.DryRunInput)


class FakeUser32:
    def __init__(self):
        self.calls = []

    def SendInput(self, count, array, size):
        items = []
        for i in range(count):
            slot = array[i]
            if slot.type == server.INPUT_MOUSE:
                mi = slot.mi
                items.append(("mouse", mi.dx, mi.dy, mi.mouseData, mi.dwFlags))
            else:
                ki = slot.ki
                items.append(("key", ki.wVk, ki.wScan, ki.dwFlags))
        self.calls.append((count, size, items))
        return count


class WindowsInputTest(unittest.TestCase):
    def setUp(self):
        self.backend = server.WindowsInput()
        self.user32 = FakeUser32()
        self.backend.api = (self.user32, self.backend.structures()[2])

    def sent(self):
        return [items for _, _, items in self.user32.calls]

    def test_instantiable_off_windows(self):
        self.assertIsNone(server.WindowsInput().api)

    def test_struct_size(self):
        mouse, keyboard, input_type = self.backend.structures()
        pointer = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(input_type), 40 if pointer == 8 else 28)
        self.assertEqual(ctypes.sizeof(mouse), 32 if pointer == 8 else 24)
        self.assertEqual(ctypes.sizeof(keyboard), 24 if pointer == 8 else 16)

    def test_size_argument_is_struct_size(self):
        self.backend.move(1, 1)
        self.assertEqual(self.user32.calls[0][1], ctypes.sizeof(self.backend.structures()[2]))

    def test_move_is_relative_and_signed(self):
        self.backend.move(-5, 7)
        self.assertEqual(self.sent(), [[("mouse", -5, 7, 0, 0x0001)]])

    def test_buttons(self):
        for name, down, flag in (("left", True, 0x2), ("left", False, 0x4),
                                 ("right", True, 0x8), ("right", False, 0x10)):
            self.backend.button(name, down)
            self.assertEqual(self.sent()[-1], [("mouse", 0, 0, 0, flag)])

    def test_scroll_vertical_down_is_negative_wheel(self):
        self.backend.scroll(0, 5)
        self.assertEqual(self.sent(), [[("mouse", 0, 0, (-15) & 0xFFFFFFFF, 0x0800)]])
        self.backend.scroll(0, -5)
        self.assertEqual(self.sent()[-1], [("mouse", 0, 0, 15, 0x0800)])

    def test_scroll_horizontal_and_zero_axes(self):
        self.backend.scroll(-2, 0)
        self.assertEqual(self.sent(), [[("mouse", 0, 0, (-6) & 0xFFFFFFFF, 0x1000)]])
        self.backend.scroll(2, 1)
        self.assertEqual(self.sent()[-1], [("mouse", 0, 0, (-3) & 0xFFFFFFFF, 0x0800),
                                           ("mouse", 0, 0, 6, 0x1000)])
        count = len(self.user32.calls)
        self.backend.scroll(0, 0)
        self.assertEqual(len(self.user32.calls), count)

    def test_text_single_call_with_utf16_units(self):
        self.backend.text("a€\U0001F600")
        self.assertEqual(len(self.user32.calls), 1)
        expected = []
        for unit in (0x61, 0x20AC, 0xD83D, 0xDE00):
            expected.append(("key", 0, unit, 0x0004))
            expected.append(("key", 0, unit, 0x0006))
        self.assertEqual(self.sent(), [expected])

    def test_keys(self):
        self.backend.key("backspace")
        self.backend.key("enter")
        self.assertEqual(self.sent(), [[("key", 0x08, 0, 0), ("key", 0x08, 0, 2)],
                                       [("key", 0x0D, 0, 0), ("key", 0x0D, 0, 2)]])


class HandshakeTest(WsTestCase):
    def test_handshake_ok(self):
        client = self.connect({"Sec-WebSocket-Key": RFC_KEY})
        self.assertEqual(client.status, 101)
        self.assertEqual(client.headers["sec-websocket-accept"], RFC_ACCEPT)
        self.assertEqual(client.headers["upgrade"].lower(), "websocket")
        self.assertIn("upgrade", client.headers["connection"].lower())

    def test_random_key_accept(self):
        client = self.open()
        self.assertEqual(client.headers["sec-websocket-accept"], server.ws_accept_key(client.key))

    def test_header_variants_accepted(self):
        client = self.open({"Upgrade": "WebSocket", "Connection": "keep-alive, Upgrade"})
        client.sync()

    def test_missing_upgrade_headers(self):
        cases = ({"Upgrade": None}, {"Connection": None}, {"Connection": "keep-alive"},
                 {"Sec-WebSocket-Key": None}, {"Sec-WebSocket-Version": None},
                 {"Sec-WebSocket-Version": "8"}, {"Upgrade": "h2c"})
        for headers in cases:
            client = self.connect(headers)
            self.assertEqual(client.status, 400, headers)
            self.assertEqual(json.loads(client.body),
                             {"ok": False, "message": "websocket upgrade required"})

    def test_origin_mismatch_forbidden(self):
        for origin in ("http://evil.example", "http://127.0.0.1:1", "null", "http://"):
            client = self.connect({"Origin": origin})
            self.assertEqual(client.status, 403, origin)
            self.assertEqual(json.loads(client.body), {"ok": False, "message": "forbidden"})

    def test_origin_equal_to_host_allowed(self):
        origin = "http://127.0.0.1:%d" % self.port
        self.open({"Origin": origin})

    def test_no_origin_allowed(self):
        self.open()

    def test_plain_http_get_still_works(self):
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=TIMEOUT)
        self.addCleanup(conn.close)
        conn.sendall(b"GET /api/buttons HTTP/1.0\r\n\r\n")
        self.assertTrue(conn.recv(100).startswith(b"HTTP/1.0 200"))


class NoGuardTest(WsTestCase):
    loopback_check = staticmethod(lambda ip: False)

    def test_works_without_loopback(self):
        client = self.open()
        client.send_json({"t": "click", "b": "left"})
        client.sync()
        self.assertEqual(self.input.events, [("button", "left", True), ("button", "left", False)])


class MessageTest(WsTestCase):
    def setUp(self):
        super().setUp()
        self.client = self.open()

    def send_and_sync(self, *messages):
        for message in messages:
            if isinstance(message, (bytes, bytearray)):
                self.client.send_frame(server.WS_OP_TEXT, bytes(message))
            elif isinstance(message, str):
                self.client.send_frame(server.WS_OP_TEXT, message.encode("utf-8"))
            else:
                self.client.send_json(message)
        self.client.sync()

    def test_move(self):
        self.send_and_sync({"t": "move", "dx": 3, "dy": -4}, {"t": "move", "dx": 500, "dy": -500})
        self.assertEqual(self.input.events, [("move", 3, -4), ("move", 500, -500)])

    def test_button_down_up(self):
        self.send_and_sync({"t": "btn", "b": "left", "d": True},
                           {"t": "btn", "b": "left", "d": False},
                           {"t": "btn", "b": "right", "d": True})
        self.assertEqual(self.input.events, [("button", "left", True), ("button", "left", False),
                                             ("button", "right", True)])

    def test_click_is_down_then_up(self):
        self.send_and_sync({"t": "click", "b": "right"})
        self.assertEqual(self.input.events, [("button", "right", True), ("button", "right", False)])

    def test_scroll(self):
        self.send_and_sync({"t": "scroll", "dx": 5, "dy": 7}, {"t": "scroll", "dy": -400},
                           {"t": "scroll", "dx": 400})
        self.assertEqual(self.input.events, [("scroll", 5, 7), ("scroll", 0, -400),
                                             ("scroll", 400, 0)])

    def test_text(self):
        self.send_and_sync({"t": "text", "s": "hello €"}, {"t": "text", "s": "x" * 64})
        self.assertEqual(self.input.events, [("text", "hello €"), ("text", "x" * 64)])

    def test_text_control_characters_stripped(self):
        self.send_and_sync({"t": "text", "s": "a\nb\tc\x00d\x7fe\x1f"})
        self.assertEqual(self.input.events, [("text", "abcde")])

    def test_text_all_control_ignored(self):
        self.send_and_sync({"t": "text", "s": "\n\t\x00\x7f"}, {"t": "text", "s": ""})
        self.assertEqual(self.input.events, [])

    def test_text_too_long_ignored(self):
        self.send_and_sync({"t": "text", "s": "x" * 65})
        self.assertEqual(self.input.events, [])

    def test_text_length_counted_after_stripping(self):
        self.send_and_sync({"t": "text", "s": "x" * 64 + "\n\n"})
        self.assertEqual(self.input.events, [("text", "x" * 64)])

    def test_keys(self):
        self.send_and_sync({"t": "key", "k": "backspace"}, {"t": "key", "k": "enter"})
        self.assertEqual(self.input.events, [("key", "backspace"), ("key", "enter")])

    def test_invalid_messages_ignored_and_connection_usable(self):
        invalid = [
            {"t": "move", "dx": 501, "dy": 0}, {"t": "move", "dx": 0, "dy": -501},
            {"t": "move", "dx": 1.5, "dy": 0}, {"t": "move", "dx": 1.0, "dy": 0},
            {"t": "move", "dx": True, "dy": 0}, {"t": "move", "dx": "1", "dy": 0},
            {"t": "move", "dx": None, "dy": 0}, {"t": "move", "dx": 1}, {"t": "move"},
            {"t": "btn", "b": "middle", "d": True}, {"t": "btn", "b": "left", "d": 1},
            {"t": "btn", "b": "left"}, {"t": "btn", "b": ["left"], "d": True},
            {"t": "click", "b": "middle"}, {"t": "click"},
            {"t": "scroll", "dx": 401, "dy": 0}, {"t": "scroll", "dx": 0, "dy": -401},
            {"t": "scroll", "dx": 0.5}, {"t": "scroll", "dy": "3"}, {"t": "scroll", "dy": True},
            {"t": "text", "s": 5}, {"t": "text"}, {"t": "text", "s": ["a"]},
            {"t": "key", "k": "tab"}, {"t": "key"}, {"t": "key", "k": ["enter"]},
            {"t": "bogus"}, {"t": 1}, {"t": ["move"]}, {"t": {"a": 1}}, {"dx": 1, "dy": 1}, {},
            [], [1, 2], "move", 5, None, True, 1.5,
        ]
        raw = [b"not json", b"{", b"", b"\xff\xfe\xfd", b'{"t":"move","dx":1,"dy":1',
               b"[" * 3000, b'{"t":"move","dx":1e400,"dy":0}', b'{"t":"move","dx":NaN,"dy":0}']
        self.send_and_sync(*invalid, *raw)
        self.assertEqual(self.input.events, [])
        self.send_and_sync({"t": "move", "dx": 1, "dy": 2})
        self.assertEqual(self.input.events, [("move", 1, 2)])

    def test_server_never_replies_to_messages(self):
        self.send_and_sync({"t": "move", "dx": 1, "dy": 1}, {"t": "bogus"}, b"nope")
        self.client.sock.settimeout(0.2)
        with self.assertRaises(socket.timeout):
            self.client.sock.recv(1)

    def test_16bit_length(self):
        padding = " " * 300
        self.send_and_sync('{"t":"key","k":"enter"}' + padding)
        self.assertEqual(self.input.events, [("key", "enter")])

    def test_64bit_length_form(self):
        payload = b'{"t":"key","k":"backspace"}'
        mask = b"\x11\x22\x33\x44"
        self.client.send_raw(b"\x81\xff" + struct.pack(">Q", len(payload)) + mask
                             + server.ws_unmask(payload, mask))
        self.client.sync()
        self.assertEqual(self.input.events, [("key", "backspace")])

    def test_max_payload_accepted(self):
        body = b'{"t":"key","k":"enter"}'
        self.send_and_sync(body + b" " * (server.WS_MAX_PAYLOAD - len(body)))
        self.assertEqual(self.input.events, [("key", "enter")])

    def test_split_frame_bytes(self):
        frame = server.ws_encode_frame(server.WS_OP_TEXT, b'{"t":"key","k":"enter"}', b"abcd")
        for i in range(len(frame)):
            self.client.send_raw(frame[i:i + 1])
            time.sleep(0.001)
        self.client.sync()
        self.assertEqual(self.input.events, [("key", "enter")])


class ControlFrameTest(WsTestCase):
    def test_ping_gets_pong_with_same_payload(self):
        client = self.open()
        client.send_frame(server.WS_OP_PING, b"hello")
        self.assertEqual(client.read_frame(), (server.WS_OP_PONG, b"hello"))

    def test_pong_ignored(self):
        client = self.open()
        client.send_frame(server.WS_OP_PONG, b"x")
        client.sync()
        self.assertEqual(self.input.events, [])

    def test_close_gets_close_reply(self):
        client = self.open()
        client.send_frame(server.WS_OP_CLOSE, struct.pack(">H", 1000))
        self.assertEqual(client.read_frame(), (server.WS_OP_CLOSE, struct.pack(">H", 1000)))
        self.assertIsNone(client.read_frame())

    def test_close_without_payload(self):
        client = self.open()
        client.send_frame(server.WS_OP_CLOSE)
        self.assertEqual(client.read_frame(), (server.WS_OP_CLOSE, b""))
        self.assertIsNone(client.read_frame())

    def test_unmasked_frame_closes_1002(self):
        client = self.open()
        client.send_raw(server.ws_encode_frame(server.WS_OP_TEXT, b'{"t":"key","k":"enter"}'))
        self.assertEqual(client.read_close_code(), 1002)
        self.assertIsNone(client.read_frame())
        self.assertEqual(self.input.events, [])

    def test_fragmented_frame_closes_1003(self):
        client = self.open()
        client.send_frame(server.WS_OP_TEXT, b'{"t":"key",', fin=False)
        self.assertEqual(client.read_close_code(), 1003)
        self.assertIsNone(client.read_frame())

    def test_binary_frame_closes_1003(self):
        client = self.open()
        client.send_frame(0x2, b"\x00\x01")
        self.assertEqual(client.read_close_code(), 1003)

    def test_continuation_frame_closes_1003(self):
        client = self.open()
        client.send_frame(0x0, b"abc")
        self.assertEqual(client.read_close_code(), 1003)

    def test_oversized_payload_closes_1009(self):
        client = self.open()
        client.send_frame(server.WS_OP_TEXT, b" " * (server.WS_MAX_PAYLOAD + 1))
        self.assertEqual(client.read_close_code(), 1009)
        self.assertIsNone(client.read_frame())

    def test_huge_declared_length_closes_1009_without_payload(self):
        client = self.open()
        client.send_raw(b"\x81\xff" + struct.pack(">Q", 1 << 40) + b"\x00\x00\x00\x00")
        self.assertEqual(client.read_close_code(), 1009)

    def test_oversized_control_frame_closes_1002(self):
        client = self.open()
        client.send_frame(server.WS_OP_PING, b"x" * 126)
        self.assertEqual(client.read_close_code(), 1002)

    def test_reserved_bits_close_1002(self):
        client = self.open()
        client.send_raw(b"\xc1\x80\x00\x00\x00\x00")
        self.assertEqual(client.read_close_code(), 1002)

    def test_unknown_opcode_closes_1002(self):
        client = self.open()
        client.send_frame(0x3, b"")
        self.assertEqual(client.read_close_code(), 1002)


class HeldButtonTest(WsTestCase):
    def released(self, name="left"):
        return wait_for(lambda: ("button", name, False) in self.input.events)

    def test_released_on_abrupt_disconnect(self):
        client = self.open()
        client.send_json({"t": "btn", "b": "left", "d": True})
        client.sync()
        client.close()
        self.assertTrue(self.released())
        self.assertEqual(self.input.events, [("button", "left", True), ("button", "left", False)])

    def test_released_on_connection_reset(self):
        client = self.open()
        client.send_json({"t": "btn", "b": "right", "d": True})
        client.sync()
        client.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.close()
        self.assertTrue(self.released("right"))

    def test_released_on_clean_close(self):
        client = self.open()
        client.send_json({"t": "btn", "b": "left", "d": True})
        client.send_frame(server.WS_OP_CLOSE, struct.pack(">H", 1000))
        self.assertEqual(client.read_close_code(), 1000)
        self.assertTrue(self.released())

    def test_released_on_protocol_error(self):
        client = self.open()
        client.send_json({"t": "btn", "b": "left", "d": True})
        client.send_frame(0x2, b"x")
        self.assertEqual(client.read_close_code(), 1003)
        self.assertTrue(self.released())

    def test_both_buttons_released_once_each(self):
        client = self.open()
        client.send_json({"t": "btn", "b": "left", "d": True})
        client.send_json({"t": "btn", "b": "right", "d": True})
        client.sync()
        client.close()
        self.assertTrue(wait_for(lambda: len(self.input.events) == 4))
        self.assertEqual(sorted(self.input.events[2:]),
                         [("button", "left", False), ("button", "right", False)])

    def test_no_release_after_button_up(self):
        client = self.open()
        client.send_json({"t": "btn", "b": "left", "d": True})
        client.send_json({"t": "btn", "b": "left", "d": False})
        client.sync()
        client.close()
        self.assertTrue(wait_for(lambda: self.httpd.ws_connections == 0))
        self.assertEqual(self.input.events, [("button", "left", True), ("button", "left", False)])

    def test_buttons_are_per_connection(self):
        first, second = self.open(), self.open()
        first.send_json({"t": "btn", "b": "left", "d": True})
        first.sync()
        second.send_frame(server.WS_OP_CLOSE)
        second.read_frame()
        self.assertTrue(wait_for(lambda: self.httpd.ws_connections == 1))
        self.assertEqual(self.input.events, [("button", "left", True)])
        first.close()
        self.assertTrue(self.released())


class ConnectionLimitTest(WsTestCase):
    def test_fifth_connection_rejected_and_slot_frees(self):
        clients = [self.open() for _ in range(server.WS_MAX_CONNECTIONS)]
        extra = self.connect()
        self.assertEqual(extra.status, 503)
        self.assertEqual(json.loads(extra.body), {"ok": False, "message": "too many connections"})
        clients[0].send_frame(server.WS_OP_CLOSE)
        clients[0].read_frame()
        clients[0].close()
        deadline = time.monotonic() + TIMEOUT
        status = None
        while time.monotonic() < deadline:
            retry = self.connect()
            status = retry.status
            if status == 101:
                break
            time.sleep(0.02)
        self.assertEqual(status, 101)

    def test_failed_handshakes_do_not_consume_slots(self):
        for _ in range(server.WS_MAX_CONNECTIONS + 2):
            self.assertEqual(self.connect({"Origin": "http://evil.example"}).status, 403)
            self.assertEqual(self.connect({"Upgrade": None}).status, 400)
        self.assertEqual(self.httpd.ws_connections, 0)
        self.open()

    def test_counter_returns_to_zero(self):
        clients = [self.open() for _ in range(3)]
        for client in clients:
            client.close()
        self.assertTrue(wait_for(lambda: self.httpd.ws_connections == 0))


class KeepaliveTest(WsTestCase):
    def setUp(self):
        super().setUp()
        self.httpd.ws_ping_interval = 0.2

    def test_default_interval(self):
        base = server.create_server(self.base_dir)
        self.addCleanup(base.server_close)
        self.assertEqual(base.ws_ping_interval, 15)

    def test_idle_client_receives_ping(self):
        client = self.open()
        frame = client.read_frame(2)
        self.assertEqual(frame, (server.WS_OP_PING, b""))

    def test_answering_client_stays_connected(self):
        client = self.open()
        deadline = time.monotonic() + 1.2
        pings = 0
        while time.monotonic() < deadline:
            frame = client.read_frame(0.5)
            self.assertIsNotNone(frame)
            if frame[0] == server.WS_OP_PING:
                pings += 1
                client.send_frame(server.WS_OP_PONG, frame[1])
        self.assertGreaterEqual(pings, 3)
        client.send_json({"t": "key", "k": "enter"})
        client.sync()
        self.assertEqual(self.input.events, [("key", "enter")])

    def test_unanswered_ping_closes_and_releases(self):
        client = self.open()
        client.send_json({"t": "btn", "b": "left", "d": True})
        started = time.monotonic()
        frames = []
        while True:
            frame = client.read_frame(3)
            if frame is None:
                break
            frames.append(frame)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(frames, [(server.WS_OP_PING, b"")])
        self.assertTrue(wait_for(lambda: ("button", "left", False) in self.input.events))
        self.assertTrue(wait_for(lambda: self.httpd.ws_connections == 0))

    def test_any_frame_clears_outstanding_ping(self):
        client = self.open()
        self.assertEqual(client.read_frame(2)[0], server.WS_OP_PING)
        client.send_json({"t": "bogus"})
        time.sleep(0.3)
        frame = client.read_frame(2)
        self.assertEqual(frame, (server.WS_OP_PING, b""))
        client.sync()


class FailingBackendTest(WsTestCase):
    input_backend_class = FailingInput

    def test_backend_exception_does_not_close_connection(self):
        client = self.open()
        client.send_json({"t": "move", "dx": 1, "dy": 1})
        client.send_json({"t": "move", "dx": 2, "dy": 2})
        client.send_json({"t": "click", "b": "left"})
        client.sync()
        self.assertEqual(self.input.events, [("button", "left", True), ("button", "left", False)])

    def test_failing_release_still_frees_slot(self):
        class BrokenButtons(server.DryRunInput):
            def button(self, name, down):
                raise RuntimeError("boom")

        self.httpd.input_backend = BrokenButtons()
        client = self.open()
        client.send_json({"t": "btn", "b": "left", "d": True})
        client.sync()
        client.close()
        self.assertTrue(wait_for(lambda: self.httpd.ws_connections == 0))


if __name__ == "__main__":
    unittest.main()
