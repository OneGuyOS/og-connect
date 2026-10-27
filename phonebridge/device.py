"""Own and monitor the single phone session shown by Phonebridge."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import subprocess
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from . import adb


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    UNAUTHORIZED = "UNAUTHORIZED"
    USB = "USB"


@dataclass(frozen=True)
class PhoneSession:
    device: adb.AdbDevice
    battery_level: Optional[int]
    charging: Optional[bool]


class DeviceManager(QObject):
    """Poll ADB and expose at most one authorized USB phone session."""

    state_changed = Signal(object, object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._state = ConnectionState.DISCONNECTED
        self._session: Optional[PhoneSession] = None
        self._preferred_serial: Optional[str] = None
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
        try:
            devices = adb.list_devices()
        except (OSError, subprocess.SubprocessError):  # keep polling if adb is unavailable
            devices = []

        usable = [device for device in devices if device.is_usable]
        selected = next(
            (device for device in usable if device.serial == self._preferred_serial),
            usable[0] if usable else None,
        )

        if selected is not None:
            if self._session is None or self._session.device.serial != selected.serial:
                self._preferred_serial = selected.serial
                self._session = self._read_session(selected)
            new_state = ConnectionState.USB
        else:
            self._session = None
            new_state = (
                ConnectionState.UNAUTHORIZED
                if any(device.state == "unauthorized" for device in devices)
                else ConnectionState.DISCONNECTED
            )

        if new_state != self._state or self._session != getattr(self, "_emitted_session", None):
            self._state = new_state
            self._emitted_session = self._session
            self.state_changed.emit(self._state, self._session)

    def _read_session(self, device: adb.AdbDevice) -> PhoneSession:
        level: Optional[int] = None
        charging: Optional[bool] = None
        try:
            output = adb.shell(device.serial, "dumpsys battery").stdout
            level_match = re.search(r"(?m)^\s*level:\s*(\d+)\s*$", output)
            status_match = re.search(r"(?m)^\s*status:\s*(\d+)\s*$", output)
            if level_match:
                level = int(level_match.group(1))
            if status_match:
                # Android status 2 means charging; 5 means fully charged.
                charging = int(status_match.group(1)) == 2
        except (OSError, RuntimeError, ValueError):
            pass
        return PhoneSession(device=device, battery_level=level, charging=charging)

