import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def button(name, **extra):
    item = {"name": name, "target": "", "process": "", "window": "normal", "ifRunning": "focus"}
    item.update(extra)
    return item


def padded(items):
    return list(items) + [None] * (server.SLOTS - len(items))


class ServerTestCase(unittest.TestCase):
    loopback_check = staticmethod(server.is_loopback)

    def setUp(self):
        self.base_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base_dir, True)
        self.httpd = server.create_server(self.base_dir, loopback_check=self.loopback_check)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def request(self, method, path, body=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body)
            resp = conn.getresponse()
            return resp.status, resp.getheader("Content-Type"), resp.read()
        finally:
            conn.close()

    def json_request(self, method, path, body=None):
        status, _, raw = self.request(method, path, body)
        return status, json.loads(raw.decode("utf-8"))

    def put(self, buttons):
        return self.json_request("PUT", "/api/buttons", {"buttons": buttons})

    def saved_config(self):
        with open(os.path.join(self.base_dir, "buttons.json"), encoding="utf-8") as f:
            return json.load(f)

    def write_file(self, name, content):
        with open(os.path.join(self.base_dir, name), "w", encoding="utf-8") as f:
            f.write(content)


class IsLoopbackTest(unittest.TestCase):
    def test_loopback_addresses(self):
        for ip in ("127.0.0.1", "127.5.5.5", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(server.is_loopback(ip), ip)

    def test_non_loopback_addresses(self):
        for ip in ("192.168.1.5", "10.0.0.2", "::ffff:192.168.1.5", "fe80::1", "garbage"):
            self.assertFalse(server.is_loopback(ip), ip)


class ConfigTest(ServerTestCase):
    def test_defaults_created_and_served(self):
        status, data = self.json_request("GET", "/api/buttons")
        self.assertEqual(status, 200)
        self.assertEqual(list(data), ["buttons"])
        self.assertEqual(len(data["buttons"]), 8)
        self.assertEqual([b["id"] for b in data["buttons"][:4]],
                         ["steam", "discord", "simhub", "simmotion"])
        self.assertEqual(data["buttons"][4:], [None] * 4)
        self.assertEqual(data["buttons"], padded(server.DEFAULT_BUTTONS))
        config = self.saved_config()
        self.assertEqual(config["port"], 8765)
        self.assertEqual(config["buttons"], padded(server.DEFAULT_BUTTONS))

    def test_default_values(self):
        buttons = {b["id"]: b for b in server.DEFAULT_BUTTONS}
        self.assertEqual(buttons["steam"]["target"], "steam://open/main")
        self.assertEqual(buttons["steam"]["process"], "steam.exe")
        self.assertEqual(buttons["discord"]["target"], "discord://")
        self.assertEqual(buttons["discord"]["process"], "Discord.exe")
        self.assertEqual(buttons["simhub"]["target"],
                         "C:\\Program Files (x86)\\SimHub\\SimHubWPF.exe")
        self.assertEqual(buttons["simhub"]["process"], "SimHubWPF.exe")
        self.assertEqual(buttons["simmotion"]["target"], "")
        self.assertEqual(buttons["simmotion"]["process"], "")
        for b in buttons.values():
            self.assertEqual(b["window"], "normal")
            self.assertEqual(b["ifRunning"], "focus")

    def test_existing_config_not_overwritten(self):
        self.stop_server()
        self.write_file("buttons.json", json.dumps({"port": 9000, "buttons": []}))
        self.httpd = server.create_server(self.base_dir)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()
        status, data = self.json_request("GET", "/api/buttons")
        self.assertEqual((status, data), (200, {"buttons": [None] * 8}))

    def restart_with(self, config):
        self.stop_server()
        self.write_file("buttons.json", json.dumps(config))
        self.httpd = server.create_server(self.base_dir)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()

    def test_short_stored_file_padded_with_nulls(self):
        stored = [button("A", id="a"), None, button("B", id="b")]
        self.restart_with({"port": 9000, "buttons": stored})
        status, data = self.json_request("GET", "/api/buttons")
        self.assertEqual(status, 200)
        self.assertEqual(data["buttons"], padded(stored))
        self.assertEqual(self.saved_config()["buttons"], stored)

    def test_long_stored_file_truncated_to_slots(self):
        stored = [button("B%d" % i, id="b%d" % i) for i in range(10)]
        self.restart_with({"port": 9000, "buttons": stored})
        status, data = self.json_request("GET", "/api/buttons")
        self.assertEqual(status, 200)
        self.assertEqual(data["buttons"], stored[:8])


class PutTest(ServerTestCase):
    def test_valid_put_saved_and_port_preserved(self):
        self.write_file("buttons.json", json.dumps({"port": 9123, "buttons": []}))
        buttons = [button("Alpha", id="alpha", target="alpha://", window="maximized",
                          ifRunning="new")]
        status, data = self.put(buttons)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["buttons"], padded([{
            "id": "alpha", "name": "Alpha", "target": "alpha://", "process": "",
            "window": "maximized", "ifRunning": "new"}]))
        config = self.saved_config()
        self.assertEqual(config["port"], 9123)
        self.assertEqual(config["buttons"], data["buttons"])
        status, got = self.json_request("GET", "/api/buttons")
        self.assertEqual(got["buttons"], data["buttons"])

    def test_put_leaves_no_temp_files(self):
        self.put([button("A", id="a")])
        self.assertEqual(sorted(os.listdir(self.base_dir)), ["buttons.json"])

    def test_8_buttons_ok(self):
        status, data = self.put([button("B%d" % i) for i in range(8)])
        self.assertEqual(status, 200)
        self.assertEqual(len(data["buttons"]), 8)
        self.assertTrue(all(data["buttons"]))

    def test_empty_list_gives_8_null_slots(self):
        status, data = self.put([])
        self.assertEqual((status, data), (200, {"ok": True, "buttons": [None] * 8}))
        self.assertEqual(self.saved_config()["buttons"], [None] * 8)

    def test_nulls_keep_their_positions(self):
        sent = [button("A", id="a"), None, None, button("D", id="d"), None, button("F", id="f")]
        status, data = self.put(sent)
        self.assertEqual(status, 200)
        positions = [b["id"] if b else None for b in data["buttons"]]
        self.assertEqual(positions, ["a", None, None, "d", None, "f", None, None])
        self.assertEqual(self.saved_config()["buttons"], data["buttons"])
        status, got = self.json_request("GET", "/api/buttons")
        self.assertEqual(got["buttons"], data["buttons"])
        status, data = self.put(got["buttons"])
        self.assertEqual(status, 200)
        self.assertEqual(data["buttons"], got["buttons"])

    def test_full_8_slot_list_with_nulls_ok(self):
        sent = [None, button("B"), None, None, None, None, None, button("H")]
        status, data = self.put(sent)
        self.assertEqual(status, 200)
        self.assertEqual([b["id"] if b else None for b in data["buttons"]],
                         [None, "b", None, None, None, None, None, "h"])

    def assert_rejected(self, body):
        before = self.saved_config()
        status, data = self.json_request("PUT", "/api/buttons", body)
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertTrue(data["message"])
        self.assertEqual(self.saved_config(), before)

    def test_bad_window(self):
        self.assert_rejected({"buttons": [button("A", window="huge")]})

    def test_bad_if_running(self):
        self.assert_rejected({"buttons": [button("A", ifRunning="always")]})

    def test_empty_name(self):
        self.assert_rejected({"buttons": [button("   ")]})

    def test_name_too_long(self):
        self.assert_rejected({"buttons": [button("x" * 41)]})

    def test_duplicate_id(self):
        self.assert_rejected({"buttons": [button("A", id="same"), button("B", id="same")]})

    def test_invalid_id(self):
        self.assert_rejected({"buttons": [button("A", id="Bad ID")]})

    def test_too_many_slots(self):
        self.assert_rejected({"buttons": [button("B%d" % i) for i in range(9)]})
        self.assert_rejected({"buttons": [None] * 9})

    def test_too_many_slots_message(self):
        status, data = self.put([button("B%d" % i) for i in range(9)])
        self.assertEqual(status, 400)
        self.assertEqual(data["message"], "too many slots (max 8)")

    def test_entry_must_be_object_or_null(self):
        for bad in ("x", 5, True, [], [None]):
            before = self.saved_config()
            status, data = self.put([button("A"), bad])
            self.assertEqual(status, 400, bad)
            self.assertEqual(data["message"], "each slot must be an object or null")
            self.assertEqual(self.saved_config(), before)

    def test_null_slots_do_not_hide_validation_errors(self):
        self.assert_rejected({"buttons": [None, button("A", window="huge"), None]})
        self.assert_rejected({"buttons": [None, button("A", id="same"), None,
                                          button("B", id="same")]})

    def test_non_json_body(self):
        self.assert_rejected("this is not json")

    def test_wrong_shape(self):
        self.assert_rejected({"nope": []})
        self.assert_rejected({"buttons": "x"})
        self.assert_rejected({"buttons": ["x"]})
        self.assert_rejected([])

    def test_non_string_field(self):
        self.assert_rejected({"buttons": [button("A", target=5)]})

    def test_oversized_body(self):
        pad = "x" * (server.MAX_BODY + 1000)
        self.assert_rejected({"buttons": [button("A", target=pad)]})

    def test_id_generated_from_name_and_deduplicated(self):
        status, data = self.put([
            button("My App"), button("My App"), button("My App"), button("!!!")])
        self.assertEqual(status, 200)
        self.assertEqual([b["id"] for b in data["buttons"][:4]],
                         ["my-app", "my-app-2", "my-app-3", "button"])
        self.assertEqual(data["buttons"][4:], [None] * 4)

    def test_generated_id_avoids_explicit_id_later_in_list(self):
        status, data = self.put([button("My App"), button("Other", id="my-app")])
        self.assertEqual(status, 200)
        self.assertEqual([b["id"] for b in data["buttons"][:2]], ["my-app-2", "my-app"])

    def test_generated_id_avoids_ids_in_later_slots_past_nulls(self):
        status, data = self.put([
            button("My App"), None, None, button("Other", id="my-app"), None,
            button("Third", id="my-app-2")])
        self.assertEqual(status, 200)
        self.assertEqual([b["id"] if b else None for b in data["buttons"]],
                         ["my-app-3", None, None, "my-app", None, "my-app-2", None, None])

    def test_process_derived_from_exe_target(self):
        status, data = self.put([
            button("A", target="C:\\Games\\Foo\\Bar.EXE"),
            button("B", target="/opt/tools/baz.exe"),
            button("C", target="C:\\Games\\Foo\\Bar.exe", process="custom.exe"),
            button("D", target="discord://"),
            button("E", target="C:\\Tools\\script.bat"),
        ])
        self.assertEqual(status, 200)
        self.assertEqual([b["process"] for b in data["buttons"][:5]],
                         ["Bar.EXE", "baz.exe", "custom.exe", "", ""])


class RemoteClientTest(ServerTestCase):
    loopback_check = staticmethod(lambda ip: False)

    def test_put_forbidden(self):
        before = self.saved_config()
        status, data = self.put([button("A", id="a")])
        self.assertEqual(status, 403)
        self.assertEqual(data, {"ok": False, "message": "forbidden"})
        self.assertEqual(self.saved_config(), before)

    def test_edit_forbidden(self):
        self.write_file("edit.html", "<html>edit</html>")
        status, data = self.json_request("GET", "/edit")
        self.assertEqual(status, 403)
        self.assertEqual(data, {"ok": False, "message": "forbidden"})

    def test_get_buttons_allowed(self):
        status, data = self.json_request("GET", "/api/buttons")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["buttons"]), 8)

    def test_press_allowed(self):
        status, data = self.json_request("POST", "/api/press/steam")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])


class LoopbackClientTest(ServerTestCase):
    def test_edit_served_to_loopback(self):
        self.write_file("edit.html", "<html>edit</html>")
        status, ctype, body = self.request("GET", "/edit")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/html; charset=utf-8")
        self.assertEqual(body, b"<html>edit</html>")

    def test_edit_missing_is_404(self):
        status, _, _ = self.request("GET", "/edit")
        self.assertEqual(status, 404)


class PressTest(ServerTestCase):
    def test_known_id(self):
        status, data = self.json_request("POST", "/api/press/steam")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True, "action": "launched", "message": "Launched Steam"})

    def test_unknown_id(self):
        status, data = self.json_request("POST", "/api/press/nope")
        self.assertEqual(status, 404)
        self.assertEqual(data, {"ok": False, "action": "error", "message": "unknown button"})

    def test_known_id_with_null_slots_around(self):
        self.put([None, button("Alpha", id="alpha", target="a://"), None, None,
                  button("Beta", id="beta", target="b://")])
        status, data = self.json_request("POST", "/api/press/beta")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True, "action": "launched", "message": "Launched Beta"})
        status, data = self.json_request("POST", "/api/press/alpha")
        self.assertEqual((status, data["ok"]), (200, True))

    def test_unknown_id_with_null_slots(self):
        self.put([None, button("Alpha", id="alpha"), None])
        status, data = self.json_request("POST", "/api/press/nope")
        self.assertEqual(status, 404)
        self.assertEqual(data, {"ok": False, "action": "error", "message": "unknown button"})


class StaticTest(ServerTestCase):
    def test_server_and_config_not_served(self):
        self.write_file("server.py", "print('secret')")
        self.write_file("SPEC.md", "# spec")
        for path in ("/server.py", "/buttons.json", "/SPEC.md", "/tests", "/../server.py",
                     "/%2e%2e/server.py"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 404, path)

    def test_whitelisted_file_content_types(self):
        expected = {
            "style.css": "text/css",
            "app.js": "text/javascript",
            "edit.js": "text/javascript",
            "manifest.webmanifest": "application/manifest+json",
        }
        for name, ctype in expected.items():
            self.write_file(name, "content of " + name)
            status, got_type, body = self.request("GET", "/" + name)
            self.assertEqual(status, 200, name)
            self.assertEqual(got_type, ctype, name)
            self.assertEqual(body, ("content of " + name).encode("utf-8"))

    def test_missing_whitelisted_file_is_404(self):
        for name in ("style.css", "app.js", "edit.js", "manifest.webmanifest", "index.html"):
            status, _, _ = self.request("GET", "/" + name)
            self.assertEqual(status, 404, name)
        status, _, _ = self.request("GET", "/")
        self.assertEqual(status, 404)

    def test_root_serves_index(self):
        self.write_file("index.html", "<html>home</html>")
        status, ctype, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/html; charset=utf-8")
        self.assertEqual(body, b"<html>home</html>")

    def test_query_string_ignored(self):
        self.write_file("style.css", "body{}")
        status, ctype, _ = self.request("GET", "/style.css?v=2")
        self.assertEqual((status, ctype), (200, "text/css"))


if __name__ == "__main__":
    unittest.main()
