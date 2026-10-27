"""Read and send SMS messages through the connected phone's ADB interface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import shlex
import subprocess
import time

from PySide6.QtCore import QThreadPool, Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import adb
from .device import ConnectionState, DeviceManager, PhoneSession
from .transfer_queue import TransferQueue
from .workers import FunctionTask


_ROW_START = re.compile(r"(?m)^Row:\s*\d+\s+_id=(-?\d+),")
_ROW_SUFFIX = re.compile(
    r",\s*date=(-?\d+)(?:,\s*read=(\d+))?(?:,\s*type=(\d+))?\s*$",
    re.DOTALL,
)
_PHONE_NUMBER = re.compile(r"^[+0-9().\-\s]{1,40}$")


@dataclass(frozen=True)
class SmsMessage:
    message_id: int
    thread_id: int | None
    address: str
    body: str
    date_ms: int
    read: int | None = None
    message_type: int | None = None


@dataclass(frozen=True)
class SmsSnapshot:
    messages: tuple[SmsMessage, ...]
    error: str = ""


def _parse_sms_rows(output: str) -> tuple[SmsMessage, ...]:
    starts = list(_ROW_START.finditer(output))
    messages: list[SmsMessage] = []
    for index, match in enumerate(starts):
        chunk = output[match.start():starts[index + 1].start() if index + 1 < len(starts) else len(output)]
        suffix = _ROW_SUFFIX.search(chunk)
        if suffix is None:
            continue
        prefix = chunk[:suffix.start()]
        thread_match = re.search(r"\bthread_id=(-?\d+),\s*address=(.*?),\s*body=", prefix, re.DOTALL)
        if thread_match:
            thread_id = int(thread_match.group(1))
            address = thread_match.group(2).strip()
        else:
            address_match = re.search(r"\baddress=(.*?),\s*body=", prefix, re.DOTALL)
            thread_id = None
            address = address_match.group(1).strip() if address_match else ""
        body_match = re.search(r"\bbody=(.*)$", prefix, re.DOTALL)
        body = body_match.group(1).strip() if body_match else ""
        messages.append(
            SmsMessage(
                message_id=int(match.group(1)),
                thread_id=thread_id,
                address=address,
                body=body,
                date_ms=int(suffix.group(1)),
                read=int(suffix.group(2)) if suffix.group(2) is not None else None,
                message_type=int(suffix.group(3)) if suffix.group(3) is not None else None,
            )
        )
    return tuple(sorted(messages, key=lambda item: item.date_ms, reverse=True))


def _query_sms(serial: str, uri: str, where: str = "") -> tuple[SmsMessage, ...]:
    command = (
        f"content query --uri {shlex.quote(uri)} "
        "--projection _id:thread_id:address:body:date:read:type"
    )
    if where:
        command += f" --where {shlex.quote(where)}"
    command += f" --sort {shlex.quote('date DESC')}"
    try:
        output = adb.shell(serial, command, timeout=25).stdout
    except subprocess.CalledProcessError as error:
        detail = f"{error.stdout or ''}\n{error.stderr or ''}"
        if "Permission Denial" in detail or "SecurityException" in detail:
            raise PermissionError("SMS reading is not available on this device via ADB") from error
        raise
    if "Permission Denial" in output or "SecurityException" in output:
        raise PermissionError("SMS reading is not available on this device via ADB")
    return _parse_sms_rows(output)


def _read_sms_snapshot(serial: str) -> SmsSnapshot:
    try:
        messages = _query_sms(serial, "content://sms")
        return SmsSnapshot(messages)
    except PermissionError as error:
        return SmsSnapshot((), str(error))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        return SmsSnapshot((), f"Could not read SMS over USB (ADB): {error}")


def _read_sms_thread(serial: str, thread_id: int | None, address: str) -> tuple[SmsMessage, ...]:
    if thread_id is not None:
        where = f"thread_id={thread_id}"
    else:
        escaped_address = address.replace("'", "''")
        where = f"address='{escaped_address}'"
    return _query_sms(serial, "content://sms", where)


def _send_sms_and_verify(serial: str, address: str, body: str) -> None:
    number = address.strip()
    if not _PHONE_NUMBER.fullmatch(number) or not any(character.isdigit() for character in number):
        raise ValueError("Enter a valid phone number before sending.")
    if not body.strip():
        raise ValueError("Enter a message before sending.")
    command = (
        f"service call isms 7 i32 0 s16 {shlex.quote('com.android.mms.service')} "
        f"s16 {shlex.quote(number)} s16 null s16 {shlex.quote(chr(39) + body + chr(39))} "
        "s16 null s16 null i32 1"
    )
    result = adb.shell(serial, command, timeout=30)
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    if re.search(r"Exception|Permission Denial", output, re.IGNORECASE):
        raise RuntimeError(f"The phone rejected the SMS service call: {output.strip()}")
    if "Result: Parcel(" not in output:
        raise RuntimeError("The SMS service call returned no result Parcel.")


def _format_date(timestamp_ms: int) -> str:
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000).astimezone().strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(timestamp_ms)


class SmsPage(QWidget):
    POLL_INTERVAL_MS = 10_000

    def __init__(self, manager: DeviceManager, transfer_queue: TransferQueue, parent=None) -> None:
        super().__init__(parent)
        self._transfer_queue = transfer_queue
        self._serial: str | None = None
        self._visible = False
        self._last_poll_at = 0.0
        self._task: FunctionTask | None = None
        self._task_serial: str | None = None
        self._generation = 0
        self._selected_thread: int | None = None
        self._selected_address = ""
        self._messages: tuple[SmsMessage, ...] = ()
        self._send_disabled = False

        self.status_label = QLabel("No phone connected")
        self.conversation_list = QListWidget()
        self.message_list = QListWidget()
        self.address_input = QLineEdit()
        self.address_input.setPlaceholderText("Phone number")
        self.message_input = QTextEdit()
        self.message_input.setPlaceholderText("Write a message")
        self.message_input.setMaximumHeight(100)
        self.history_note = QLabel(
            "Messages sent through Sidekick may not appear in the phone's Messages app sent history."
        )
        self.history_note.setWordWrap(True)
        self.send_button = QPushButton("Send")
        self.send_button.setEnabled(False)
        self.send_button.setToolTip("Connect a phone over USB to send SMS.")

        lists = QSplitter(Qt.Orientation.Horizontal)
        lists.addWidget(self.conversation_list)
        lists.addWidget(self.message_list)
        lists.setStretchFactor(0, 1)
        lists.setStretchFactor(1, 2)
        composer = QHBoxLayout()
        composer.addWidget(self.address_input, 1)
        composer.addWidget(self.send_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(self.status_label)
        layout.addWidget(lists, 1)
        layout.addWidget(self.message_input)
        layout.addWidget(self.history_note)
        layout.addLayout(composer)

        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_if_due)
        self.conversation_list.currentItemChanged.connect(self._conversation_selected)
        self.send_button.clicked.connect(self._send_current_message)
        self._transfer_queue.changed.connect(self._schedule_poll_check)
        manager.state_changed.connect(self.set_connection)
        self.set_connection(manager.state, manager.session)

    def set_sms_visible(self, visible: bool) -> None:
        visible = bool(visible)
        if visible == self._visible:
            return
        self._visible = visible
        if visible and self._serial is not None:
            self._timer.start()
            self._poll_if_due()
        else:
            self._timer.stop()

    def set_connection(self, state: object, session: object) -> None:
        if state == ConnectionState.USB and isinstance(session, PhoneSession):
            serial = session.device.serial
            if serial != self._serial:
                self._generation += 1
                self._serial = serial
                self._messages = ()
                self._clear_lists()
                self._send_disabled = False
            self._update_send_enabled()
            if self._visible:
                self._timer.start()
                self._poll_if_due()
            return
        self._generation += 1
        self._serial = None
        self._timer.stop()
        self._messages = ()
        self._selected_thread = None
        self._selected_address = ""
        self._clear_lists()
        self.status_label.setText("No phone connected")
        self.address_input.clear()
        self.message_input.clear()
        self._update_send_enabled()

    def _clear_lists(self) -> None:
        self.conversation_list.clear()
        self.message_list.clear()

    def _update_send_enabled(self) -> None:
        self.send_button.setEnabled(self._serial is not None and not self._send_disabled)
        self.address_input.setEnabled(self._serial is not None and not self._send_disabled)
        self.message_input.setEnabled(self._serial is not None and not self._send_disabled)
        if self._serial is None:
            self.send_button.setToolTip("Connect a phone over USB to send SMS.")
        elif self._send_disabled:
            self.send_button.setToolTip("SMS sending is unavailable after a send failure.")
        else:
            self.send_button.setToolTip("")

    def _poll_if_due(self) -> None:
        if (
            not self._visible or self._serial is None or self._task is not None
            or self._transfer_queue.is_running
            or time.monotonic() - self._last_poll_at < self.POLL_INTERVAL_MS / 1000
        ):
            return
        self._last_poll_at = time.monotonic()
        serial = self._serial
        self._task_serial = serial
        generation = self._generation
        task = FunctionTask(_read_sms_snapshot, serial)
        self._task = task
        task.signals.completed.connect(
            lambda result, error, expected=serial, gen=generation:
                self._finish_snapshot(expected, gen, result, error)
        )
        QThreadPool.globalInstance().start(task)

    def _schedule_poll_check(self) -> None:
        QTimer.singleShot(0, self._poll_if_due)

    def _finish_snapshot(self, serial: str, generation: int, result: object, error: object) -> None:
        self._task = None
        self._task_serial = None
        if serial != self._serial or generation != self._generation:
            return
        if error is not None or not isinstance(result, SmsSnapshot):
            self.status_label.setText(f"SMS read failed: {error or 'unknown error'}")
            return
        self._messages = result.messages
        self.status_label.setText(result.error or f"{len(result.messages)} messages")
        if result.error == "SMS reading is not available on this device via ADB":
            self._send_disabled = True
            self._update_send_enabled()
        self._render_conversations()

    def _render_conversations(self) -> None:
        selected = (self._selected_thread, self._selected_address)
        grouped: dict[tuple[int | None, str], list[SmsMessage]] = {}
        for message in self._messages:
            key = (message.thread_id, message.address)
            grouped.setdefault(key, []).append(message)
        conversations = sorted(
            grouped.items(), key=lambda pair: max(item.date_ms for item in pair[1]), reverse=True
        )
        self.conversation_list.blockSignals(True)
        self.conversation_list.clear()
        selection_index = -1
        for index, ((thread_id, address), messages) in enumerate(conversations):
            newest = max(messages, key=lambda item: item.date_ms)
            snippet = " ".join(newest.body.split())
            if len(snippet) > 100:
                snippet = snippet[:97] + "…"
            item = QListWidgetItem(f"{address}\n{snippet}\n{_format_date(newest.date_ms)}")
            item.setData(Qt.ItemDataRole.UserRole, (thread_id, address))
            self.conversation_list.addItem(item)
            if (thread_id, address) == selected:
                selection_index = index
        self.conversation_list.blockSignals(False)
        if selection_index < 0 and conversations:
            selection_index = 0
        if selection_index >= 0:
            self.conversation_list.setCurrentRow(selection_index)
        else:
            self.message_list.clear()

    def _conversation_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        data = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, tuple) or len(data) != 2:
            return
        self._selected_thread, self._selected_address = data
        self.address_input.setText(self._selected_address)
        if self._serial is None:
            return
        if self._task is not None:
            return
        serial = self._serial
        generation = self._generation
        task = FunctionTask(_read_sms_thread, serial, self._selected_thread, self._selected_address)
        self._task = task
        self._task_serial = serial
        task.signals.completed.connect(
            lambda result, error, expected=serial, gen=generation:
                self._finish_thread(expected, gen, result, error)
        )
        QThreadPool.globalInstance().start(task)

    def _finish_thread(self, serial: str, generation: int, result: object, error: object) -> None:
        self._task = None
        self._task_serial = None
        if serial != self._serial or generation != self._generation:
            return
        self.message_list.clear()
        if error is not None or not isinstance(result, tuple):
            self.status_label.setText(f"Could not load this conversation: {error or 'unknown error'}")
            return
        for message in sorted(result, key=lambda item: item.date_ms):
            item = QListWidgetItem(f"{_format_date(message.date_ms)}\n{message.body}")
            item.setData(Qt.ItemDataRole.UserRole, message.message_id)
            self.message_list.addItem(item)
        self.status_label.setText(f"Conversation with {self._selected_address}")

    def _send_current_message(self) -> None:
        if self._serial is None or self._send_disabled or self._task is not None:
            return
        number = self.address_input.text().strip()
        body = self.message_input.toPlainText()
        serial = self._serial
        generation = self._generation
        task = FunctionTask(_send_sms_and_verify, serial, number, body)
        self._task = task
        self._task_serial = serial
        self.send_button.setEnabled(False)
        task.signals.completed.connect(
            lambda result, error, expected=serial, gen=generation:
                self._finish_send(expected, gen, result, error)
        )
        QThreadPool.globalInstance().start(task)

    def _finish_send(self, serial: str, generation: int, _result: object, error: object) -> None:
        self._task = None
        self._task_serial = None
        if serial != self._serial or generation != self._generation:
            return
        if error is not None:
            self._send_disabled = True
            self.status_label.setText(f"SMS was not confirmed: {error}")
            self.address_input.setEnabled(False)
            self.message_input.setEnabled(False)
            self._update_send_enabled()
            return
        self.message_input.clear()
        self.status_label.setText("SMS service call completed without an error Parcel.")
        self._update_send_enabled()
        self._last_poll_at = 0
        self._poll_if_due()
