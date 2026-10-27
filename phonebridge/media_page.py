"""USB ADB media-session display and transport controls."""

from __future__ import annotations

from dataclasses import dataclass
import re
import subprocess
import time

from PySide6.QtCore import QThreadPool, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import adb
from .device import ConnectionState, DeviceManager, PhoneSession
from .transfer_queue import TransferQueue
from .workers import FunctionTask


_SESSION_HEADER = re.compile(r"^\s{4,}.+\(userId=\d+\)\s*$")
_PLAYBACK_STATE = re.compile(
    r"state=PlaybackState\s*\{\s*state=([A-Z_]+)\((\d+)\)", re.IGNORECASE
)
_PLAYBACK_ACTIONS = re.compile(r"\bactions=(\d+)\b")
ACTION_SKIP_TO_PREVIOUS = 0x10
ACTION_SKIP_TO_NEXT = 0x20
_METADATA_KEY = re.compile(
    r"(?:android\.media\.metadata\.|METADATA_KEY_)(TITLE|ARTIST|ALBUM)\s*=\s*(.*)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MediaSnapshot:
    available: bool
    session_active: bool = False
    playback_state: str = "Stopped"
    title: str = ""
    artist: str = ""
    album: str = ""
    actions: int | None = None

    @property
    def has_track(self) -> bool:
        return bool(self.title or self.artist or self.album)


def _clean_metadata(value: str) -> str:
    cleaned = value.strip().strip('"\'')
    if not cleaned or cleaned.lower() in {"null", "none", "<null>"}:
        return ""
    return cleaned


def _session_blocks(output: str) -> list[str] | None:
    stack_start = output.find("Sessions Stack - have")
    if stack_start < 0:
        if re.search(r"(?m)^Total sessions:\s*0\b", output):
            return []
        return None
    region = output[stack_start:]
    end_markers = ("\nisAppCastingOn:", "\nMediaSession events", "\nAudio playback")
    end_positions = [region.find(marker) for marker in end_markers if region.find(marker) >= 0]
    if end_positions:
        region = region[:min(end_positions)]

    blocks: list[str] = []
    current: list[str] | None = None
    for line in region.splitlines()[1:]:
        if _SESSION_HEADER.match(line):
            if current is not None:
                blocks.append("\n".join(current))
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append("\n".join(current))
    count_match = re.search(r"Sessions Stack - have\s+(\d+)\s+sessions", region)
    if count_match and int(count_match.group(1)) == 0:
        return []
    return blocks


def parse_media_session(output: str) -> MediaSnapshot:
    """Parse the Android media_session dump format observed on the target phone."""
    blocks = _session_blocks(output)
    if blocks is None:
        return MediaSnapshot(available=False, playback_state="Unknown")
    if not blocks:
        return MediaSnapshot(available=True)

    active_blocks = [
        block for block in blocks
        if re.search(r"(?m)^\s*(?:SUMMARY:.*\s)?active=true\s*$", block)
    ]
    if not active_blocks:
        return MediaSnapshot(available=True)

    for block in active_blocks:
        state_match = _PLAYBACK_STATE.search(block)
        actions_match = _PLAYBACK_ACTIONS.search(block)
        metadata_lines = [
            line for line in block.splitlines()
            if re.search(r"(?i)\bmetadata\s*:", line)
        ]
        key_values: dict[str, str] = {}
        for match in _METADATA_KEY.finditer(block):
            key_values[match.group(1).lower()] = _clean_metadata(match.group(2))

        if state_match is None and not metadata_lines and not key_values:
            continue

        title = key_values.get("title", "")
        artist = key_values.get("artist", "")
        album = key_values.get("album", "")
        if not any((title, artist, album)):
            description_match = re.search(
                r"metadata:\s*size=\d+\s*,\s*description=(.*)$",
                block, re.IGNORECASE | re.MULTILINE,
            )
            if description_match:
                description = [
                    _clean_metadata(part)
                    for part in description_match.group(1).split(",")
                ]
                description += [""] * (3 - len(description))
                title, artist, album = description[:3]

        if state_match:
            state_name, state_number = state_match.groups()
            playback_state = {
                "3": "Playing",
                "2": "Paused",
                "1": "Stopped",
            }.get(state_number, "Unknown")
            if state_name.upper() == "NONE":
                playback_state = "Unknown"
        else:
            playback_state = "Unknown"
        return MediaSnapshot(
            available=True,
            session_active=True,
            playback_state=playback_state,
            title=title,
            artist=artist,
            album=album,
            actions=int(actions_match.group(1)) if actions_match else None,
        )
    return MediaSnapshot(available=False, playback_state="Unknown")


def _read_media_session(serial: str) -> MediaSnapshot:
    output = adb.shell(serial, "dumpsys media_session", timeout=15).stdout
    return parse_media_session(output)


def _send_media_key(serial: str, keycode: str) -> None:
    adb.shell(serial, f"input keyevent {keycode}", timeout=10)


class MediaPage(QWidget):
    """Poll media only while this page is visible and the phone is idle from transfers."""

    POLL_INTERVAL_MS = 5_000

    def __init__(
        self, manager: DeviceManager, transfer_queue: TransferQueue, parent=None
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._transfer_queue = transfer_queue
        self._serial: str | None = None
        self._visible = False
        self._request_task: FunctionTask | None = None
        self._request_serial: str | None = None
        self._request_expected_generation = 0
        self._generation = 0
        self._last_poll_at = 0.0
        self._snapshot = MediaSnapshot(available=True)

        self.title_label = QLabel("—")
        self.artist_label = QLabel("—")
        self.album_label = QLabel("—")
        self.state_label = QLabel("Playback state: Stopped")
        self.status_label = QLabel("No phone connected")
        self.transport_label = QLabel("Transport: USB (ADB)")
        for label in (self.title_label, self.artist_label, self.album_label):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.play_button = QPushButton("Play")
        self.pause_button = QPushButton("Pause")
        self.next_button = QPushButton("Next")
        self.previous_button = QPushButton("Previous")
        controls = QHBoxLayout()
        controls.addWidget(self.previous_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.next_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(self.title_label)
        layout.addWidget(self.artist_label)
        layout.addWidget(self.album_label)
        layout.addWidget(self.state_label)
        layout.addLayout(controls)
        layout.addWidget(self.transport_label)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_if_due)
        self._transfer_queue.changed.connect(self._schedule_poll_check)
        self.play_button.clicked.connect(lambda: self._send_key("KEYCODE_MEDIA_PLAY"))
        self.pause_button.clicked.connect(lambda: self._send_key("KEYCODE_MEDIA_PAUSE"))
        self.next_button.clicked.connect(lambda: self._send_key("KEYCODE_MEDIA_NEXT"))
        self.previous_button.clicked.connect(
            lambda: self._send_key("KEYCODE_MEDIA_PREVIOUS")
        )
        self.set_connection(manager.state, manager.session)
        self._render_state()

    def set_media_visible(self, visible: bool) -> None:
        visible = bool(visible)
        if self._visible == visible:
            return
        self._visible = visible
        self._generation += 1
        if visible and self._serial is not None:
            self._timer.start()
            self._poll_if_due()
        else:
            self._timer.stop()

    def set_connection(self, state: object, session: object) -> None:
        if state == ConnectionState.USB and isinstance(session, PhoneSession):
            new_serial = session.device.serial
            if new_serial != self._serial:
                self._generation += 1
                self._serial = new_serial
                self._snapshot = MediaSnapshot(available=True)
            self._render_state()
            if self._visible and self._serial is not None:
                self._timer.start()
                self._poll_if_due()
            return

        self._generation += 1
        self._serial = None
        self._timer.stop()
        self._snapshot = MediaSnapshot(available=True)
        self.title_label.setText("—")
        self.artist_label.setText("—")
        self.album_label.setText("—")
        self.state_label.setText("Playback state: Unknown")
        self.status_label.setText("No phone connected")
        self._render_controls()

    def _render_state(self) -> None:
        if self._serial is None:
            self.status_label.setText("No phone connected")
            self.state_label.setText("Playback state: Unknown")
            self.title_label.setText("—")
            self.artist_label.setText("—")
            self.album_label.setText("—")
        elif not self._snapshot.available:
            self.status_label.setText("Media session unavailable on this device")
            self.state_label.setText("Playback state: Unknown")
            self.title_label.setText("—")
            self.artist_label.setText("—")
            self.album_label.setText("—")
        elif (
            not self._snapshot.session_active
            or (not self._snapshot.has_track and self._snapshot.playback_state != "Playing")
        ):
            self.status_label.setText("No media playing")
            self.state_label.setText(
                f"Playback state: {self._snapshot.playback_state}"
            )
            self.title_label.setText("—")
            self.artist_label.setText("—")
            self.album_label.setText("—")
        else:
            self.status_label.setText("")
            self.state_label.setText(
                f"Playback state: {self._snapshot.playback_state}"
            )
            self.title_label.setText(f"Title: {self._snapshot.title or '—'}")
            self.artist_label.setText(f"Artist: {self._snapshot.artist or '—'}")
            self.album_label.setText(f"Album: {self._snapshot.album or '—'}")
        self._render_controls()

    def _render_controls(self) -> None:
        connected = self._serial is not None
        has_media = (
            self._snapshot.available
            and self._snapshot.session_active
            and (
                self._snapshot.has_track
                or self._snapshot.playback_state == "Playing"
            )
        )
        self.play_button.setEnabled(connected)
        is_playing = self._snapshot.playback_state == "Playing"
        self.play_button.setEnabled(connected and not is_playing)
        self.pause_button.setEnabled(connected and has_media and is_playing)
        self.next_button.setEnabled(
            connected
            and has_media
            and self._snapshot.actions is not None
            and bool(self._snapshot.actions & ACTION_SKIP_TO_NEXT)
        )
        self.previous_button.setEnabled(
            connected
            and has_media
            and self._snapshot.actions is not None
            and bool(self._snapshot.actions & ACTION_SKIP_TO_PREVIOUS)
        )

        if not connected:
            play_reason = pause_reason = "Connect a phone over USB to control media."
            next_reason = previous_reason = play_reason
        elif not self._snapshot.available:
            play_reason = "Media session data is unavailable on this device."
            pause_reason = next_reason = previous_reason = play_reason
        elif not has_media:
            play_reason = "No active media session; Play may start or resume playback."
            pause_reason = "Nothing is currently playing."
            next_reason = "Next not available for this track"
            previous_reason = "Previous not available for this track"
        else:
            play_reason = "Playback is already playing." if is_playing else ""
            pause_reason = "" if is_playing else "Playback is not currently playing."
            actions = self._snapshot.actions or 0
            next_reason = "" if actions & ACTION_SKIP_TO_NEXT else "Next not available for this track"
            previous_reason = "" if actions & ACTION_SKIP_TO_PREVIOUS else "Previous not available for this track"
        self.play_button.setToolTip(play_reason)
        self.pause_button.setToolTip(pause_reason)
        self.next_button.setToolTip(next_reason)
        self.previous_button.setToolTip(previous_reason)

    def _poll_if_due(self) -> None:
        if (
            not self._visible
            or self._serial is None
            or self._request_task is not None
            or self._transfer_queue.is_running
        ):
            return
        now = time.monotonic()
        if now - self._last_poll_at < self.POLL_INTERVAL_MS / 1000:
            return
        serial = self._serial
        self._request_serial = serial
        generation = self._generation
        self._request_expected_generation = generation
        self._last_poll_at = now
        task = FunctionTask(_read_media_session, serial)
        self._request_task = task
        task.signals.completed.connect(self._finish_poll_result)
        QThreadPool.globalInstance().start(task)

    def _schedule_poll_check(self) -> None:
        QTimer.singleShot(0, self._poll_if_due)

    def _finish_poll_result(self, result: object, error: object) -> None:
        self._finish_poll(
            self._request_serial or "",
            self._request_expected_generation,
            result,
            error,
        )

    def _finish_poll(
        self, serial: str, generation: int, result: object, error: object
    ) -> None:
        self._request_task = None
        self._request_serial = None
        if generation != self._generation or serial != self._serial:
            if self._visible and self._serial is not None:
                self._poll_if_due()
            return
        if error is not None or not isinstance(result, MediaSnapshot):
            self._snapshot = MediaSnapshot(available=False, playback_state="Unknown")
        else:
            self._snapshot = result
        self._render_state()

    def _send_key(self, keycode: str) -> None:
        if self._serial is None:
            return
        task = FunctionTask(_send_media_key, self._serial, keycode)
        task.signals.completed.connect(self._finish_key)
        QThreadPool.globalInstance().start(task)

    def _finish_key(self, _result: object, error: object) -> None:
        if error is not None:
            self.status_label.setText("Could not send the media key over USB (ADB).")
