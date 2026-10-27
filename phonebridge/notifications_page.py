"""Read active notifications from the connected phone over USB ADB."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
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
    QVBoxLayout,
    QWidget,
)

from . import adb
from .device import ConnectionState, DeviceManager, PhoneSession
from .transfer_queue import TransferQueue
from .workers import FunctionTask


_RECORD_START = re.compile(
    r"(?m)^\s{4}NotificationRecord\([^:]*:\s*pkg=(\S+)\s+user=.*?"
    r"\bid=(-?\d+)\s+tag=(.*?)\s+importance=(\d+)\s+key=(.*?)"
    r"\s*:\s*Notification\("
)


@dataclass(frozen=True)
class ActiveNotification:
    key: str
    package_name: str
    notification_id: int
    tag: str
    title: str
    text: str
    timestamp_ms: int
    dismissable: bool = False
    replyable: bool = False


@dataclass(frozen=True)
class NotificationSnapshot:
    notifications: tuple[ActiveNotification, ...]
    error: str = ""


def _extra_string(block: str, name: str) -> str:
    match = re.search(
        rf"(?m)^[ \t]*{re.escape(name)}=[A-Za-z_$][\w.$]* \((.*)\)[ \t]*$",
        block,
    )
    if not match:
        return ""
    value = match.group(1).strip()
    return "" if value.lower() in {"null", "none"} else value


def parse_notification_dump(output: str) -> tuple[ActiveNotification, ...]:
    """Parse current active NotificationRecord entries from dumpsys output."""
    list_start = output.find("Notification List:")
    if list_start < 0:
        return ()
    end_markers = ("\n  TimeoutPendingIntent:", "\n  NotificationReminder", "\n  History Notification List:")
    end_positions = [output.find(marker, list_start) for marker in end_markers]
    end_positions = [position for position in end_positions if position >= 0]
    section = output[:min(end_positions)] if end_positions else output
    starts = list(_RECORD_START.finditer(section))
    notifications: list[ActiveNotification] = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(section)
        block = section[match.start():end]
        timestamp_match = re.search(r"\bwhen=(\d+)(?:/\d+)?", block)
        if timestamp_match is None or int(timestamp_match.group(1)) <= 0:
            timestamp_match = re.search(r"\bmCreationTimeMs=(\d+)", block)
        notifications.append(
            ActiveNotification(
                key=match.group(5).strip(),
                package_name=match.group(1),
                notification_id=int(match.group(2)),
                tag="" if match.group(3) == "null" else match.group(3),
                title=_extra_string(block, "android.title"),
                text=(
                    _extra_string(block, "android.text")
                    or _extra_string(block, "android.bigText")
                ),
                timestamp_ms=int(timestamp_match.group(1)) if timestamp_match else 0,
            )
        )
    return tuple(
        sorted(notifications, key=lambda item: item.timestamp_ms, reverse=True)
    )


def _read_notifications(serial: str) -> NotificationSnapshot:
    try:
        output = adb.shell(
            serial, "dumpsys notification --noredact", timeout=20
        ).stdout
        return NotificationSnapshot(parse_notification_dump(output))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        return NotificationSnapshot((), f"Could not read notifications over USB (ADB): {error}")


def _format_timestamp(timestamp_ms: int) -> str:
    if timestamp_ms <= 0:
        return "Time unavailable"
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (OverflowError, OSError, ValueError):
        return str(timestamp_ms)


class NotificationsPage(QWidget):
    POLL_INTERVAL_MS = 5_000

    def __init__(
        self, manager: DeviceManager, transfer_queue: TransferQueue, parent=None
    ) -> None:
        super().__init__(parent)
        self._transfer_queue = transfer_queue
        self._serial: str | None = None
        self._visible = False
        self._last_poll_at = 0.0
        self._task: FunctionTask | None = None
        self._generation = 0
        self._notifications: tuple[ActiveNotification, ...] = ()

        self.status_label = QLabel("No phone connected")
        self.notification_list = QListWidget()
        self.capability_label = QLabel(
            "Dismiss and inline reply are not exposed by this device's ADB notification commands."
        )
        self.capability_label.setWordWrap(True)
        self.detail_label = QLabel("Select a notification to view its details.")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.setEnabled(False)
        self.dismiss_button.setToolTip(
            "This device does not expose an ADB command to dismiss notifications."
        )
        self.reply_input = QLineEdit()
        self.reply_input.setPlaceholderText("Inline reply unavailable via ADB")
        self.reply_input.setEnabled(False)
        self.reply_input.setToolTip(
            "cmd notification does not expose a reply action for this notification."
        )
        self.reply_button = QPushButton("Reply")
        self.reply_button.setEnabled(False)
        self.reply_button.setToolTip(self.reply_input.toolTip())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(self.status_label)
        layout.addWidget(self.notification_list, 1)
        layout.addWidget(self.detail_label)
        actions = QHBoxLayout()
        actions.addWidget(self.dismiss_button)
        actions.addWidget(self.reply_input, 1)
        actions.addWidget(self.reply_button)
        layout.addLayout(actions)
        layout.addWidget(self.capability_label)

        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_if_due)
        self.notification_list.currentItemChanged.connect(self._selection_changed)
        self._transfer_queue.changed.connect(self._schedule_poll_check)
        manager.state_changed.connect(self.set_connection)
        self.set_connection(manager.state, manager.session)

    def set_notifications_visible(self, visible: bool) -> None:
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
                self._notifications = ()
                self.notification_list.clear()
                self.detail_label.setText("Select a notification to view its details.")
            if self._visible:
                self._timer.start()
                self._poll_if_due()
            return
        self._generation += 1
        self._serial = None
        self._timer.stop()
        self._notifications = ()
        self.notification_list.clear()
        self.detail_label.setText("Select a notification to view its details.")
        self.status_label.setText("No phone connected")

    def _poll_if_due(self) -> None:
        if (
            not self._visible
            or self._serial is None
            or self._task is not None
            or self._transfer_queue.is_running
            or time.monotonic() - self._last_poll_at < self.POLL_INTERVAL_MS / 1000
        ):
            return
        serial = self._serial
        generation = self._generation
        self._last_poll_at = time.monotonic()
        task = FunctionTask(_read_notifications, serial)
        self._task = task
        task.signals.completed.connect(
            lambda result, error, expected=serial, gen=generation:
                self._finish_poll(expected, gen, result, error)
        )
        QThreadPool.globalInstance().start(task)

    def _schedule_poll_check(self) -> None:
        QTimer.singleShot(0, self._poll_if_due)

    def _finish_poll(
        self, serial: str, generation: int, result: object, error: object
    ) -> None:
        self._task = None
        if serial != self._serial or generation != self._generation:
            return
        if error is not None or not isinstance(result, NotificationSnapshot):
            self.status_label.setText(
                f"Notification read failed: {error or 'unknown error'}"
            )
            return
        self._notifications = result.notifications
        self.status_label.setText(
            result.error or f"{len(result.notifications)} active notifications"
        )
        self._render_notifications()

    def _render_notifications(self) -> None:
        selected_key = ""
        current = self.notification_list.currentItem()
        if current is not None:
            selected_key = str(current.data(Qt.ItemDataRole.UserRole) or "")
        self.notification_list.blockSignals(True)
        self.notification_list.clear()
        selected_index = -1
        for index, notification in enumerate(self._notifications):
            title = notification.title or "(no title)"
            snippet = " ".join(notification.text.split())
            if len(snippet) > 180:
                snippet = snippet[:177] + "…"
            item = QListWidgetItem(
                f"{notification.package_name} — {title}\n{snippet}\n"
                f"{_format_timestamp(notification.timestamp_ms)}\nKey: {notification.key}"
            )
            item.setData(Qt.ItemDataRole.UserRole, notification.key)
            self.notification_list.addItem(item)
            if notification.key == selected_key:
                selected_index = index
        self.notification_list.blockSignals(False)
        if selected_index >= 0:
            self.notification_list.setCurrentRow(selected_index)
        elif self.notification_list.count():
            self.notification_list.setCurrentRow(0)
        else:
            self.detail_label.setText("No active notifications")

    def _selection_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            self.detail_label.setText("No active notifications")
            self.dismiss_button.setEnabled(False)
            self.reply_input.setEnabled(False)
            self.reply_button.setEnabled(False)
            return
        key = str(current.data(Qt.ItemDataRole.UserRole) or "")
        notification = next(
            (item for item in self._notifications if item.key == key), None
        )
        if notification is None:
            return
        self.dismiss_button.setEnabled(notification.dismissable)
        self.reply_input.setEnabled(notification.replyable)
        self.reply_button.setEnabled(notification.replyable)
        self.dismiss_button.setToolTip(
            "" if notification.dismissable
            else "This device does not expose an ADB command to dismiss notifications."
        )
        reply_reason = (
            "" if notification.replyable
            else "cmd notification does not expose a reply action for this notification."
        )
        self.reply_input.setToolTip(reply_reason)
        self.reply_button.setToolTip(reply_reason)
        self.detail_label.setText(
            f"{notification.package_name}\n{notification.title or '(no title)'}\n"
            f"{notification.text or '(no text)'}\n"
            f"{_format_timestamp(notification.timestamp_ms)}\n"
            f"ID: {notification.notification_id}; tag: {notification.tag or '—'}\n"
            f"Key: {notification.key}"
        )
