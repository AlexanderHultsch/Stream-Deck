import contextlib
import ctypes
import http.client
import inspect
import io
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
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402
import test_ws  # noqa: E402

TIMEOUT = 5
STATUS_PATH = "/ws/status"
DEFAULT_IDS = {"steam", "discord", "simhub"}


def wait_for(predicate, timeout=TIMEOUT):
    return test_ws.wait_for(predicate, timeout)


class CountingSource(server.DryRunProcessSource):
    def __init__(self, processes=None):
        super().__init__()
        self.processes = list(processes or [])
        self.error = None
        self.count_lock = threading.Lock()
        self.count = 0

    @property
    def calls(self):
        with self.count_lock:
            return self.count

    def snapshot(self):
        with self.count_lock:
            self.count += 1
        if self.error is not None:
            raise self.error
        return super().snapshot()


class ToggleSource(CountingSource):
    def snapshot(self):
        super().snapshot()
        return [("steam.exe", 1)] if self.calls % 2 else []


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, changes):
        self.calls.append(changes)


class Raiser:
    def __init__(self):
        self.count = 0

    def __call__(self, changes):
        self.count += 1
        raise RuntimeError("boom")


def settle(seconds):
    time.sleep(seconds)


class DryRunProcessSourceTest(unittest.TestCase):
    def test_default_is_empty(self):
        self.assertEqual(server.DryRunProcessSource().snapshot(), [])

    def test_snapshot_returns_a_copy(self):
        source = server.DryRunProcessSource()
        source.processes = [("a.exe", 1)]
        first = source.snapshot()
        self.assertEqual(first, [("a.exe", 1)])
        first.append(("b.exe", 2))
        self.assertEqual(source.processes, [("a.exe", 1)])
        source.processes.append(("c.exe", 3))
        self.assertEqual(first, [("a.exe", 1), ("b.exe", 2)])
        self.assertEqual(source.snapshot(), [("a.exe", 1), ("c.exe", 3)])

    def test_default_process_source_on_this_system(self):
        if sys.platform != "win32":
            self.assertIsInstance(server.default_process_source(), server.DryRunProcessSource)

    def test_default_process_source_on_windows(self):
        with mock.patch.object(server.sys, "platform", "win32"):
            self.assertIsInstance(server.default_process_source(), server.WindowsProcessSource)


class FakeToolhelp:
    def __init__(self, entries, handle=0x1234, fail_at=None, first_result=None):
        self.entries = entries
        self.handle = handle
        self.fail_at = fail_at
        self.first_result = first_result
        self.index = 0
        self.created = []
        self.closed = []
        self.sizes = []

    def fill(self, pointer):
        entry = pointer.contents
        name, pid = self.entries[self.index]
        entry.th32ProcessID = pid
        data = (name + "\0").encode("utf-16-le")
        ctypes.memmove(ctypes.addressof(entry.szExeFile), data, len(data))

    def CreateToolhelp32Snapshot(self, flags, pid):
        self.created.append((flags, pid))
        return self.handle

    def Process32FirstW(self, handle, pointer):
        self.sizes.append(pointer.contents.dwSize)
        self.index = 0
        if self.first_result is not None:
            return self.first_result
        if not self.entries:
            return 0
        self.fill(pointer)
        return 1

    def Process32NextW(self, handle, pointer):
        self.index += 1
        if self.fail_at == self.index:
            raise RuntimeError("boom")
        if self.index >= len(self.entries):
            return 0
        self.fill(pointer)
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


def fake_win_error(code=None):
    return OSError(code, "fake win error")


class WindowsProcessSourceTest(unittest.TestCase):
    def make(self, **kwargs):
        entries = kwargs.pop("entries")
        fake = FakeToolhelp(entries, **kwargs)
        source = server.WindowsProcessSource()
        source.api = (fake, source.structures())
        return source, fake

    def snapshot_with_errors(self, source):
        with mock.patch.object(ctypes, "WinError", fake_win_error, create=True), \
                mock.patch.object(ctypes, "get_last_error", lambda: 5, create=True):
            return source.snapshot()

    def test_instantiable_off_windows(self):
        self.assertIsNone(server.WindowsProcessSource().api)

    def test_struct_layout(self):
        entry = server.WindowsProcessSource().structures()
        wide = ctypes.sizeof(ctypes.c_void_p) == 8
        self.assertEqual(ctypes.sizeof(entry), 568 if wide else 556)
        self.assertEqual(entry.th32ProcessID.offset, 8)
        self.assertEqual(entry.th32DefaultHeapID.offset, 16 if wide else 12)
        self.assertEqual(entry.th32ParentProcessID.offset, 32 if wide else 24)
        self.assertEqual(entry.szExeFile.offset, 44 if wide else 36)
        self.assertEqual(entry.szExeFile.size, 520)

    def test_iterates_all_entries_lowercased(self):
        source, fake = self.make(entries=[("System", 4), ("Discord.EXE", 1234),
                                          ("a.exe", 7), ("\u00dcnic\u00f6de.exe", 99)])
        self.assertEqual(self.snapshot_with_errors(source),
                         [("system", 4), ("discord.exe", 1234), ("a.exe", 7),
                          ("\u00fcnic\u00f6de.exe", 99)])

    def test_shorter_name_after_longer_has_no_leftovers(self):
        source, fake = self.make(entries=[("VeryLongProcessName.exe", 1), ("x.exe", 2)])
        self.assertEqual(self.snapshot_with_errors(source),
                         [("verylongprocessname.exe", 1), ("x.exe", 2)])

    def test_snapshot_flags_size_and_close(self):
        source, fake = self.make(entries=[("a.exe", 1), ("b.exe", 2)])
        self.snapshot_with_errors(source)
        self.assertEqual(fake.created, [(server.TH32CS_SNAPPROCESS, 0)])
        self.assertEqual(server.TH32CS_SNAPPROCESS, 0x2)
        self.assertEqual(fake.sizes, [ctypes.sizeof(source.structures())])
        self.assertEqual(fake.closed, [0x1234])

    def test_empty_snapshot(self):
        source, fake = self.make(entries=[])
        self.assertEqual(self.snapshot_with_errors(source), [])
        self.assertEqual(fake.closed, [0x1234])

    def test_handle_closed_when_iteration_raises(self):
        source, fake = self.make(entries=[("a.exe", 1), ("b.exe", 2), ("c.exe", 3)], fail_at=2)
        with self.assertRaises(RuntimeError):
            self.snapshot_with_errors(source)
        self.assertEqual(fake.closed, [0x1234])

    def test_invalid_handle_value_raises_oserror(self):
        invalid = ctypes.c_void_p(-1).value
        for handle in (invalid, 0, None):
            source, fake = self.make(entries=[("a.exe", 1)], handle=handle)
            with self.assertRaises(OSError):
                self.snapshot_with_errors(source)
            self.assertEqual(fake.closed, [])
            self.assertEqual(fake.sizes, [])

    def test_repeated_snapshots_use_fresh_handles(self):
        source, fake = self.make(entries=[("a.exe", 1)])
        self.snapshot_with_errors(source)
        self.snapshot_with_errors(source)
        self.assertEqual(len(fake.created), 2)
        self.assertEqual(fake.closed, [0x1234, 0x1234])


class WindowsPlatformTest(unittest.TestCase):
    def test_no_tasklist_left(self):
        self.assertNotIn("tasklist", inspect.getsource(server))
        self.assertFalse(hasattr(server, "parse_tasklist"))
        self.assertFalse(hasattr(server, "CREATE_NO_WINDOW"))

    def test_default_source_off_windows(self):
        if sys.platform != "win32":
            self.assertIsInstance(server.WindowsPlatform().source, server.DryRunProcessSource)

    def test_explicit_source_used(self):
        source = server.DryRunProcessSource()
        self.assertIs(server.WindowsPlatform(source).source, source)

    def test_default_platform_passes_source_through(self):
        source = server.DryRunProcessSource()
        with mock.patch.object(server.sys, "platform", "win32"):
            platform = server.default_platform(source)
        self.assertIsInstance(platform, server.WindowsPlatform)
        self.assertIs(platform.source, source)

    def test_default_platform_off_windows_accepts_source(self):
        if sys.platform != "win32":
            platform = server.default_platform(server.DryRunProcessSource())
            self.assertIsInstance(platform, server.DryRunPlatform)

    def test_is_running_and_pids_use_the_source(self):
        source = server.DryRunProcessSource()
        source.processes = [("app.exe", 42), ("other.exe", 43), ("app.exe", 44)]
        platform = server.WindowsPlatform(source)
        with mock.patch.object(server.subprocess, "run", side_effect=AssertionError("spawned")):
            self.assertEqual(platform.pids("App.EXE"), {42, 44})
            self.assertTrue(platform.is_running("app.exe"))
            self.assertTrue(platform.is_running("OTHER.exe"))
            self.assertFalse(platform.is_running("missing.exe"))
        source.processes = []
        self.assertFalse(platform.is_running("app.exe"))

    def test_source_errors_propagate_as_oserror_for_press_button(self):
        class Broken:
            def snapshot(self):
                raise OSError("denied")

        button = {"id": "app", "name": "App", "target": "C:\\app.exe", "process": "app.exe",
                  "window": "normal", "ifRunning": "focus"}
        result = server.press_button(button, server.WindowsPlatform(Broken()))
        self.assertEqual(result, {"ok": False, "action": "error",
                                  "message": "Could not check App: denied"})

    def test_focus_without_pids_does_not_touch_user32(self):
        platform = server.WindowsPlatform(server.DryRunProcessSource())
        with mock.patch.object(ctypes, "WinDLL", create=True, side_effect=AssertionError):
            self.assertFalse(platform.focus("app.exe"))

    def test_focus_uses_pids_of_the_process(self):
        source = server.DryRunProcessSource()
        source.processes = [("app.exe", 42), ("other.exe", 43), ("app.exe", 44)]
        platform = server.WindowsPlatform(source)
        user32 = mock.MagicMock()
        user32.IsIconic.return_value = False
        with mock.patch.object(ctypes, "WinDLL", create=True, return_value=user32), \
                mock.patch.object(platform, "find_window", return_value=0x4242) as find, \
                mock.patch.object(server.subprocess, "run", side_effect=AssertionError):
            self.assertTrue(platform.focus("APP.exe"))
        self.assertEqual(find.call_args[0][0], user32)
        self.assertEqual(find.call_args[0][1], {42, 44})
        user32.SetForegroundWindow.assert_called_once_with(0x4242)
        self.assertEqual(user32.keybd_event.call_args_list,
                         [mock.call(0x12, 0, 0, 0), mock.call(0x12, 0, 2, 0)])
        user32.ShowWindow.assert_not_called()

    def test_focus_restores_minimized_window(self):
        source = server.DryRunProcessSource()
        source.processes = [("app.exe", 42)]
        platform = server.WindowsPlatform(source)
        user32 = mock.MagicMock()
        user32.IsIconic.return_value = True
        with mock.patch.object(ctypes, "WinDLL", create=True, return_value=user32), \
                mock.patch.object(platform, "find_window", return_value=0x4242):
            self.assertTrue(platform.focus("app.exe"))
        user32.ShowWindow.assert_called_once_with(0x4242, server.SW_RESTORE)

    def test_focus_without_window_is_false(self):
        source = server.DryRunProcessSource()
        source.processes = [("app.exe", 42)]
        platform = server.WindowsPlatform(source)
        with mock.patch.object(ctypes, "WinDLL", create=True, return_value=mock.MagicMock()), \
                mock.patch.object(platform, "find_window", return_value=None):
            self.assertFalse(platform.focus("app.exe"))


class WatcherTestCase(unittest.TestCase):
    def make(self, source=None, watched=None, **kwargs):
        self.source = source if source is not None else CountingSource()
        self.watched = {"steam": "steam.exe", "discord": "discord.exe"} if watched is None \
            else watched
        kwargs.setdefault("interval", 0.02)
        kwargs.setdefault("burst_interval", 0.01)
        kwargs.setdefault("burst_seconds", 0.2)
        watcher = server.ProcessWatcher(self.source, lambda: dict(self.watched), **kwargs)
        self.addCleanup(watcher.shutdown)
        return watcher


class ProcessWatcherTest(WatcherTestCase):
    def test_no_thread_and_no_snapshots_without_subscribers(self):
        watcher = self.make(interval=0.005, burst_interval=0.005)
        watcher.kick()
        settle(0.3)
        self.assertIsNone(watcher.thread)
        self.assertEqual(self.source.calls, 0)

    def test_no_snapshots_after_last_unsubscribe(self):
        watcher = self.make()
        callback = Recorder()
        watcher.subscribe(callback)
        watcher.unsubscribe(callback)
        settle(0.1)
        calls = self.source.calls
        settle(0.3)
        self.assertEqual(self.source.calls, calls)
        self.assertIsNone(watcher.thread)

    def test_subscribe_starts_thread_and_returns_state(self):
        self.source = CountingSource([("steam.exe", 10)])
        watcher = self.make(source=self.source)
        state = watcher.subscribe(Recorder())
        self.assertEqual(state, {"steam": True, "discord": False})
        self.assertTrue(watcher.thread.is_alive())
        self.assertTrue(watcher.thread.daemon)
        self.assertEqual(self.source.calls, 1)

    def test_subscribe_returns_state_for_every_watched_id(self):
        watcher = self.make(watched={})
        self.assertEqual(watcher.subscribe(Recorder()), {})

    def test_no_snapshots_while_nothing_is_watched(self):
        watcher = self.make(watched={})
        callback = Recorder()
        self.assertEqual(watcher.subscribe(callback), {})
        watcher.kick()
        settle(0.3)
        self.assertTrue(watcher.thread.is_alive())
        self.assertEqual(self.source.calls, 0)
        self.assertEqual(callback.calls, [])
        self.watched["steam"] = "steam.exe"
        self.assertTrue(wait_for(lambda: callback.calls))
        self.assertEqual(callback.calls, [{"steam": False}])
        self.assertGreater(self.source.calls, 0)

    def test_running_thread_serves_last_state_without_snapshot(self):
        watcher = self.make(interval=60, burst_interval=60)
        self.source.processes = [("steam.exe", 1)]
        first = watcher.subscribe(Recorder())
        self.assertEqual(first, {"steam": True, "discord": False})
        self.source.processes = []
        second = watcher.subscribe(Recorder())
        self.assertEqual(second, first)
        self.assertEqual(self.source.calls, 1)
        second["steam"] = None
        self.assertEqual(watcher.subscribe(Recorder()), first)

    def test_changes_only_and_nothing_when_unchanged(self):
        watcher = self.make()
        callback = Recorder()
        watcher.subscribe(callback)
        settle(0.3)
        self.assertGreaterEqual(self.source.calls, 4)
        self.assertEqual(callback.calls, [])
        self.source.processes = [("steam.exe", 1)]
        self.assertTrue(wait_for(lambda: callback.calls))
        self.assertEqual(callback.calls, [{"steam": True}])
        settle(0.15)
        self.assertEqual(callback.calls, [{"steam": True}])
        self.source.processes = [("steam.exe", 1), ("discord.exe", 2)]
        self.assertTrue(wait_for(lambda: len(callback.calls) == 2))
        self.assertEqual(callback.calls[1], {"discord": True})
        self.source.processes = [("discord.exe", 2)]
        self.assertTrue(wait_for(lambda: len(callback.calls) == 3))
        self.assertEqual(callback.calls[2], {"steam": False})

    def test_pid_change_alone_is_not_a_change(self):
        watcher = self.make()
        self.source.processes = [("steam.exe", 1)]
        callback = Recorder()
        watcher.subscribe(callback)
        self.source.processes = [("steam.exe", 99)]
        settle(0.2)
        self.assertEqual(callback.calls, [])

    def test_callback_gets_its_own_copy(self):
        watcher = self.make()

        def mutate(changes):
            changes["junk"] = True

        other = Recorder()
        watcher.subscribe(mutate)
        watcher.subscribe(other)
        self.source.processes = [("steam.exe", 1)]
        self.assertTrue(wait_for(lambda: other.calls))
        self.assertEqual(other.calls[0], {"steam": True})

    def test_newly_watched_ids_appear(self):
        watcher = self.make()
        callback = Recorder()
        watcher.subscribe(callback)
        self.source.processes = [("new.exe", 5)]
        self.watched["fresh"] = "new.exe"
        self.assertTrue(wait_for(lambda: callback.calls))
        self.assertEqual(callback.calls, [{"fresh": True}])
        self.watched["other"] = "nothing.exe"
        self.assertTrue(wait_for(lambda: len(callback.calls) == 2))
        self.assertEqual(callback.calls[1], {"other": False})

    def test_idle_cadence_is_the_interval(self):
        watcher = self.make(interval=0.05, burst_interval=0.001)
        started = time.monotonic()
        watcher.subscribe(Recorder())
        self.assertTrue(wait_for(lambda: self.source.calls >= 4))
        settle(0.3)
        elapsed = time.monotonic() - started
        self.assertLessEqual(self.source.calls, elapsed / 0.05 + 2)

    def test_kick_bursts_wakes_immediately_and_returns_to_interval(self):
        watcher = self.make(interval=5.0, burst_interval=0.02, burst_seconds=0.4)
        watcher.subscribe(Recorder())
        settle(0.3)
        self.assertEqual(self.source.calls, 1)
        watcher.kick()
        self.assertTrue(wait_for(lambda: self.source.calls >= 6, 3))
        settle(0.4 + 0.3)
        settled = self.source.calls
        settle(0.3)
        self.assertEqual(self.source.calls, settled)
        self.assertTrue(watcher.thread.is_alive())

    def test_kick_extends_an_active_burst(self):
        watcher = self.make(interval=5.0, burst_interval=0.01, burst_seconds=0.3)
        watcher.subscribe(Recorder())
        watcher.kick()
        settle(0.25)
        before = self.source.calls
        watcher.kick()
        settle(0.25)
        self.assertGreater(self.source.calls, before + 10)

    def test_thread_ends_after_last_unsubscribe(self):
        watcher = self.make()
        first, second = Recorder(), Recorder()
        watcher.subscribe(first)
        thread = watcher.thread
        watcher.subscribe(second)
        self.assertIs(watcher.thread, thread)
        watcher.unsubscribe(first)
        self.assertIs(watcher.thread, thread)
        self.assertTrue(thread.is_alive())
        watcher.unsubscribe(second)
        self.assertIsNone(watcher.thread)
        thread.join(TIMEOUT)
        self.assertFalse(thread.is_alive())
        watcher.unsubscribe(second)

    def test_resubscribe_after_stop_starts_fresh_thread_and_state(self):
        watcher = self.make()
        callback = Recorder()
        watcher.subscribe(callback)
        old = watcher.thread
        watcher.unsubscribe(callback)
        self.source.processes = [("steam.exe", 1)]
        state = watcher.subscribe(callback)
        self.assertEqual(state, {"steam": True, "discord": False})
        self.assertIsNot(watcher.thread, old)
        self.assertTrue(watcher.thread.is_alive())
        old.join(TIMEOUT)
        self.assertFalse(old.is_alive())
        settle(0.2)
        self.assertEqual(callback.calls, [])

    def test_one_snapshot_per_tick_for_many_subscribers(self):
        watched = {"steam": "steam.exe"}
        watcher = self.make(source=ToggleSource(), watched=watched)
        recorders = [Recorder() for _ in range(3)]
        for recorder in recorders:
            watcher.subscribe(recorder)
        self.assertTrue(wait_for(lambda: len(recorders[0].calls) >= 6))
        thread = watcher.thread
        watcher.shutdown()
        thread.join(TIMEOUT)
        self.assertFalse(thread.is_alive())
        ticks = len(recorders[0].calls)
        self.assertEqual([len(r.calls) for r in recorders], [ticks] * 3)
        extra = self.source.calls - 1 - ticks
        self.assertIn(extra, (0, 1))

    def test_snapshot_exception_skips_tick_and_keeps_thread(self):
        watcher = self.make()
        callback = Recorder()
        watcher.subscribe(callback)
        thread = watcher.thread
        self.source.error = RuntimeError("boom")
        self.source.processes = [("steam.exe", 1)]
        start = self.source.calls
        self.assertTrue(wait_for(lambda: self.source.calls >= start + 4))
        self.assertEqual(callback.calls, [])
        self.assertTrue(thread.is_alive())
        self.source.error = None
        self.assertTrue(wait_for(lambda: callback.calls))
        self.assertEqual(callback.calls, [{"steam": True}])
        self.assertIs(watcher.thread, thread)

    def test_initial_snapshot_failure_still_starts_thread(self):
        self.source = CountingSource()
        self.source.error = OSError("denied")
        watcher = self.make(source=self.source)
        callback = Recorder()
        self.assertEqual(watcher.subscribe(callback), {})
        self.assertTrue(watcher.thread.is_alive())
        self.source.error = None
        self.assertTrue(wait_for(lambda: callback.calls))
        self.assertEqual(callback.calls, [{"steam": False, "discord": False}])

    def test_get_watched_exception_skips_tick_and_keeps_thread(self):
        failing = []
        source = CountingSource()
        watcher = server.ProcessWatcher(
            source, lambda: self.fail_or({"steam": "steam.exe"}, failing),
            interval=0.02, burst_interval=0.01)
        self.addCleanup(watcher.shutdown)
        callback = Recorder()
        watcher.subscribe(callback)
        thread = watcher.thread
        failing.append(True)
        source.processes = [("steam.exe", 1)]
        settle(0.3)
        self.assertEqual(callback.calls, [])
        self.assertTrue(thread.is_alive())
        failing.clear()
        self.assertTrue(wait_for(lambda: callback.calls))
        self.assertEqual(callback.calls, [{"steam": True}])

    @staticmethod
    def fail_or(value, failing):
        if failing:
            raise ValueError("bad config")
        return value

    def test_raising_callback_removes_only_that_subscriber(self):
        watcher = self.make(source=ToggleSource(), watched={"steam": "steam.exe"})
        bad, good = Raiser(), Recorder()
        watcher.subscribe(bad)
        watcher.subscribe(good)
        thread = watcher.thread
        self.assertTrue(wait_for(lambda: len(good.calls) >= 3))
        self.assertEqual(bad.count, 1)
        self.assertEqual(watcher.subscribers, [good])
        self.assertIs(watcher.thread, thread)
        self.assertTrue(thread.is_alive())

    def test_raising_only_subscriber_stops_the_thread(self):
        watcher = self.make(source=ToggleSource(), watched={"steam": "steam.exe"})
        bad = Raiser()
        watcher.subscribe(bad)
        thread = watcher.thread
        self.assertTrue(wait_for(lambda: watcher.thread is None))
        thread.join(TIMEOUT)
        self.assertFalse(thread.is_alive())
        self.assertEqual(bad.count, 1)

    def test_shutdown_stops_thread_and_drops_subscribers(self):
        watcher = self.make()
        callback = Recorder()
        watcher.subscribe(callback)
        thread = watcher.thread
        watcher.shutdown()
        self.assertIsNone(watcher.thread)
        self.assertEqual(watcher.subscribers, [])
        thread.join(TIMEOUT)
        self.assertFalse(thread.is_alive())
        calls = self.source.calls
        settle(0.2)
        self.assertEqual(self.source.calls, calls)
        watcher.kick()
        self.assertIsNone(watcher.thread)

    def test_shutdown_is_idempotent_and_safe_without_subscribers(self):
        watcher = self.make()
        watcher.shutdown()
        watcher.shutdown()
        self.assertIsNone(watcher.thread)


class WatchedProcessesTest(unittest.TestCase):
    def setUp(self):
        self.base_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base_dir, True)

    def write(self, text):
        with open(os.path.join(self.base_dir, "buttons.json"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_defaults(self):
        server.ensure_config(self.base_dir)
        self.assertEqual(server.watched_processes(self.base_dir),
                         {"steam": "steam.exe", "discord": "discord.exe",
                          "simhub": "simhubwpf.exe"})

    def test_skips_null_empty_and_blank_process(self):
        self.write(json.dumps({"buttons": [
            None, {"id": "a", "process": "A.EXE"}, {"id": "b", "process": ""},
            {"id": "c", "process": "   "}, {"id": "d"}, {"id": "e", "process": " e.exe "}]}))
        self.assertEqual(server.watched_processes(self.base_dir), {"a": "a.exe", "e": "e.exe"})

    def test_read_errors_give_empty_dict(self):
        self.assertEqual(server.watched_processes(self.base_dir), {})
        self.write("{not json")
        self.assertEqual(server.watched_processes(self.base_dir), {})


class StatusTestCase(unittest.TestCase):
    def setUp(self):
        self.base_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base_dir, True)
        self.source = self.make_source()
        self.input = server.DryRunInput()
        self.httpd = server.create_server(self.base_dir, input_backend=self.input,
                                          process_source=self.source)
        self.watcher = self.httpd.watcher
        self.watcher.interval = 0.05
        self.watcher.burst_interval = 0.02
        self.watcher.burst_seconds = 0.3
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.clients = []

    def make_source(self):
        return CountingSource()

    def stop_server(self):
        for client in self.clients:
            client.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def connect(self, headers=None, path=STATUS_PATH):
        client = test_ws.WsClient(self.port, headers, path)
        self.clients.append(client)
        return client

    def open(self, headers=None, path=STATUS_PATH):
        client = self.connect(headers, path)
        self.assertEqual(client.status, 101)
        return client

    def read_status(self, client, timeout=TIMEOUT):
        frame = client.read_frame(timeout)
        self.assertIsNotNone(frame)
        self.assertEqual(frame[0], server.WS_OP_TEXT)
        message = json.loads(frame[1].decode("utf-8"))
        self.assertEqual(message["t"], "status")
        self.assertEqual(list(message), ["t", "running"])
        return message["running"]

    def open_ready(self, headers=None):
        client = self.open(headers)
        self.read_status(client)
        return client

    def idle(self):
        return wait_for(lambda: self.watcher.thread is None and not self.watcher.subscribers)

    def post(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=TIMEOUT)
        try:
            conn.request("POST", path)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()


class StatusHandshakeTest(StatusTestCase):
    def test_handshake_ok(self):
        client = self.connect({"Sec-WebSocket-Key": test_ws.RFC_KEY})
        self.assertEqual(client.status, 101)
        self.assertEqual(client.headers["sec-websocket-accept"], test_ws.RFC_ACCEPT)
        self.assertEqual(client.headers["upgrade"].lower(), "websocket")
        self.assertIn("upgrade", client.headers["connection"].lower())

    def test_random_key_accept(self):
        client = self.open()
        self.assertEqual(client.headers["sec-websocket-accept"], server.ws_accept_key(client.key))

    def test_missing_upgrade_headers(self):
        cases = ({"Upgrade": None}, {"Connection": None}, {"Connection": "keep-alive"},
                 {"Sec-WebSocket-Key": None}, {"Sec-WebSocket-Version": None},
                 {"Sec-WebSocket-Version": "8"}, {"Upgrade": "h2c"})
        for headers in cases:
            client = self.connect(headers)
            self.assertEqual(client.status, 400, headers)
            self.assertEqual(json.loads(client.body),
                             {"ok": False, "message": "websocket upgrade required"})
        self.assertIsNone(self.watcher.thread)

    def test_origin_mismatch_forbidden(self):
        for origin in ("http://evil.example", "http://127.0.0.1:1", "null", "http://"):
            client = self.connect({"Origin": origin})
            self.assertEqual(client.status, 403, origin)
            self.assertEqual(json.loads(client.body), {"ok": False, "message": "forbidden"})
        self.assertIsNone(self.watcher.thread)

    def test_origin_equal_to_host_allowed(self):
        self.open({"Origin": "http://127.0.0.1:%d" % self.port})

    def test_no_origin_allowed(self):
        self.open()

    def test_no_loopback_guard(self):
        self.httpd.loopback_check = lambda ip: False
        self.open()

    def test_failed_handshakes_do_not_consume_slots(self):
        for _ in range(server.MAX_STATUS_CONNECTIONS + 2):
            self.assertEqual(self.connect({"Origin": "http://evil.example"}).status, 403)
            self.assertEqual(self.connect({"Upgrade": None}).status, 400)
        self.assertEqual(self.httpd.status_connections, 0)
        self.open()

    def test_sub_paths_are_not_websocket_routes(self):
        for path in ("/ws/status/", "/ws/other", "/ws/"):
            client = self.connect(path=path)
            self.assertEqual(client.status, 404, path)


class StatusConnectionLimitTest(StatusTestCase):
    def test_fifth_status_connection_rejected_input_still_works(self):
        self.assertEqual(server.MAX_STATUS_CONNECTIONS, 4)
        clients = [self.open() for _ in range(server.MAX_STATUS_CONNECTIONS)]
        extra = self.connect()
        self.assertEqual(extra.status, 503)
        self.assertEqual(json.loads(extra.body), {"ok": False, "message": "too many connections"})
        pad = self.open(path="/ws")
        pad.send_json({"t": "key", "k": "enter"})
        pad.sync()
        self.assertEqual(self.input.events, [("key", "enter")])
        self.assertEqual(self.httpd.ws_connections, 1)
        self.assertEqual(self.httpd.status_connections, 4)
        clients[0].send_frame(server.WS_OP_CLOSE)
        while clients[0].read_frame()[0] != server.WS_OP_CLOSE:
            pass
        clients[0].close()
        deadline = time.monotonic() + TIMEOUT
        status = None
        while time.monotonic() < deadline:
            status = self.connect().status
            if status == 101:
                break
            time.sleep(0.02)
        self.assertEqual(status, 101)

    def test_input_cap_does_not_block_status(self):
        pads = [self.open(path="/ws") for _ in range(server.WS_MAX_CONNECTIONS)]
        self.assertEqual(self.connect(path="/ws").status, 503)
        status = self.open()
        self.read_status(status)
        self.assertEqual(self.httpd.ws_connections, 4)
        self.assertEqual(self.httpd.status_connections, 1)
        self.assertEqual(len(pads), 4)

    def test_counter_returns_to_zero(self):
        clients = [self.open() for _ in range(3)]
        for client in clients:
            client.close()
        self.assertTrue(wait_for(lambda: self.httpd.status_connections == 0))
        self.assertEqual(self.httpd.ws_connections, 0)


class StatusStreamTest(StatusTestCase):
    def test_first_frame_is_full_state_as_one_text_frame(self):
        client = self.open()
        frame = client.read_frame()
        self.assertEqual(frame[0], server.WS_OP_TEXT)
        self.assertEqual(json.loads(frame[1].decode("utf-8")),
                         {"t": "status", "running": {"steam": False, "discord": False,
                                                     "simhub": False}})

    def test_first_frame_reflects_running_processes_case_insensitively(self):
        self.source.processes = [("steam.exe", 1), ("discord.exe", 2), ("other.exe", 3)]
        client = self.open()
        self.assertEqual(self.read_status(client),
                         {"steam": True, "discord": True, "simhub": False})

    def test_diff_frame_has_only_changed_ids(self):
        client = self.open()
        self.read_status(client)
        self.source.processes = [("steam.exe", 1)]
        self.assertEqual(self.read_status(client), {"steam": True})
        self.source.processes = [("steam.exe", 1), ("simhubwpf.exe", 2)]
        self.assertEqual(self.read_status(client), {"simhub": True})
        self.source.processes = []
        self.assertEqual(self.read_status(client), {"steam": False, "simhub": False})

    def test_unchanged_source_produces_no_frames(self):
        client = self.open_ready()
        settle(0.3)
        self.assertGreaterEqual(self.source.calls, 3)
        with self.assertRaises(socket.timeout):
            client.read_frame(0.3)

    def test_every_client_gets_the_diff(self):
        first, second = self.open_ready(), self.open_ready()
        self.source.processes = [("discord.exe", 1)]
        self.assertEqual(self.read_status(first), {"discord": True})
        self.assertEqual(self.read_status(second), {"discord": True})

    def test_late_client_gets_current_state_not_history(self):
        first = self.open_ready()
        self.source.processes = [("steam.exe", 1)]
        self.assertEqual(self.read_status(first), {"steam": True})
        late = self.open()
        self.assertEqual(self.read_status(late),
                         {"steam": True, "discord": False, "simhub": False})

    def test_config_change_is_picked_up(self):
        client = self.open_ready()
        config = {"port": 8765, "buttons": [
            {"id": "extra", "name": "Extra", "target": "x://", "process": "Extra.exe",
             "window": "normal", "ifRunning": "focus"}]}
        server.write_config(self.base_dir, config)
        self.assertEqual(self.read_status(client), {"extra": False})

    def test_no_work_without_status_clients(self):
        self.assertIsNone(self.watcher.thread)
        self.post("/api/press/steam")
        self.post("/api/press/simmotion")
        self.open(path="/ws")
        settle(0.3)
        self.assertIsNone(self.watcher.thread)
        self.assertEqual(self.source.calls, 0)


class StatusIncomingTest(StatusTestCase):
    def test_data_frames_are_ignored(self):
        client = self.open_ready()
        for message in ({"t": "move", "dx": 5, "dy": 5}, {"t": "click", "b": "left"},
                        {"t": "key", "k": "enter"}, {"t": "bogus"}):
            client.send_json(message)
        client.send_frame(server.WS_OP_TEXT, b"not json")
        client.send_frame(server.WS_OP_TEXT, b"")
        client.sync()
        self.assertEqual(self.input.events, [])
        with self.assertRaises(socket.timeout):
            client.read_frame(0.2)
        self.source.processes = [("steam.exe", 1)]
        self.assertEqual(self.read_status(client), {"steam": True})

    def test_ping_gets_pong_and_pong_is_ignored(self):
        client = self.open_ready()
        client.send_frame(server.WS_OP_PING, b"hello")
        self.assertEqual(client.read_frame(), (server.WS_OP_PONG, b"hello"))
        client.send_frame(server.WS_OP_PONG, b"x")
        client.sync()

    def test_close_gets_close_reply_and_stops_watcher(self):
        client = self.open_ready()
        thread = self.watcher.thread
        self.assertIsNotNone(thread)
        client.send_frame(server.WS_OP_CLOSE, struct.pack(">H", 1000))
        self.assertEqual(client.read_frame(), (server.WS_OP_CLOSE, struct.pack(">H", 1000)))
        self.assertIsNone(client.read_frame())
        self.assertTrue(self.idle())
        thread.join(TIMEOUT)
        self.assertFalse(thread.is_alive())
        self.assertTrue(wait_for(lambda: self.httpd.status_connections == 0))

    def test_close_without_payload(self):
        client = self.open_ready()
        client.send_frame(server.WS_OP_CLOSE)
        self.assertEqual(client.read_frame(), (server.WS_OP_CLOSE, b""))
        self.assertTrue(self.idle())

    def test_abrupt_drop_unsubscribes(self):
        client = self.open_ready()
        self.assertIsNotNone(self.watcher.thread)
        client.close()
        self.assertTrue(self.idle())
        self.assertTrue(wait_for(lambda: self.httpd.status_connections == 0))

    def test_connection_reset_unsubscribes(self):
        client = self.open_ready()
        client.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.close()
        self.assertTrue(self.idle())

    def test_watcher_keeps_running_for_remaining_client(self):
        first, second = self.open_ready(), self.open_ready()
        first.close()
        self.assertTrue(wait_for(lambda: len(self.watcher.subscribers) == 1))
        self.assertTrue(self.watcher.thread.is_alive())
        self.source.processes = [("steam.exe", 1)]
        self.assertEqual(self.read_status(second), {"steam": True})

    def test_protocol_violations_keep_their_close_codes_and_unsubscribe(self):
        cases = (
            (lambda c: c.send_raw(server.ws_encode_frame(server.WS_OP_TEXT, b"{}")), 1002),
            (lambda c: c.send_frame(server.WS_OP_TEXT, b'{"t":', fin=False), 1003),
            (lambda c: c.send_frame(0x2, b"\x00\x01"), 1003),
            (lambda c: c.send_frame(0x0, b"abc"), 1003),
            (lambda c: c.send_frame(server.WS_OP_TEXT, b" " * (server.WS_MAX_PAYLOAD + 1)), 1009),
            (lambda c: c.send_frame(server.WS_OP_PING, b"x" * 126), 1002),
            (lambda c: c.send_raw(b"\xc1\x80\x00\x00\x00\x00"), 1002),
            (lambda c: c.send_frame(0x3, b""), 1002),
        )
        for send, code in cases:
            client = self.open_ready()
            send(client)
            self.assertEqual(client.read_close_code(), code)
            self.assertIsNone(client.read_frame())
            self.assertTrue(self.idle())
            client.close()
            self.assertTrue(wait_for(lambda: self.httpd.status_connections == 0))


class StatusKeepaliveTest(StatusTestCase):
    def setUp(self):
        super().setUp()
        self.httpd.ws_ping_interval = 0.2

    def test_idle_client_receives_ping(self):
        client = self.open()
        self.read_status(client)
        self.assertEqual(client.read_frame(2), (server.WS_OP_PING, b""))

    def test_answering_client_stays_subscribed(self):
        client = self.open()
        self.read_status(client)
        deadline = time.monotonic() + 1.0
        pings = 0
        while time.monotonic() < deadline:
            frame = client.read_frame(0.5)
            self.assertIsNotNone(frame)
            if frame[0] == server.WS_OP_PING:
                pings += 1
                client.send_frame(server.WS_OP_PONG, frame[1])
        self.assertGreaterEqual(pings, 3)
        self.source.processes = [("steam.exe", 1)]
        self.assertEqual(self.read_status(client), {"steam": True})

    def test_unanswered_ping_closes_and_unsubscribes(self):
        client = self.open()
        self.read_status(client)
        frames = []
        while True:
            frame = client.read_frame(3)
            if frame is None:
                break
            frames.append(frame)
        self.assertEqual(frames, [(server.WS_OP_PING, b"")])
        self.assertTrue(self.idle())
        self.assertTrue(wait_for(lambda: self.httpd.status_connections == 0))


class SlowSocket:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def sendall(self, data):
        with self.lock:
            self.events.append(("start", data))
        time.sleep(0.005)
        with self.lock:
            self.events.append(("end", data))


class SendLockTest(unittest.TestCase):
    def make_handler(self):
        handler = server.Handler.__new__(server.Handler)
        handler.connection = SlowSocket()
        handler.ws_send_lock = threading.Lock()
        handler.ws_closed = False
        return handler

    def test_concurrent_writes_never_interleave(self):
        handler = self.make_handler()

        def writer(tag):
            for i in range(15):
                handler.ws_send(server.ws_encode_frame(server.WS_OP_TEXT, (tag * 10).encode()))

        threads = [threading.Thread(target=writer, args=(tag,)) for tag in "ab"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(TIMEOUT)
        events = handler.connection.events
        self.assertEqual(len(events), 60)
        for start, end in zip(events[0::2], events[1::2]):
            self.assertEqual(start[0], "start")
            self.assertEqual(end, ("end", start[1]))

    def test_notify_waits_for_the_send_lock(self):
        handler = self.make_handler()
        handler.ws_send_lock.acquire()
        thread = threading.Thread(target=handler.status_notify, args=({"steam": True},))
        thread.start()
        thread.join(0.2)
        self.assertTrue(thread.is_alive())
        self.assertEqual(handler.connection.events, [])
        handler.ws_send_lock.release()
        thread.join(TIMEOUT)
        self.assertFalse(thread.is_alive())
        self.assertEqual(handler.connection.events[0][1], server.status_frame({"steam": True}))

    def test_nothing_is_written_after_the_close_frame(self):
        handler = self.make_handler()
        close = server.ws_encode_frame(server.WS_OP_CLOSE, b"")
        self.assertTrue(handler.ws_send(close, closing=True))
        self.assertFalse(handler.ws_send(server.ws_encode_frame(server.WS_OP_PING)))
        handler.status_notify({"steam": True})
        self.assertEqual(handler.connection.events, [("start", close), ("end", close)])


class StatusInterleaveTest(StatusTestCase):
    def make_source(self):
        return ToggleSource()

    def test_pongs_and_diffs_never_corrupt_each_other(self):
        self.watcher.interval = 0.003
        self.watcher.burst_interval = 0.003
        client = self.open()
        total = 150
        frames = []
        failure = []

        def reader():
            pongs = 0
            try:
                while pongs < total:
                    frame = client.read_frame(5)
                    if frame is None:
                        failure.append("closed")
                        return
                    frames.append(frame)
                    pongs += frame[0] == server.WS_OP_PONG
            except Exception as e:
                failure.append(repr(e))

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        expected = []
        for i in range(total):
            payload = bytes([i % 251]) * (1 + i % 120)
            expected.append(payload)
            client.send_frame(server.WS_OP_PING, payload)
            time.sleep(0.002)
        thread.join(10)
        self.assertEqual(failure, [])
        self.assertFalse(thread.is_alive())
        pongs = [payload for opcode, payload in frames if opcode == server.WS_OP_PONG]
        self.assertEqual(pongs, expected)
        statuses = [json.loads(payload.decode("utf-8")) for opcode, payload in frames
                    if opcode == server.WS_OP_TEXT]
        self.assertEqual(len(statuses) + len(pongs), len(frames))
        self.assertGreater(len(statuses), 10)
        self.assertEqual(set(statuses[0]["running"]), DEFAULT_IDS)
        for message in statuses[1:]:
            self.assertEqual(message["t"], "status")
            self.assertEqual(list(message["running"]), ["steam"])
        values = [m["running"]["steam"] for m in statuses[1:]]
        self.assertTrue(all(a != b for a, b in zip(values, values[1:])))


class PressKickTest(StatusTestCase):
    def setUp(self):
        super().setUp()
        self.watcher.interval = 5.0
        self.kicks = []
        original = self.watcher.kick

        def spy():
            self.kicks.append(1)
            original()

        self.watcher.kick = spy

    def test_successful_press_kicks_and_response_is_unchanged(self):
        client = self.open_ready()
        self.source.processes = [("steam.exe", 1)]
        with self.assertRaises(socket.timeout):
            client.read_frame(0.3)
        self.assertEqual(self.post("/api/press/steam"),
                         (200, {"ok": True, "action": "launched", "message": "Launched Steam"}))
        self.assertEqual(self.kicks, [1])
        self.assertEqual(self.read_status(client, 3), {"steam": True})

    def test_failed_and_unknown_presses_do_not_kick(self):
        self.open_ready()
        self.assertEqual(self.post("/api/press/simmotion"),
                         (200, {"ok": False, "action": "error",
                                "message": "SimMotion is not configured"}))
        self.assertEqual(self.post("/api/press/nope"),
                         (404, {"ok": False, "action": "error", "message": "unknown button"}))
        self.assertEqual(self.kicks, [])

    def test_press_without_subscribers_starts_nothing(self):
        self.assertEqual(self.post("/api/press/steam")[0], 200)
        self.assertEqual(self.kicks, [1])
        settle(0.2)
        self.assertIsNone(self.watcher.thread)
        self.assertEqual(self.source.calls, 0)


class NodelayTest(unittest.TestCase):
    def pair(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.create_connection(listener.getsockname())
        self.addCleanup(client.close)
        conn, _ = listener.accept()
        self.addCleanup(conn.close)
        return client, conn

    def test_sets_tcp_nodelay_on_real_socket(self):
        client, conn = self.pair()
        self.assertEqual(conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 0)
        server.set_nodelay(conn)
        self.assertEqual(conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)

    def test_swallows_oserror_on_closed_socket(self):
        client, conn = self.pair()
        conn.close()
        self.assertIsNone(server.set_nodelay(conn))


class NodelayServerTest(StatusTestCase):
    def test_applied_to_both_websocket_routes(self):
        with mock.patch.object(server, "set_nodelay", wraps=server.set_nodelay) as spy:
            self.open(path="/ws")
            self.assertTrue(wait_for(lambda: spy.call_count == 1))
            self.open()
            self.assertTrue(wait_for(lambda: spy.call_count == 2))
            for call in spy.call_args_list:
                self.assertIsInstance(call[0][0], socket.socket)
            self.connect({"Upgrade": None})
            self.connect({"Origin": "http://evil.example"})
            settle(0.1)
            self.assertEqual(spy.call_count, 2)


class FakeKernel32:
    def __init__(self, result=1, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def GetCurrentProcess(self):
        return 0xFFFFFFFFFFFFFFFF

    def SetPriorityClass(self, handle, priority):
        if self.error is not None:
            raise self.error
        self.calls.append((handle, priority))
        return self.result


class LowerPriorityTest(unittest.TestCase):
    def test_sets_below_normal_on_current_process(self):
        kernel32 = FakeKernel32()
        self.assertTrue(server.lower_priority(kernel32))
        self.assertEqual(kernel32.calls, [(0xFFFFFFFFFFFFFFFF, 0x4000)])
        self.assertEqual(server.BELOW_NORMAL_PRIORITY_CLASS, 0x00004000)

    def test_failure_returns_false(self):
        self.assertFalse(server.lower_priority(FakeKernel32(result=0)))

    def test_exception_returns_false(self):
        self.assertFalse(server.lower_priority(FakeKernel32(error=OSError("denied"))))

    def test_without_windows_api_returns_false(self):
        if sys.platform != "win32":
            self.assertFalse(server.lower_priority())


class MainTest(unittest.TestCase):
    def run_main(self, argv, platform, lowered=True):
        fake = mock.MagicMock()
        fake.serve_forever.side_effect = KeyboardInterrupt
        lower = mock.MagicMock(return_value=lowered)
        out = io.StringIO()
        with mock.patch.object(server, "create_server", return_value=fake) as create, \
                mock.patch.object(server, "ensure_config"), \
                mock.patch.object(server, "lan_ip", return_value="10.0.0.5"), \
                mock.patch.object(server, "lower_priority", lower), \
                mock.patch.object(server.sys, "platform", platform), \
                mock.patch.object(server.sys, "argv", ["server.py"] + argv), \
                contextlib.redirect_stdout(out):
            server.main()
        fake.server_close.assert_called_once_with()
        return out.getvalue(), lower, create

    def test_parser_flag(self):
        parser = server.build_parser()
        args = parser.parse_args([])
        self.assertFalse(args.normal_priority)
        self.assertIsNone(args.port)
        args = parser.parse_args(["--normal-priority", "--port", "9000"])
        self.assertTrue(args.normal_priority)
        self.assertEqual(args.port, 9000)

    def test_priority_lowered_on_windows(self):
        out, lower, create = self.run_main(["--port", "9000"], "win32")
        lower.assert_called_once_with()
        self.assertEqual(out.splitlines(), [
            "Stream Deck running", "  PC:    http://localhost:9000",
            "  Phone: http://10.0.0.5:9000",
            "  Priority: below normal (start with --normal-priority to disable)"])

    def test_normal_priority_flag_skips_lowering(self):
        out, lower, create = self.run_main(["--port", "9000", "--normal-priority"], "win32")
        lower.assert_not_called()
        self.assertNotIn("Priority", out)

    def test_not_applied_off_windows(self):
        out, lower, create = self.run_main(["--port", "9000"], "linux")
        lower.assert_not_called()
        self.assertNotIn("Priority", out)

    def test_no_line_when_lowering_fails(self):
        out, lower, create = self.run_main(["--port", "9000"], "win32", lowered=False)
        lower.assert_called_once_with()
        self.assertNotIn("Priority", out)


class DeckServerTest(unittest.TestCase):
    def setUp(self):
        self.base_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base_dir, True)

    def test_exposes_source_and_watcher_sharing_one_source(self):
        source = CountingSource()
        httpd = server.create_server(self.base_dir, process_source=source)
        self.addCleanup(httpd.server_close)
        self.assertIs(httpd.process_source, source)
        self.assertIsInstance(httpd.watcher, server.ProcessWatcher)
        self.assertIs(httpd.watcher.source, source)
        self.assertEqual(httpd.watcher.interval, 2.0)
        self.assertEqual(httpd.watcher.burst_interval, 0.5)
        self.assertEqual(httpd.watcher.burst_seconds, 10.0)
        self.assertEqual(httpd.watcher.get_watched(),
                         {"steam": "steam.exe", "discord": "discord.exe",
                          "simhub": "simhubwpf.exe"})

    def test_default_source_off_windows(self):
        httpd = server.create_server(self.base_dir)
        self.addCleanup(httpd.server_close)
        if sys.platform != "win32":
            self.assertIsInstance(httpd.process_source, server.DryRunProcessSource)
        self.assertIs(httpd.watcher.source, httpd.process_source)

    def test_platform_and_watcher_share_the_default_source(self):
        with mock.patch.object(server.sys, "platform", "win32"):
            httpd = server.create_server(self.base_dir)
        self.addCleanup(httpd.server_close)
        self.assertIsInstance(httpd.platform, server.WindowsPlatform)
        self.assertIs(httpd.platform.source, httpd.process_source)
        self.assertIs(httpd.watcher.source, httpd.process_source)

    def test_explicit_platform_is_kept(self):
        platform = server.DryRunPlatform()
        httpd = server.create_server(self.base_dir, platform=platform)
        self.addCleanup(httpd.server_close)
        self.assertIs(httpd.platform, platform)

    def test_server_close_stops_the_watcher(self):
        httpd = server.create_server(self.base_dir, process_source=CountingSource())
        httpd.watcher.subscribe(Recorder())
        thread = httpd.watcher.thread
        self.assertTrue(thread.is_alive())
        httpd.server_close()
        self.assertIsNone(httpd.watcher.thread)
        self.assertEqual(httpd.watcher.subscribers, [])
        thread.join(TIMEOUT)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
