import hashlib
import http.client
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def decode_png(data):
    assert data[:8] == SIGNATURE, "bad signature"
    pos = 8
    chunks = []
    while pos < len(data):
        length, = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        crc, = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF, "bad CRC for %r" % kind
        chunks.append((kind, body))
        pos += 12 + length
    assert pos == len(data)
    return chunks


def decode_rgba(data):
    chunks = decode_png(data)
    kinds = [kind for kind, _ in chunks]
    assert kinds == [b"IHDR", b"IDAT", b"IEND"], kinds
    width, height, depth, colour, comp, filt, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (depth, colour, comp, filt, interlace) == (8, 6, 0, 0, 0)
    raw = zlib.decompress(chunks[1][1])
    assert len(raw) == height * (1 + width * 4)
    stride = 1 + width * 4
    pixels = bytearray()
    for y in range(height):
        assert raw[y * stride] == 0, "filter byte"
        pixels += raw[y * stride + 1:(y + 1) * stride]
    return width, height, bytes(pixels)


def make_rgba(width, height):
    return bytes((x * 7 + y * 13 + c * 50) % 256
                 for y in range(height) for x in range(width) for c in range(4))


def make_button(bid="app", target="C:\\app.exe"):
    return {"id": bid, "name": bid, "target": target, "process": "",
            "window": "normal", "ifRunning": "focus"}


class FakeExtractor:
    def __init__(self, result="default", error=None):
        self.result = (2, 2, make_rgba(2, 2)) if result == "default" else result
        self.error = error
        self.targets = []

    def extract(self, target):
        self.targets.append(target)
        if self.error is not None:
            raise self.error
        return self.result


class EncodePngTest(unittest.TestCase):
    def test_roundtrip(self):
        rgba = make_rgba(5, 3)
        data = server.encode_png(5, 3, rgba)
        self.assertEqual(decode_rgba(data), (5, 3, rgba))

    def test_single_pixel(self):
        data = server.encode_png(1, 1, b"\x01\x02\x03\x04")
        self.assertEqual(decode_rgba(data), (1, 1, b"\x01\x02\x03\x04"))

    def test_wrong_length(self):
        for size in (0, 15, 17):
            with self.assertRaises(ValueError):
                server.encode_png(2, 2, bytes(size))


class PixelHelpersTest(unittest.TestCase):
    def test_bgra_to_rgba(self):
        self.assertEqual(server.bgra_to_rgba(b"\x01\x02\x03\x04\x05\x06\x07\x08"),
                         b"\x03\x02\x01\x04\x07\x06\x05\x08")
        self.assertEqual(server.bgra_to_rgba(b""), b"")

    def test_apply_mask_alpha(self):
        rgba = b"\x01\x02\x03\x00\x04\x05\x06\x00\x07\x08\x09\x00"
        result = server.apply_mask_alpha(rgba, bytes([0, 255, 1]))
        self.assertEqual(result, b"\x01\x02\x03\xff\x04\x05\x06\x00\x07\x08\x09\x00")


class ParseCommandExeTest(unittest.TestCase):
    def test_quoted_with_args(self):
        command = '"C:\\Program Files (x86)\\Steam\\steam.exe" -- "%1"'
        self.assertEqual(server.parse_command_exe(command),
                         "C:\\Program Files (x86)\\Steam\\steam.exe")

    def test_quoted_path_with_spaces(self):
        self.assertEqual(server.parse_command_exe('"C:\\My Apps\\a b.exe"'), "C:\\My Apps\\a b.exe")

    def test_unquoted_with_args(self):
        self.assertEqual(server.parse_command_exe("C:\\x\\a.exe %1"), "C:\\x\\a.exe")

    def test_uppercase_exe(self):
        self.assertEqual(server.parse_command_exe("C:\\x\\A.EXE /url %1"), "C:\\x\\A.EXE")
        self.assertEqual(server.parse_command_exe('"C:\\x\\A.EXE" %1'), "C:\\x\\A.EXE")

    def test_none_cases(self):
        for command in ("", "   ", "C:\\x\\a.bat %1", '"C:\\x\\a.bat" %1', '"no exe here"', '"'):
            self.assertIsNone(server.parse_command_exe(command), command)


class IconResponseTest(unittest.TestCase):
    def setUp(self):
        self.base_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base_dir, True)
        self.logos = os.path.join(self.base_dir, "logos")
        self.cache = os.path.join(self.base_dir, "cache")

    def write(self, directory, name, content):
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "wb") as f:
            f.write(content)

    def cache_name(self, bid, target):
        return "%s-%s.png" % (bid, hashlib.sha1(target.encode("utf-8")).hexdigest()[:10])

    def test_precedence(self):
        btn = make_button()
        extractor = FakeExtractor()
        self.write(self.cache, self.cache_name("app", btn["target"]), b"cached")
        self.write(self.logos, "app.svg", b"<svg/>")
        self.write(self.logos, "app.png", b"logo-png")
        self.assertEqual(server.icon_response(self.base_dir, btn, extractor),
                         ("image/png", b"logo-png"))
        os.remove(os.path.join(self.logos, "app.png"))
        self.assertEqual(server.icon_response(self.base_dir, btn, extractor),
                         ("image/svg+xml", b"<svg/>"))
        os.remove(os.path.join(self.logos, "app.svg"))
        self.assertEqual(server.icon_response(self.base_dir, btn, extractor),
                         ("image/png", b"cached"))
        os.remove(os.path.join(self.cache, self.cache_name("app", btn["target"])))
        content_type, body = server.icon_response(self.base_dir, btn, extractor)
        self.assertEqual(content_type, "image/png")
        self.assertEqual(decode_rgba(body), extractor.result)
        self.assertEqual(extractor.targets, [btn["target"]])

    def test_extractor_result_is_cached(self):
        btn = make_button()
        extractor = FakeExtractor()
        first = server.icon_response(self.base_dir, btn, extractor)
        second = server.icon_response(self.base_dir, btn, extractor)
        self.assertEqual(first, second)
        self.assertEqual(len(extractor.targets), 1)
        self.assertEqual(os.listdir(self.cache), [self.cache_name("app", btn["target"])])

    def test_changed_target_calls_extractor_again(self):
        extractor = FakeExtractor()
        server.icon_response(self.base_dir, make_button(target="C:\\a.exe"), extractor)
        server.icon_response(self.base_dir, make_button(target="C:\\b.exe"), extractor)
        self.assertEqual(extractor.targets, ["C:\\a.exe", "C:\\b.exe"])
        self.assertEqual(len(os.listdir(self.cache)), 2)

    def test_extractor_failures_give_fallback(self):
        for extractor in (FakeExtractor(result=None), FakeExtractor(error=RuntimeError("boom")),
                          FakeExtractor(error=OSError("denied")),
                          FakeExtractor(result=(2, 2, b"short"))):
            content_type, body = server.icon_response(self.base_dir, make_button(), extractor)
            self.assertEqual(content_type, "image/png")
            self.assertEqual(decode_rgba(body)[:2], (128, 128))
            self.assertEqual(len(extractor.targets), 1)
        self.assertFalse(os.path.exists(self.cache))

    def test_null_extractor_gives_fallback(self):
        _, body = server.icon_response(self.base_dir, make_button(), server.NullExtractor())
        self.assertEqual(body, server.fallback_png("app"))

    def test_invalid_id_gives_fallback_without_touching_paths(self):
        self.write(self.logos, "x.png", b"logo-png")
        extractor = FakeExtractor()
        for bid in ("../logos/x", "../x", "a/b", "A", "", "x.png"):
            btn = make_button(bid)
            self.assertEqual(server.icon_response(self.base_dir, btn, extractor),
                             ("image/png", server.fallback_png(bid)), bid)
        self.assertEqual(extractor.targets, [])
        self.assertFalse(os.path.exists(self.cache))

    def test_fallback_is_flat_deterministic_and_readable(self):
        data = server.fallback_png("steam")
        self.assertEqual(data, server.fallback_png("steam"))
        width, height, pixels = decode_rgba(data)
        self.assertEqual((width, height), (128, 128))
        self.assertEqual(len(set(pixels[i:i + 4] for i in range(0, len(pixels), 4))), 1)
        self.assertTrue(all(60 <= channel <= 200 for channel in pixels[:3]))
        self.assertEqual(pixels[3], 255)

    def test_fallback_differs_per_id(self):
        colours = {server.fallback_png(bid) for bid in ("steam", "discord", "simhub", "simmotion")}
        self.assertGreaterEqual(len(colours), 2)
        self.assertNotEqual(server.fallback_png("steam"), server.fallback_png("discord"))


class DeckIconTest(unittest.TestCase):
    def test_corner_is_background_and_size_matches(self):
        for size in (180, 192, 512):
            width, height, pixels = decode_rgba(server.deck_icon_png(size))
            self.assertEqual((width, height), (size, size))
            self.assertEqual(pixels[:4], b"\x0b\x0d\x10\xff")
            self.assertEqual(pixels[-4:], b"\x0b\x0d\x10\xff")

    def test_accent_squares(self):
        size = 512
        _, _, pixels = decode_rgba(server.deck_icon_png(size))
        margin = int(size * 0.18)
        gap = int(size * 0.08)
        cell = (size - 2 * margin - gap) // 2
        starts = (margin, margin + cell + gap)
        expected = ((0x1e, 0x90, 0xff), (0x2e, 0xcc, 0x71), (0xff, 0x9f, 0x1a), (0xe7, 0x4c, 0x3c))
        for index, colour in enumerate(expected):
            x = starts[index % 2] + cell // 2
            y = starts[index // 2] + cell // 2
            offset = (y * size + x) * 4
            self.assertEqual(tuple(pixels[offset:offset + 3]), colour)
        gap_offset = (starts[0] + cell // 2 + size * (margin + cell + gap // 2)) * 4
        self.assertEqual(pixels[gap_offset:gap_offset + 4], b"\x0b\x0d\x10\xff")


class IconEndpointTest(unittest.TestCase):
    def setUp(self):
        self.base_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base_dir, True)
        self.extractor = FakeExtractor()
        self.start(server.is_loopback)

    def start(self, loopback_check):
        self.httpd = server.create_server(self.base_dir, loopback_check=loopback_check,
                                          extractor=self.extractor)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp, resp.read()
        finally:
            conn.close()

    def test_icon_served(self):
        status, resp, body = self.get("/api/icon/steam")
        self.assertEqual(status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "image/png")
        self.assertEqual(resp.getheader("Cache-Control"), "no-cache")
        self.assertEqual(body[:8], SIGNATURE)
        self.assertEqual(decode_rgba(body), self.extractor.result)
        self.assertEqual(self.extractor.targets, ["steam://open/main"])

    def test_logo_override_served(self):
        os.makedirs(os.path.join(self.base_dir, "logos"))
        with open(os.path.join(self.base_dir, "logos", "steam.svg"), "wb") as f:
            f.write(b"<svg/>")
        status, resp, body = self.get("/api/icon/steam")
        self.assertEqual((status, resp.getheader("Content-Type"), body),
                         (200, "image/svg+xml", b"<svg/>"))
        self.assertEqual(self.extractor.targets, [])

    def test_fallback_served_when_extractor_fails(self):
        self.extractor.error = RuntimeError("boom")
        status, resp, body = self.get("/api/icon/discord")
        self.assertEqual(status, 200)
        self.assertEqual(body, server.fallback_png("discord"))

    def test_served_to_remote_client(self):
        self.stop_server()
        self.start(lambda ip: False)
        status, resp, body = self.get("/api/icon/steam")
        self.assertEqual(status, 200)
        self.assertEqual(body[:8], SIGNATURE)

    def test_deck_icons_served(self):
        for path, size in (("/apple-touch-icon.png", 180), ("/icon-192.png", 192),
                           ("/icon-512.png", 512)):
            status, resp, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(resp.getheader("Content-Type"), "image/png", path)
            self.assertEqual(body[:8], SIGNATURE, path)
            width, height = struct.unpack(">II", body[16:24])
            self.assertEqual((width, height), (size, size), path)
            self.assertEqual(body, server.deck_icon_png(size), path)

    def test_deck_icons_served_to_remote_client(self):
        self.stop_server()
        self.start(lambda ip: False)
        status, resp, body = self.get("/icon-192.png")
        self.assertEqual((status, body[:8]), (200, SIGNATURE))

    def test_other_icon_sizes_not_served(self):
        for path in ("/icon-256.png", "/icon-192.png/x", "/apple-touch-icon-precomposed.png"):
            status, resp, body = self.get(path)
            self.assertEqual(status, 404, path)

    def test_unknown_id(self):
        status, resp, body = self.get("/api/icon/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body.decode("utf-8")),
                         {"ok": False, "message": "unknown button"})

    def test_path_tricks_are_unknown_buttons(self):
        for path in ("/api/icon/..%2f..%2fserver.py", "/api/icon/../server.py",
                     "/api/icon/..%5c..%5cserver.py", "/api/icon/", "/api/icon/steam%2f..%2f"):
            status, resp, body = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertEqual(json.loads(body.decode("utf-8"))["ok"], False, path)
        self.assertEqual(self.extractor.targets, [])


if __name__ == "__main__":
    unittest.main()
