"""device.py - Owns the connected phone as a single session."""

from dataclasses import dataclass
from enum import Enum
import re
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from phonebridge.adb import AdbDevice, list_devices, shell


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    UNAUTHORIZED = "UNAUTHORIZED"
    USB = "USB"


def _read_battery(serial: str) -> tuple[Optional[int], Optional[bool]]:
    """Read battery level and charging state once via dumpsys battery.

    If the call fails or outputs unexpected content, returns (None, None) without crashing.
    """
    try:
        proc = shell(serial, "dumpsys battery", timeout=5.0)
        if proc.returncode != 0:
            return None, None
        output = proc.stdout

        level: Optional[int] = None
        level_m = re.search(r"^\s*level:\s*(\d+)", output, re.MULTILINE)
        if level_m:
            level = int(level_m.group(1))

        charging: Optional[bool] = None
        # BatteryManager status constants:
        # 1: BATTERY_STATUS_UNKNOWN
        # 2: BATTERY_STATUS_CHARGING
        # 3: BATTERY_STATUS_DISCHARGING
        # 4: BATTERY_STATUS_NOT_CHARGING
        # 5: BATTERY_STATUS_FULL
        status_m = re.search(r"^\s*status:\s*(\d+)", output, re.MULTILINE)
        if status_m:
            status_val = int(status_m.group(1))
            charging = (status_val == 2)
        else:
            # Fallback to power source indicators if status line is absent
            ac_m = re.search(r"^\s*AC powered:\s*(true|false)", output, re.MULTILINE | re.IGNORECASE)
            usb_m = re.search(r"^\s*USB powered:\s*(true|false)", output, re.MULTILINE | re.IGNORECASE)
            wire_m = re.search(r"^\s*Wireless powered:\s*(true|false)", output, re.MULTILINE | re.IGNORECASE)
            if ac_m or usb_m or wire_m:
                charging = any(
                    m and m.group(1).lower() == "true"
                    for m in (ac_m, usb_m, wire_m)
                )

        return level, charging
    except Exception:
        return None, None


@dataclass
class PhoneSession:
    """Holds the AdbDevice plus battery level and charging state read at session start."""

    device: AdbDevice
    battery_level: Optional[int] = None
    charging: Optional[bool] = None

    @classmethod
    def from_device(cls, device: AdbDevice) -> "PhoneSession":
        level, charging = _read_battery(device.serial)
        return cls(device=device, battery_level=level, charging=charging)

    create = from_device

    @property
    def is_charging(self) -> Optional[bool]:
        return self.charging


class DeviceManager(QObject):
    """Monitors connected ADB devices via periodic polling and manages the active PhoneSession."""

    state_changed = Signal(object, object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._state: ConnectionState = ConnectionState.DISCONNECTED
        self._session: Optional[PhoneSession] = None
        self._last_selected_serial: Optional[str] = None
        self._has_polled_once: bool = False

        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self.poll)
        self._timer.start()

        # Perform initial poll
        self.poll()

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def session(self) -> Optional[PhoneSession]:
        return self._session

    def stop(self) -> None:
        """Stop polling timer."""
        self._timer.stop()

    def poll(self) -> None:
        """Polls `adb devices -l` and updates the active session/state."""
        try:
            devices = list_devices()
        except Exception:
            devices = []

        usable_usb = [d for d in devices if d.is_usable]
        unauthorized_usb = [
            d for d in devices if d.state == "unauthorized" and d.transport == "usb"
        ]

        new_state: ConnectionState
        new_session: Optional[PhoneSession] = None

        if usable_usb:
            new_state = ConnectionState.USB

            # If multiple USB phones are attached, prefer the serial chosen last time
            selected_device: Optional[AdbDevice] = None
            if self._last_selected_serial:
                for d in usable_usb:
                    if d.serial == self._last_selected_serial:
                        selected_device = d
                        break

            if selected_device is None:
                selected_device = usable_usb[0]

            self._last_selected_serial = selected_device.serial

            # If the device has not changed, keep the existing session (battery is read once)
            if (
                self._state == ConnectionState.USB
                and self._session is not None
                and self._session.device.serial == selected_device.serial
            ):
                new_session = self._session
            else:
                new_session = PhoneSession.from_device(selected_device)

        elif unauthorized_usb:
            new_state = ConnectionState.UNAUTHORIZED
            new_session = None
        else:
            new_state = ConnectionState.DISCONNECTED
            new_session = None

        changed = False
        if not self._has_polled_once:
            changed = True
        elif new_state != self._state:
            changed = True
        elif (self._session is None) != (new_session is None):
            changed = True
        elif self._session is not None and new_session is not None:
            if self._session.device.serial != new_session.device.serial:
                changed = True

        self._has_polled_once = True
        self._state = new_state
        self._session = new_session

        if changed:
            self.state_changed.emit(self._state, self._session)
