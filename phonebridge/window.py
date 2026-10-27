"""window.py - Minimal main window showing phone connection state."""

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QVBoxLayout, QWidget

from phonebridge.device import ConnectionState, DeviceManager, PhoneSession


def format_status(state: ConnectionState | str, session: Optional[PhoneSession]) -> str:
    """Format status string according to requirements."""
    if state == ConnectionState.UNAUTHORIZED or state == "UNAUTHORIZED":
        return "Phone unauthorized — check the prompt on your phone"

    if (state == ConnectionState.USB or state == "USB") and session is not None:
        model = session.device.model or session.device.serial or "Unknown"
        if session.battery_level is None:
            return f"Connected over USB: {model} — battery unknown"

        charging_text = "charging" if session.charging else "not charging"
        return f"Connected over USB: {model} — battery {session.battery_level}% ({charging_text})"

    return "No phone connected"


class MainWindow(QMainWindow):
    """Small main window displaying live connection status."""

    def __init__(self, device_manager: DeviceManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._device_manager = device_manager

        self.setWindowTitle("phonebridge")
        self.resize(480, 120)

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 20, 20, 20)

        self._status_label = QLabel(self)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        # Initialize label with current manager state
        self._on_state_changed(self._device_manager.state, self._device_manager.session)

        # Connect live updates from DeviceManager
        self._device_manager.state_changed.connect(self._on_state_changed)

    def _on_state_changed(
        self,
        state: ConnectionState | str,
        session: Optional[PhoneSession],
    ) -> None:
        text = format_status(state, session)
        self._status_label.setText(text)
