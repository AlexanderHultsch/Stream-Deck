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


class FakePlatform:
    def __init__(self, running=False, focus_result=True, launch_error=None,
                 running_error=None, focus_error=None):
        self.running = running
        self.focus_result = focus_result
        self.launch_error = launch_error
        self.running_error = running_error
        self.focus_error = focus_error
        self.calls = []

    def is_running(self, process):
        self.calls.append(("is_running", process))
        if self.running_error is not None:
            raise self.running_error
        return self.running

    def focus(self, process):
        self.calls.append(("focus", process))
        if self.focus_error is not None:
            raise self.focus_error
        return self.focus_result

    def launch(self, target, window):
        self.calls.append(("launch", target, window))
        if self.launch_error is not None:
            raise self.launch_error


def make_button(**extra):
    item = {"id": "app", "name": "App", "target": "C:\\app.exe", "process": "app.exe",
            "window": "normal", "ifRunning": "focus"}
    item.update(extra)
    return item


LAUNCHED = {"ok": True, "action": "launched", "message": "Launched App"}
FOCUSED = {"ok": True, "action": "focused", "message": "Focused App"}
SKIPPED = {"ok": True, "action": "skipped", "message": "App is already running"}


class PressButtonTest(unittest.TestCase):
    def test_not_running_launches(self):
        for mode in server.IF_RUNNING:
            platform = FakePlatform(running=False)
            result = server.press_button(make_button(ifRunning=mode, window="maximized"), platform)
            self.assertEqual(result, LAUNCHED, mode)
            self.assertEqual(platform.calls, [("is_running", "app.exe"),
                                              ("launch", "C:\\app.exe", "maximized")], mode)

    def test_running_focus_succeeds(self):
        platform = FakePlatform(running=True, focus_result=True)
        result = server.press_button(make_button(ifRunning="focus"), platform)
        self.assertEqual(result, FOCUSED)
        self.assertEqual(platform.calls, [("is_running", "app.exe"), ("focus", "app.exe")])

    def test_running_focus_fails_falls_back_to_launch(self):
        platform = FakePlatform(running=True, focus_result=False)
        result = server.press_button(make_button(ifRunning="focus", window="minimized"), platform)
        self.assertEqual(result, LAUNCHED)
        self.assertEqual(platform.calls, [("is_running", "app.exe"), ("focus", "app.exe"),
                                          ("launch", "C:\\app.exe", "minimized")])

    def test_running_new_launches_without_focus(self):
        for focus_result in (True, False):
            platform = FakePlatform(running=True, focus_result=focus_result)
            result = server.press_button(make_button(ifRunning="new"), platform)
            self.assertEqual(result, LAUNCHED)
            self.assertEqual(platform.calls, [("is_running", "app.exe"),
                                              ("launch", "C:\\app.exe", "normal")])

    def test_running_nothing_skips(self):
        for focus_result in (True, False):
            platform = FakePlatform(running=True, focus_result=focus_result)
            result = server.press_button(make_button(ifRunning="nothing"), platform)
            self.assertEqual(result, SKIPPED)
            self.assertEqual(platform.calls, [("is_running", "app.exe")])

    def test_empty_target_is_error(self):
        for target in ("", None):
            platform = FakePlatform(running=True)
            result = server.press_button(make_button(target=target), platform)
            self.assertEqual(result, {"ok": False, "action": "error",
                                      "message": "App is not configured"})
            self.assertEqual(platform.calls, [])

    def test_empty_process_launches_regardless_of_running(self):
        for mode in server.IF_RUNNING:
            platform = FakePlatform(running=True)
            result = server.press_button(make_button(process="", ifRunning=mode), platform)
            self.assertEqual(result, LAUNCHED, mode)
            self.assertNotIn(("focus", ""), platform.calls)
            self.assertEqual(platform.calls[-1], ("launch", "C:\\app.exe", "normal"), mode)

    def test_launch_oserror_is_error_result(self):
        platform = FakePlatform(launch_error=OSError("file not found"))
        result = server.press_button(make_button(), platform)
        self.assertEqual(result, {"ok": False, "action": "error",
                                  "message": "Could not start App: file not found"})

    def test_launch_oserror_on_focus_fallback(self):
        platform = FakePlatform(running=True, focus_result=False,
                                launch_error=FileNotFoundError("gone"))
        result = server.press_button(make_button(ifRunning="focus"), platform)
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "error")
        self.assertEqual(result["message"], "Could not start App: gone")

    def test_is_running_oserror_is_error_result(self):
        platform = FakePlatform(running_error=OSError("tasklist missing"))
        result = server.press_button(make_button(), platform)
        self.assertEqual(result, {"ok": False, "action": "error",
                                  "message": "Could not check App: tasklist missing"})
        self.assertEqual(platform.calls, [("is_running", "app.exe")])

    def test_focus_oserror_is_error_result(self):
        platform = FakePlatform(running=True, focus_error=OSError("access denied"))
        result = server.press_button(make_button(ifRunning="focus"), platform)
        self.assertEqual(result, {"ok": False, "action": "error",
                                  "message": "Could not check App: access denied"})
        self.assertEqual(platform.calls, [("is_running", "app.exe"), ("focus", "app.exe")])

    def test_uri_target_passed_through(self):
        platform = FakePlatform()
        server.press_button(make_button(target="steam://open/main", process="steam.exe"), platform)
        self.assertEqual(platform.calls[-1], ("launch", "steam://open/main", "normal"))


class HelpersTest(unittest.TestCase):
    def test_is_uri(self):
        self.assertTrue(server.is_uri("steam://open/main"))
        self.assertTrue(server.is_uri("discord://"))
        self.assertFalse(server.is_uri("C:\\x\\a.exe"))
        self.assertFalse(server.is_uri("/a/b"))
        self.assertFalse(server.is_uri(""))

    def test_show_command(self):
        self.assertEqual(server.show_command("normal"), 1)
        self.assertEqual(server.show_command("minimized"), 2)
        self.assertEqual(server.show_command("maximized"), 3)


class PlatformTest(unittest.TestCase):
    def test_dry_run_platform(self):
        platform = server.DryRunPlatform()
        self.assertFalse(platform.is_running("anything.exe"))
        self.assertFalse(platform.focus("anything.exe"))
        self.assertEqual(platform.launched, [])
        self.assertIsNone(platform.launch("a.exe", "maximized"))
        self.assertIsNone(platform.launch("steam://x", "normal"))
        self.assertEqual(platform.launched, [("a.exe", "maximized"), ("steam://x", "normal")])

    def test_default_platform_on_this_system(self):
        if sys.platform == "win32":
            self.assertIsInstance(server.default_platform(), server.WindowsPlatform)
        else:
            self.assertIsInstance(server.default_platform(), server.DryRunPlatform)

    def test_windows_platform_importable(self):
        self.assertTrue(hasattr(server.WindowsPlatform(), "launch"))


class PressHttpTest(unittest.TestCase):
    def start(self, platform, buttons):
        base_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base_dir, True)
        with open(os.path.join(base_dir, "buttons.json"), "w", encoding="utf-8") as f:
            json.dump({"port": 8765, "buttons": buttons}, f)
        httpd = server.create_server(base_dir, platform=platform)
        thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        self.addCleanup(self.stop, httpd, thread)
        self.port = httpd.server_address[1]
        return httpd

    @staticmethod
    def stop(httpd, thread):
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)

    def post(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", path)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    def test_platform_attribute(self):
        platform = FakePlatform()
        self.assertIs(self.start(platform, [make_button()]).platform, platform)

    def test_launched(self):
        platform = FakePlatform(running=False)
        self.start(platform, [make_button()])
        self.assertEqual(self.post("/api/press/app"), (200, LAUNCHED))
        self.assertEqual(platform.calls[-1], ("launch", "C:\\app.exe", "normal"))

    def test_focused(self):
        platform = FakePlatform(running=True, focus_result=True)
        self.start(platform, [make_button()])
        self.assertEqual(self.post("/api/press/app"), (200, FOCUSED))

    def test_skipped(self):
        platform = FakePlatform(running=True)
        self.start(platform, [make_button(ifRunning="nothing")])
        self.assertEqual(self.post("/api/press/app"), (200, SKIPPED))

    def test_error_results_are_http_200(self):
        platform = FakePlatform(launch_error=OSError("boom"))
        self.start(platform, [make_button(), make_button(id="empty", target="")])
        self.assertEqual(self.post("/api/press/app"), (200, {
            "ok": False, "action": "error", "message": "Could not start App: boom"}))
        self.assertEqual(self.post("/api/press/empty"), (200, {
            "ok": False, "action": "error", "message": "App is not configured"}))

    def test_running_check_oserror_is_http_200(self):
        platform = FakePlatform(running_error=OSError("boom"))
        self.start(platform, [make_button()])
        self.assertEqual(self.post("/api/press/app"), (200, {
            "ok": False, "action": "error", "message": "Could not check App: boom"}))

    def test_unknown_id_is_404(self):
        platform = FakePlatform()
        self.start(platform, [make_button()])
        self.assertEqual(self.post("/api/press/nope"), (404, {
            "ok": False, "action": "error", "message": "unknown button"}))
        self.assertEqual(platform.calls, [])


if __name__ == "__main__":
    unittest.main()
