"""Own and monitor the single phone session shown by Phonebridge."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import subprocess
from typing import Optional

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal

from . import adb
from .workers import FunctionTask


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    UNAUTHORIZED = "UNAUTHORIZED"
    USB = "USB"


@dataclass(frozen=True)
class PhoneSession:
    device: adb.AdbDevice
    battery_level: Optional[int]
    charging: Optional[bool]


def _discover_phone(
    preferred_serial: Optional[str], current_serial: Optional[str]
) -> tuple[list[adb.AdbDevice], Optional[adb.AdbDevice], Optional[PhoneSession]]:
    devices = adb.list_devices(timeout=10)
    usable = [device for device in devices if device.is_usable]
    selected = next(
        (device for device in usable if device.serial == preferred_serial),
        usable[0] if usable else None,
    )
    session = None
    if selected is not None and selected.serial != current_serial:
        session = _read_session(selected)
    return devices, selected, session


def _read_session(device: adb.AdbDevice) -> PhoneSession:
    level: Optional[int] = None
    charging: Optional[bool] = None
    try:
        output = adb.shell(device.serial, "dumpsys battery", timeout=10).stdout
        level_match = re.search(r"(?m)^\s*level:\s*(\d+)\s*$", output)
        status_match = re.search(r"(?m)^\s*status:\s*(\d+)\s*$", output)
        if level_match:
            level = int(level_match.group(1))
        if status_match:
            charging = int(status_match.group(1)) == 2
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        pass
    return PhoneSession(device=device, battery_level=level, charging=charging)


class DeviceManager(QObject):
    """Poll ADB in a worker and expose at most one authorized USB session."""

    state_changed = Signal(object, object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._state = ConnectionState.DISCONNECTED
        self._session: Optional[PhoneSession] = None
        self._preferred_serial: Optional[str] = None
        self._poll_task: Optional[FunctionTask] = None
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self.poll)
        self.poll()
        self._timer.start()

    @property
    def session(self) -> Optional[PhoneSession]:
        return self._session

    @property
    def state(self) -> ConnectionState:
        return self._state

    def poll(self) -> None:
        if self._poll_task is not None:
            return
        current_serial = self._session.device.serial if self._session else None
        task = FunctionTask(_discover_phone, self._preferred_serial, current_serial)
        self._poll_task = task
        task.signals.completed.connect(self._finish_poll)
        QThreadPool.globalInstance().start(task)

    def _finish_poll(self, result: object, error: object) -> None:
        self._poll_task = None
        old_state = self._state
        old_session = self._session

        if error is not None:
            devices: list[adb.AdbDevice] = []
            selected = None
            new_session = None
        else:
            devices, selected, new_session = result  # type: ignore[misc]

        if selected is not None:
            if new_session is not None:
                self._session = new_session
                self._preferred_serial = selected.serial
            self._state = ConnectionState.USB
        else:
            self._session = None
            self._state = (
                ConnectionState.UNAUTHORIZED
                if any(device.state == "unauthorized" for device in devices)
                else ConnectionState.DISCONNECTED
            )

        if self._state != old_state or self._session != old_session:
            self.state_changed.emit(self._state, self._session)
