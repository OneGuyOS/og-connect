"""Settings persistence, status display, and conflict-policy integration."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import unittest
from unittest.mock import ANY, patch

from PySide6.QtCore import QObject, QEventLoop, QTimer, Signal
from PySide6.QtWidgets import QApplication

from phonebridge.device import ConnectionState, DeviceManager
from phonebridge.settings_page import SCRCPY_MISSING, SettingsPage
from phonebridge.settings_store import AppSettings, SettingsStore
from phonebridge.transfer_queue import JobState, TransferQueue
from phonebridge.window import HomeDashboard, MainWindow


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout: float = 15.0) -> bool:
    app = QApplication.instance()
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)

    def check():
        if predicate():
            timer.stop()
            loop.quit()

    timer.timeout.connect(check)
    timer.start()
    QTimer.singleShot(int(timeout * 1000), loop.quit)
    check()
    if not predicate():
        loop.exec()
    timer.stop()
    return bool(predicate())


class _DisconnectedManager(QObject):
    state_changed = Signal(object, object)
    state = ConnectionState.DISCONNECTED
    session = None


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT, prefix=".settings-test-")
        self.path = Path(self.temp.name) / "settings.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_invalid_and_persisted_values(self):
        initial = SettingsStore(self.path)
        self.assertEqual(initial.settings, AppSettings())
        self.assertTrue(self.path.is_file())
        changed = AppSettings(False, "Skip", 75)
        self.assertTrue(initial.save(changed))
        reopened = SettingsStore(self.path)
        self.assertEqual(reopened.settings, changed)

        self.path.write_text("{ invalid json", encoding="utf-8")
        recovered = SettingsStore(self.path)
        self.assertEqual(recovered.settings, AppSettings())
        self.assertEqual(SettingsStore(self.path).settings, AppSettings())

    def test_invalid_values_fall_back_to_sane_defaults(self):
        self.path.write_text(
            '{"show_home_on_startup":"yes","default_conflict_behavior":"Delete",'
            '"battery_refresh_seconds":9999}',
            encoding="utf-8",
        )
        store = SettingsStore(self.path)
        self.assertEqual(store.settings, AppSettings())


class SettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT, prefix=".settings-ui-test-")
        self.store = SettingsStore(Path(self.temp.name) / "settings.json")
        self.manager = _DisconnectedManager()
        self.queue = TransferQueue()
        self.home = HomeDashboard(self.manager, self.queue)
        self.page = SettingsPage(self.store, self.home)
        self.page.preferences_changed.connect(
            lambda settings: self.home.set_refresh_interval(settings.battery_refresh_seconds)
        )
        self.assertTrue(wait_for(lambda: self.page._host_task is None))

    def tearDown(self):
        self.page.close()
        self.home.close()
        self.temp.cleanup()

    def test_preferences_reload_and_update_home_interval(self):
        self.page.battery_interval.setValue(65)
        self.page.show_home_checkbox.setChecked(False)
        self.page.conflict_combo.setCurrentText("Skip")
        reopened = SettingsStore(self.store.path)
        self.assertEqual(reopened.settings, AppSettings(False, "Skip", 65))
        self.assertEqual(self.home._refresh_timer.interval(), 65_000)
        restarted_window = MainWindow(self.manager, reopened)
        self.assertTrue(wait_for(lambda: restarted_window.settings_page._host_task is None))
        self.assertEqual(restarted_window.home_dashboard._refresh_timer.interval(), 65_000)
        self.assertEqual(
            restarted_window.pages.tabText(restarted_window.pages.currentIndex()), "Files"
        )
        restarted_window.close()

    def test_saved_home_startup_preference_selects_files_when_disabled(self):
        self.store.save(AppSettings(False, "Ask", 30))
        window = MainWindow(self.manager, SettingsStore(self.store.path))
        self.assertEqual(
            [window.pages.tabText(index) for index in range(window.pages.count())],
            ["Home", "Settings", "Media", "SMS", "Notifications", "Screen", "Files", "Transfers"],
        )
        self.assertEqual(window.pages.tabText(window.pages.currentIndex()), "Files")
        window.close()

    def test_refresh_status_and_clipboard_diagnostics(self):
        self.assertTrue(wait_for(lambda: self.page._host_task is None))
        worker_threads = []
        main_thread = threading.get_ident()

        def host_status():
            worker_threads.append(threading.get_ident())
            return "/usr/bin/adb", "Android Debug Bridge version 1.0.41", "scrcpy 3.3"

        with patch(
            "phonebridge.settings_page._read_host_status",
            side_effect=host_status,
        ):
            self.page.refresh_status()
            self.assertTrue(wait_for(lambda: self.page._host_task is None))
        self.assertTrue(worker_threads and worker_threads[0] != main_thread)
        self.assertIn("/usr/bin/adb", self.page.adb_label.text())
        self.assertEqual(self.page.scrcpy_label.text(), "Installed — scrcpy 3.3")
        with patch(
            "phonebridge.settings_page._read_host_status",
            return_value=("/usr/bin/adb", "Android Debug Bridge version 1.0.41", SCRCPY_MISSING),
        ):
            self.page.refresh_status()
            self.assertTrue(wait_for(lambda: self.page._host_task is None))
        self.assertEqual(self.page.scrcpy_label.text(), SCRCPY_MISSING)
        self.page.set_phone_info("SM_S731U1", "17", "USB-SERIAL")
        diagnostics = self.page.copy_diagnostics()
        self.assertEqual(QApplication.clipboard().text(), diagnostics)
        for expected in (
            "OS:", "ADB: /usr/bin/adb", "scrcpy: scrcpy not installed",
            "SM_S731U1", "Android version: 17", "USB-SERIAL",
        ):
            self.assertIn(expected, diagnostics)
        self.assertNotIn(str(Path.home()), diagnostics)


class ConflictPreferenceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.previous_env = {
            key: os.environ.get(key)
            for key in ("ADB", "PHONEBRIDGE_TEST_PHONE_ROOT", "PHONEBRIDGE_TEST_SERIAL")
        }
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT, prefix=".settings-conflict-test-")
        self.root = Path(self.temp.name)
        self.phone_root = self.root / "phone"
        self.phone_root.mkdir()
        self.serial = "sim-" + secrets.token_hex(8)
        self.fake_adb = self.root / "fake-adb"
        shutil.copyfile(Path(__file__).with_name("fake_adb.py"), self.fake_adb)
        self.fake_adb.chmod(0o755)
        os.environ["ADB"] = str(self.fake_adb)
        os.environ["PHONEBRIDGE_TEST_PHONE_ROOT"] = str(self.phone_root)
        os.environ["PHONEBRIDGE_TEST_SERIAL"] = self.serial
        self.store = SettingsStore(self.root / "settings.json")
        self.manager = DeviceManager()
        self.window = MainWindow(self.manager, self.store)
        self.assertTrue(wait_for(lambda: self.manager.session is not None))
        self.assertEqual(self.window.pages.currentWidget(), self.window.home_dashboard)

    def tearDown(self):
        self.manager._timer.stop()
        self.window.close()
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def _copy_conflicting(self, behavior: str, source_name: str, old: bytes):
        source = self.root / source_name
        source.write_bytes(b"new contents")
        (self.phone_root / source_name).write_bytes(old)
        self.window.settings_page.conflict_combo.setCurrentText(behavior)
        self.window._copy_to_phone([str(source)])
        job = self.window.transfer_queue.jobs[-1]
        self.assertTrue(wait_for(lambda: job.state in (JobState.DONE, JobState.CANCELED, JobState.FAILED)))
        self.assertEqual(self.window._conflict_prompts, {})
        return job

    def test_media_tab_is_after_settings_and_polling_tracks_visibility(self):
        self.assertEqual(
            [self.window.pages.tabText(index) for index in range(self.window.pages.count())],
            ["Home", "Settings", "Media", "SMS", "Notifications", "Screen", "Files", "Transfers"],
        )
        media = self.window.media_page
        self.assertFalse(media._timer.isActive())
        self.window.pages.setCurrentWidget(media)
        self.assertTrue(wait_for(lambda: media._request_task is None))
        self.assertTrue(media._timer.isActive())
        self.window.pages.setCurrentWidget(self.window.home_dashboard)
        self.assertFalse(media._timer.isActive())
        sms = self.window.sms_page
        self.assertFalse(sms._timer.isActive())
        self.window.pages.setCurrentWidget(sms)
        self.assertTrue(wait_for(lambda: sms._task is None))
        self.assertTrue(sms._timer.isActive())
        self.window.pages.setCurrentWidget(self.window.home_dashboard)
        self.assertFalse(sms._timer.isActive())
        notifications = self.window.notifications_page
        self.assertFalse(notifications._timer.isActive())
        self.window.pages.setCurrentWidget(notifications)
        self.assertTrue(wait_for(lambda: notifications._task is None))
        self.assertTrue(notifications._timer.isActive())
        self.assertEqual(notifications._timer.interval(), 5000)
        self.window.pages.setCurrentWidget(self.window.home_dashboard)
        self.assertFalse(notifications._timer.isActive())
        media.set_connection(ConnectionState.DISCONNECTED, None)
        self.assertEqual(media.status_label.text(), "No phone connected")

    def test_screen_action_uses_session_serial_view_only_and_stops_on_disconnect(self):
        page = self.window.screen_page
        self.assertTrue(page.action_button.isEnabled())
        page.control_checkbox.setChecked(False)
        with patch.object(self.window.scrcpy_manager, "start") as start:
            self.window._launch_scrcpy()
        start.assert_called_once_with(
            self.manager.session.device.serial,
            view_only=True,
            window_position=ANY,
        )
        self.assertEqual(page.status_label.text(), "Screen running — View only")

        self.window._update_status(ConnectionState.DISCONNECTED, None)
        self.assertFalse(page.action_button.isEnabled())
        with patch.object(
            self.window.scrcpy_manager, "is_running", return_value=True
        ), patch.object(self.window.scrcpy_manager, "stop") as stop:
            self.window._handle_scrcpy_connection(ConnectionState.DISCONNECTED, None)
            self.assertTrue(wait_for(lambda: self.window._scrcpy_stop_task is None))
        stop.assert_called_once()
        self.assertIn("Phone disconnected", page.status_label.text())
        self.window._update_status(ConnectionState.USB, self.manager.session)

    def test_skip_overwrite_and_rename_are_automatic(self):
        skipped = self._copy_conflicting("Skip", "duplicate.bin", b"keep skip")
        self.assertEqual(skipped.state, JobState.CANCELED)
        self.assertEqual((self.phone_root / "duplicate.bin").read_bytes(), b"keep skip")

        overwritten = self._copy_conflicting("Overwrite", "duplicate.bin", b"keep overwrite")
        self.assertEqual(overwritten.state, JobState.DONE)
        self.assertEqual((self.phone_root / "duplicate.bin").read_bytes(), b"new contents")

        renamed = self._copy_conflicting("Rename", "rename-me.tar.gz", b"keep original")
        self.assertEqual(renamed.state, JobState.DONE)
        self.assertEqual((self.phone_root / "rename-me.tar.gz").read_bytes(), b"keep original")
        self.assertEqual((self.phone_root / "rename-me (1).tar.gz").read_bytes(), b"new contents")


if __name__ == "__main__":
    unittest.main()
