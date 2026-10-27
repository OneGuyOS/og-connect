"""Active notification parsing and visible-only ADB polling tests."""

from __future__ import annotations

import time
import threading
import unittest
from unittest.mock import PropertyMock, patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from phonebridge.adb import AdbDevice
from phonebridge.device import ConnectionState, PhoneSession
from phonebridge.notifications_page import (
    ActiveNotification,
    NotificationSnapshot,
    NotificationsPage,
    parse_notification_dump,
)
from phonebridge.transfer_queue import TransferQueue


NOTIFICATION_DUMP = """Current Notification Manager state:
  Notification List:
    NotificationRecord(0x1234: pkg=com.example.chat user=UserHandle{0} id=42 tag=message importance=4 key=0|com.example.chat|42|message|10123: Notification(channel=messages actions=1))
      notification=
            when=1700000000000/1700000000000
            extras={
                android.title=String (Alex)
                android.text=String (A new message)
            }
  TimeoutPendingIntent:
"""


class _Manager(QObject):
    state_changed = Signal(object, object)
    state = ConnectionState.DISCONNECTED
    session = None


class NotificationsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.manager = _Manager()
        self.queue = TransferQueue()
        self.page = NotificationsPage(self.manager, self.queue)

    def tearDown(self):
        self.page.close()

    def _connect(self):
        session = PhoneSession(
            AdbDevice("USB-SERIAL", "device", "usb", model="SM_S731U1"), None, None
        )
        self.page.set_connection(ConnectionState.USB, session)

    @staticmethod
    def _wait(predicate, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            QApplication.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_parser_extracts_title_text_timestamp_and_key(self):
        entries = parse_notification_dump(NOTIFICATION_DUMP)
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            (entries[0].package_name, entries[0].title, entries[0].text),
            ("com.example.chat", "Alex", "A new message"),
        )
        self.assertEqual(entries[0].timestamp_ms, 1700000000000)
        self.assertEqual(entries[0].key, "0|com.example.chat|42|message|10123")

    def test_visible_only_polling_and_disconnect_clear(self):
        snapshot = NotificationSnapshot(parse_notification_dump(NOTIFICATION_DUMP))
        worker_threads = []

        def read(_serial):
            worker_threads.append(threading.get_ident())
            return snapshot

        main_thread = threading.get_ident()
        with patch(
            "phonebridge.notifications_page._read_notifications",
            side_effect=read,
        ) as read:
            self.page.set_notifications_visible(True)
            self.assertEqual(read.call_count, 0)
            self._connect()
            self.assertTrue(self._wait(lambda: self.page._task is None))
            self.assertEqual(self.page.notification_list.count(), 1)
            self.assertTrue(worker_threads)
            self.assertNotEqual(worker_threads[0], main_thread)
            self.assertEqual(self.page._timer.interval(), 5000)
            self.assertEqual(self.page.status_label.text(), "1 active notifications")
            self.assertIn("com.example.chat", self.page.notification_list.item(0).text())
            self.page.set_notifications_visible(False)
            self.assertFalse(self.page._timer.isActive())
            calls = read.call_count
            self.page._poll_if_due()
            self.assertEqual(read.call_count, calls)

            self.page.set_notifications_visible(True)
            self.page._last_poll_at = 0
            with patch.object(
                TransferQueue, "is_running", new_callable=PropertyMock, return_value=True
            ):
                self.page._poll_if_due()
                self.assertEqual(read.call_count, calls)
            self.page.set_notifications_visible(False)

            self.page.set_connection(ConnectionState.DISCONNECTED, None)
            self.assertEqual(self.page.status_label.text(), "No phone connected")
            self.assertEqual(self.page.notification_list.count(), 0)
            self.assertFalse(self.page._timer.isActive())

    def test_no_phone_does_not_poll(self):
        with patch("phonebridge.notifications_page._read_notifications") as read:
            self.page.set_notifications_visible(True)
            self.page._poll_if_due()
            QApplication.processEvents()
            self.assertEqual(read.call_count, 0)
            self.assertEqual(self.page.status_label.text(), "No phone connected")

    def test_details_show_id_tag_and_action_limit(self):
        self.page._notifications = parse_notification_dump(NOTIFICATION_DUMP)
        self.page._render_notifications()
        self.assertIn("ID: 42; tag: message", self.page.detail_label.text())
        self.assertIn("Key: 0|com.example.chat|42|message|10123", self.page.detail_label.text())
        self.assertIn("not exposed", self.page.capability_label.text())
        self.assertFalse(self.page.dismiss_button.isEnabled())
        self.assertFalse(self.page.reply_input.isEnabled())
        self.assertFalse(self.page.reply_button.isEnabled())
        self.assertIn("does not expose", self.page.reply_input.toolTip())


if __name__ == "__main__":
    unittest.main()
