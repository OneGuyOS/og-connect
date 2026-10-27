"""Main window with connection status and independent file browsers."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QSplitter, QVBoxLayout, QWidget

from .device import ConnectionState, DeviceManager, PhoneSession
from .panes import ComputerPane, PhonePane


class MainWindow(QMainWindow):
    def __init__(self, manager: DeviceManager) -> None:
        super().__init__()
        self.setWindowTitle("Phonebridge")
        self.resize(1100, 700)
        self._manager = manager

        self._status = QLabel()
        self._status.setMargin(8)
        self._status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.computer_pane = ComputerPane()
        self.phone_pane = PhonePane(manager)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.computer_pane)
        splitter.addWidget(self.phone_pane)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._status)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

        manager.state_changed.connect(self._update_status)
        self._update_status(manager.state, manager.session)

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
