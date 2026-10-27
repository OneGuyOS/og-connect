"""SMS parsing, tab visibility, and ADB task tests without sending real messages."""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from phonebridge.adb import AdbDevice
from phonebridge.device import ConnectionState, PhoneSession
from phonebridge.sms_page import (
    SmsMessage,
    SmsPage,
    SmsSnapshot,
    _parse_sms_rows,
    _send_sms_and_verify,
)
from phonebridge.transfer_queue import TransferQueue


class _Manager(QObject):
    state_changed = Signal(object, object)
    state = ConnectionState.DISCONNECTED
    session = None


class SmsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.manager = _Manager()
        self.queue = TransferQueue()
        self.page = SmsPage(self.manager, self.queue)

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

    def test_parser_handles_multiline_sms_body_and_sorts_newest_first(self):
        output = (
            "Row: 0 _id=12, thread_id=4, address=+15550001, body=Newest, date=200, read=1, type=1\n"
            "Row: 1 _id=11, thread_id=4, address=+15550001, body=Older\nsecond line, date=100, read=0, type=2\n"
        )
        messages = _parse_sms_rows(output)
        self.assertEqual([message.message_id for message in messages], [12, 11])
        self.assertEqual(messages[1].body, "Older\nsecond line")
        self.assertEqual(messages[0].thread_id, 4)

    def test_connection_visibility_polling_and_disconnect(self):
        with patch(
            "phonebridge.sms_page._read_sms_snapshot",
            return_value=SmsSnapshot((SmsMessage(1, 2, "+15550001", "hello", 100),)),
        ) as read, patch(
            "phonebridge.sms_page._read_sms_thread",
            return_value=(SmsMessage(1, 2, "+15550001", "hello", 100),),
        ):
            self.page.set_sms_visible(True)
            self.assertEqual(read.call_count, 0)
            self._connect()
            self.assertTrue(self._wait(lambda: self.page._task is None))
            self.assertEqual(self.page._timer.interval(), 10_000)
            self.assertEqual(self.page.conversation_list.count(), 1)
            self.assertEqual(self.page.address_input.text(), "+15550001")
            self.assertTrue(self.page.send_button.isEnabled())
            self.assertIn("may not appear", self.page.history_note.text())
            self.page.set_sms_visible(False)
            self.assertFalse(self.page._timer.isActive())
            calls = read.call_count
            self.page._poll_if_due()
            self.assertEqual(read.call_count, calls)

            self.page.set_connection(ConnectionState.DISCONNECTED, None)
            self.assertEqual(self.page.status_label.text(), "No phone connected")
            self.assertEqual(self.page.conversation_list.count(), 0)
            self.assertEqual(self.page.message_list.count(), 0)
            self.assertFalse(self.page.send_button.isEnabled())
            self.assertFalse(self.page._timer.isActive())

    def test_permission_error_message_disables_available_send_path(self):
        self._connect()
        self.page._finish_snapshot(
            "USB-SERIAL", self.page._generation,
            SmsSnapshot((), "SMS reading is not available on this device via ADB"), None,
        )
        self.assertEqual(
            self.page.status_label.text(),
            "SMS reading is not available on this device via ADB",
        )

    def test_send_uses_samsung_flag_and_accepts_clean_service_parcel(self):
        command_calls = []

        def shell(serial, command, timeout=None):
            command_calls.append((serial, command, timeout, threading.get_ident()))
            return type("Result", (), {
                "stdout": "Result: Parcel(00000000 00000000   '........')",
                "stderr": "",
            })()

        with patch("phonebridge.sms_page.adb.shell", side_effect=shell), patch(
            "phonebridge.sms_page._query_sms"
        ) as query:
            _send_sms_and_verify("USB-SERIAL", "+15550002", "test body")
        self.assertEqual(command_calls[0][0], "USB-SERIAL")
        self.assertIn("service call isms 7", command_calls[0][1])
        self.assertIn("i32 1", command_calls[0][1])
        query.assert_not_called()

    def test_compose_sends_from_worker_thread(self):
        self._connect()
        self.page.address_input.setText("+15550002")
        self.page.message_input.setPlainText("test body")
        main_thread = threading.get_ident()
        worker_threads = []
        with patch(
            "phonebridge.sms_page._send_sms_and_verify",
            side_effect=lambda *_args: worker_threads.append(threading.get_ident()),
        ):
            self.page._send_current_message()
            self.assertTrue(self._wait(lambda: self.page._task is None))
        self.assertTrue(worker_threads)
        self.assertNotEqual(worker_threads[0], main_thread)

    def test_send_rejects_exception_or_permission_denial_in_service_output(self):
        for output in (
            "Exception: transaction failed",
            "Permission Denial: isms",
        ):
            with self.subTest(output=output), patch(
                "phonebridge.sms_page.adb.shell",
                return_value=type("Result", (), {"stdout": f"Result: Parcel({output})", "stderr": ""})(),
            ):
                with self.assertRaisesRegex(RuntimeError, "rejected the SMS service call"):
                    _send_sms_and_verify("USB-SERIAL", "+15550002", "test body")

    def test_send_requires_a_result_parcel(self):
        with patch(
            "phonebridge.sms_page.adb.shell",
            return_value=type("Result", (), {"stdout": "", "stderr": ""})(),
        ):
            with self.assertRaisesRegex(RuntimeError, "no result Parcel"):
                _send_sms_and_verify("USB-SERIAL", "+15550002", "test body")

    def test_failed_send_disables_composer(self):
        self._connect()
        self.page._finish_send("USB-SERIAL", self.page._generation, None, RuntimeError("send failed"))
        self.assertFalse(self.page.send_button.isEnabled())
        self.assertFalse(self.page.address_input.isEnabled())
        self.assertFalse(self.page.message_input.isEnabled())
        self.assertIn("SMS was not confirmed", self.page.status_label.text())


if __name__ == "__main__":
    unittest.main()
