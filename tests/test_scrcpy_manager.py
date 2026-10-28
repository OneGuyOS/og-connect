"""Tests for the managed external scrcpy process."""

from __future__ import annotations

import os
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from phonebridge.scrcpy_manager import ScrcpyManager


class _Child:
    pid = 987654

    def __init__(self, terminate_exits=True):
        self.returncode = None
        self._exited = threading.Event()
        self.terminate_exits = terminate_exits
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if self.terminate_exits:
            self.returncode = -15
            self._exited.set()

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self._exited.set()

    def exit(self, returncode=0):
        self.returncode = returncode
        self._exited.set()

    def wait(self, timeout=None):
        if self._exited.wait(timeout):
            return self.returncode
        raise subprocess.TimeoutExpired("scrcpy", timeout)


class ScrcpyManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.manager = ScrcpyManager()

    def tearDown(self):
        self.manager.stop()

    @staticmethod
    def _wait(predicate, timeout=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            QApplication.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_x11_command_position_view_only_and_no_duplicate(self):
        child = _Child()
        with patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, clear=False), patch(
            "phonebridge.scrcpy_manager.shutil.which", return_value="/usr/bin/scrcpy"
        ), patch("phonebridge.scrcpy_manager.subprocess.Popen", return_value=child) as popen:
            first = self.manager.start("USB-SERIAL", True, (1234, 40))
            second = self.manager.start("OTHER-SERIAL", False, (0, 0))
        self.assertIs(first, child)
        self.assertIs(second, child)
        popen.assert_called_once_with(
            ["scrcpy", "-s", "USB-SERIAL", "--audio-source=playback", "--window-x", "1234", "--window-y", "40", "--no-control"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.manager.stop()
        self.assertEqual(child.terminate_calls, 1)
        self.assertEqual(child.kill_calls, 0)

    def test_wayland_omits_window_position(self):
        with patch.dict(
            os.environ,
            {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
            clear=False,
        ):
            self.assertEqual(
                self.manager._command("USB-SERIAL", False, (400, 20)),
                ["scrcpy", "-s", "USB-SERIAL", "--audio-source=playback"],
            )

    def test_dex_mode_adds_virtual_display_and_audio_is_always_enabled(self):
        with patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": ""}, clear=False):
            self.assertEqual(
                self.manager._command("USB-SERIAL", False, (400, 20), dex_mode=True),
                [
                    "scrcpy", "-s", "USB-SERIAL", "--audio-source=playback",
                    "--new-display=1920x1080/284", "--window-x", "400", "--window-y", "20",
                ],
            )
            self.assertEqual(
                self.manager._command("USB-SERIAL", True, (400, 20), dex_mode=False),
                ["scrcpy", "-s", "USB-SERIAL", "--audio-source=playback", "--window-x", "400", "--window-y", "20", "--no-control"],
            )

    def test_stop_escalates_only_the_stored_child_after_timeout(self):
        child = _Child(terminate_exits=False)
        with patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland"}, clear=False), patch(
            "phonebridge.scrcpy_manager.shutil.which", return_value="/usr/bin/scrcpy"
        ), patch("phonebridge.scrcpy_manager.subprocess.Popen", return_value=child):
            self.manager.start("USB-SERIAL")
        self.manager.stop()
        self.assertEqual(child.terminate_calls, 1)
        self.assertEqual(child.kill_calls, 1)
        self.assertFalse(self.manager.is_running())

    def test_missing_scrcpy_has_install_hint(self):
        with patch("phonebridge.scrcpy_manager.shutil.which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "apt install scrcpy"):
                self.manager.start("USB-SERIAL")

    def test_child_exit_is_reported_and_manager_clears_it(self):
        child = _Child()
        exited = []
        self.manager.process_exited.connect(exited.append)
        with patch("phonebridge.scrcpy_manager.shutil.which", return_value="/usr/bin/scrcpy"), patch(
            "phonebridge.scrcpy_manager.subprocess.Popen", return_value=child
        ):
            self.manager.start("USB-SERIAL")
        child.exit(0)
        self.assertTrue(self._wait(lambda: bool(exited)))
        self.assertEqual(exited, [0])
        self.assertFalse(self.manager.is_running())


if __name__ == "__main__":
    unittest.main()
