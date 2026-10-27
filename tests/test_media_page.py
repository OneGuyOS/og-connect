"""Media session parsing, visible-only polling, and key routing tests."""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import PropertyMock, patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from phonebridge.adb import AdbDevice
from phonebridge.device import ConnectionState, PhoneSession
from phonebridge.media_page import MediaPage, MediaSnapshot, parse_media_session
from phonebridge.transfer_queue import TransferQueue


PLAYING_DUMP = """MEDIA SESSION SERVICE (dumpsys media_session)
Total sessions: 1 across 1 package(s)
=== DETAILED SESSION INFO ===
User Records:
  Sessions Stack - have 1 sessions:
    com.amazon.mp3 session (userId=0)
      SUMMARY: package=com.amazon.mp3, uid=10489, controllers=1, active=true
      active=true
      state=PlaybackState {state=PLAYING(3), position=1000, speed=1.0, actions=404147}
      metadata: size=3, description=Song Title, Singer Name, Album Name
isAppCastingOn:false
"""


class _Manager(QObject):
    state_changed = Signal(object, object)
    state = ConnectionState.DISCONNECTED
    session = None


class MediaPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.manager = _Manager()
        self.queue = TransferQueue()
        self.page = MediaPage(self.manager, self.queue)

    def tearDown(self):
        self.page.close()

    def _connect(self):
        session = PhoneSession(
            AdbDevice("USB-SERIAL", "device", "usb", model="SM_S731U1"), None, None
        )
        self.page.set_connection(ConnectionState.USB, session)
        return session

    @staticmethod
    def _wait(predicate, timeout=3):
        app = QApplication.instance()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_parser_reads_track_state_and_no_session_or_unknown_format(self):
        parsed = parse_media_session(PLAYING_DUMP)
        self.assertTrue(parsed.available)
        self.assertTrue(parsed.session_active)
        self.assertEqual(
            (parsed.title, parsed.artist, parsed.album, parsed.playback_state, parsed.actions),
            ("Song Title", "Singer Name", "Album Name", "Playing", 404147),
        )
        no_sessions = parse_media_session(
            "Total sessions: 0 across 0 package(s)\nSessions Stack - have 0 sessions:\n"
        )
        self.assertTrue(no_sessions.available)
        self.assertFalse(no_sessions.session_active)
        self.assertFalse(parse_media_session("unrecognized output").available)

    def test_no_phone_and_hidden_tab_never_poll(self):
        with patch("phonebridge.media_page._read_media_session") as read:
            self.page.set_media_visible(True)
            self.page._poll_if_due()
            QApplication.processEvents()
            self.assertEqual(read.call_count, 0)
            self.assertEqual(self.page.status_label.text(), "No phone connected")
            self.assertFalse(any(button.isEnabled() for button in self._buttons()))

            self.page.set_media_visible(False)
            self._connect()
            self.page._poll_if_due()
            self.assertEqual(read.call_count, 0)
            self.assertFalse(self.page._timer.isActive())

    def test_no_session_controls_and_unparseable_status(self):
        with patch(
            "phonebridge.media_page._read_media_session",
            return_value=MediaSnapshot(available=True),
        ):
            self._connect()
            self.page.set_media_visible(True)
            self.assertTrue(self._wait(lambda: self.page._request_task is None))
            self.assertEqual(self.page.status_label.text(), "No media playing")
            self.assertTrue(self.page.play_button.isEnabled())
            self.assertFalse(self.page.pause_button.isEnabled())
            self.assertFalse(self.page.next_button.isEnabled())
            self.assertFalse(self.page.previous_button.isEnabled())
            self.assertTrue(self.page.pause_button.toolTip())

            self.page._last_poll_at = 0
            self.page._poll_if_due()
            self.assertTrue(self._wait(lambda: self.page._request_task is None))
            self.page._snapshot = MediaSnapshot(available=False, playback_state="Unknown")
            self.page._render_state()
            self.assertEqual(
                self.page.status_label.text(), "Media session unavailable on this device"
            )

    def test_action_bits_gate_next_and_previous_and_update_on_poll(self):
        next_only_dump = PLAYING_DUMP.replace("actions=404147", "actions=32")
        both_actions_dump = PLAYING_DUMP.replace("actions=404147", "actions=48")
        responses = iter((
            parse_media_session(next_only_dump),
            parse_media_session(both_actions_dump),
        ))
        with patch(
            "phonebridge.media_page._read_media_session",
            side_effect=lambda _serial: next(responses),
        ):
            self._connect()
            self.page.set_media_visible(True)
            self.assertTrue(self._wait(lambda: self.page._request_task is None))
            self.assertTrue(self.page.next_button.isEnabled())
            self.assertFalse(self.page.previous_button.isEnabled())
            self.assertEqual(
                self.page.previous_button.toolTip(),
                "Previous not available for this track",
            )

            self.page._last_poll_at = 0
            self.page._poll_if_due()
            self.assertTrue(self._wait(lambda: self.page._request_task is None))
            self.assertTrue(self.page.next_button.isEnabled())
            self.assertTrue(self.page.previous_button.isEnabled())
            self.assertEqual(self.page.previous_button.toolTip(), "")

    def test_media_keys_are_sent_from_worker_thread(self):
        self._connect()
        self.page._snapshot = parse_media_session(PLAYING_DUMP)
        self.page._render_state()
        self.assertEqual(self.page.title_label.text(), "Title: Song Title")
        self.assertEqual(self.page.artist_label.text(), "Artist: Singer Name")
        self.assertEqual(self.page.album_label.text(), "Album: Album Name")
        self.assertEqual(self.page.state_label.text(), "Playback state: Playing")
        self.assertFalse(self.page.play_button.isEnabled())
        self.assertTrue(self.page.pause_button.isEnabled())
        calls = []
        main_thread = threading.get_ident()

        def send(serial, keycode):
            calls.append((serial, keycode, threading.get_ident()))

        with patch("phonebridge.media_page._send_media_key", side_effect=send):
            for keycode in (
                "KEYCODE_MEDIA_PLAY",
                "KEYCODE_MEDIA_PAUSE",
                "KEYCODE_MEDIA_NEXT",
                "KEYCODE_MEDIA_PREVIOUS",
            ):
                self.page._send_key(keycode)
            self.assertTrue(self._wait(lambda: len(calls) == 4))
        self.assertEqual(
            {(serial, keycode) for serial, keycode, _thread in calls},
            {
                ("USB-SERIAL", "KEYCODE_MEDIA_PLAY"),
                ("USB-SERIAL", "KEYCODE_MEDIA_PAUSE"),
                ("USB-SERIAL", "KEYCODE_MEDIA_NEXT"),
                ("USB-SERIAL", "KEYCODE_MEDIA_PREVIOUS"),
            },
        )
        self.assertTrue(all(thread_id != main_thread for _, _, thread_id in calls))

    def test_polling_is_visible_only_throttled_and_skips_transfer(self):
        calls = []

        def read(serial):
            calls.append((serial, time.monotonic()))
            return parse_media_session(PLAYING_DUMP)

        with patch("phonebridge.media_page._read_media_session", side_effect=read):
            self._connect()
            self.page.set_media_visible(True)
            self.assertTrue(self._wait(lambda: len(calls) == 1))
            self.assertTrue(self.page._timer.isActive())
            self.assertEqual(self.page._timer.interval(), 5000)
            self.page._poll_if_due()
            QApplication.processEvents()
            self.assertEqual(len(calls), 1)

            self.page._last_poll_at = 0
            with patch.object(
                TransferQueue, "is_running", new_callable=PropertyMock, return_value=True
            ):
                self.page._poll_if_due()
                self.assertEqual(len(calls), 1)

            self.page.set_media_visible(False)
            self.assertFalse(self.page._timer.isActive())
            self.page._poll_if_due()
            self.assertEqual(len(calls), 1)

    def test_disconnect_clears_and_stops_polling(self):
        with patch(
            "phonebridge.media_page._read_media_session",
            return_value=parse_media_session(PLAYING_DUMP),
        ):
            self._connect()
            self.page.set_media_visible(True)
            self.assertTrue(self._wait(lambda: self.page._request_task is None))
            self.page.set_connection(ConnectionState.DISCONNECTED, None)
            self.assertEqual(self.page.status_label.text(), "No phone connected")
            self.assertEqual(self.page.title_label.text(), "—")
            self.assertFalse(self.page._timer.isActive())
            self.assertFalse(any(button.isEnabled() for button in self._buttons()))

    def _buttons(self):
        return (
            self.page.play_button,
            self.page.pause_button,
            self.page.next_button,
            self.page.previous_button,
        )


if __name__ == "__main__":
    unittest.main()
