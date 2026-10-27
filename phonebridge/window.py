"""Minimal connection status window."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QMainWindow

from .device import ConnectionState, DeviceManager, PhoneSession


class MainWindow(QMainWindow):
    def __init__(self, manager: DeviceManager) -> None:
        super().__init__()
        self.setWindowTitle("Phonebridge")
        self._manager = manager
        self._status = QLabel()
        self._status.setMargin(20)
        self.setCentralWidget(self._status)
        self._manager.state_changed.connect(self._update_status)
        self._update_status(self._manager.state, self._manager.session)

    def _update_status(self, state: object, session: object) -> None:
        if state == ConnectionState.UNAUTHORIZED:
            text = "Phone unauthorized — check the prompt on your phone"
        elif state == ConnectionState.USB and isinstance(session, PhoneSession):
            model = session.device.model or "unknown model"
            if session.battery_level is None:
                battery = "battery unknown"
            else:
                charge_text = "charging" if session.charging else "not charging"
                battery = f"battery {session.battery_level}% ({charge_text})"
            text = f"Connected over USB: {model} — {battery}"
        else:
            text = "No phone connected"
        self._status.setText(text)
