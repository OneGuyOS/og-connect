"""Controls the separately managed scrcpy window."""

from __future__ import annotations

import shutil

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QVBoxLayout, QWidget


class ScreenPage(QWidget):
    launch_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.scrcpy_path = shutil.which("scrcpy")
        self.phone_connected = False
        self.running = False
        self.view_only = False

        self.install_label = QLabel()
        self.install_label.setWordWrap(True)
        self.control_checkbox = QCheckBox("Enable input control (keyboard/mouse)")
        self.control_checkbox.setChecked(True)
        self.dex_checkbox = QCheckBox("Launch in DeX Mode (Virtual Display)")
        self.action_button = QPushButton("Launch Screen")
        self.action_button.setEnabled(False)
        self.status_label = QLabel("")

        layout = QVBoxLayout(self)
        layout.addWidget(self.install_label)
        layout.addWidget(self.control_checkbox)
        layout.addWidget(self.dex_checkbox)
        layout.addWidget(self.action_button)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.action_button.clicked.connect(self._action_clicked)
        self._refresh_controls()

    def _action_clicked(self) -> None:
        if self.running:
            self.close_requested.emit()
        else:
            self.launch_requested.emit()

    def set_connection(self, connected: bool) -> None:
        self.phone_connected = connected
        if not connected:
            self.status_label.setText("Phone disconnected")
        elif not self.running:
            self.status_label.setText(
                "Ready" if self.scrcpy_path else
                "scrcpy is not installed — install with: apt install scrcpy"
            )
        self._refresh_controls()

    def set_running(self, running: bool, view_only: bool = False) -> None:
        self.running = running
        self.view_only = view_only
        if running:
            mode = "View only" if view_only else "Input enabled"
            self.status_label.setText(f"Screen running — {mode}")
        self._refresh_controls()

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        if not self.scrcpy_path:
            self.install_label.setText(
                "scrcpy is not installed. Install it with: apt install scrcpy"
            )
        else:
            self.install_label.setText("Ready")
        self.action_button.setText("Close Screen" if self.running else "Launch Screen")
        self.action_button.setEnabled(
            self.phone_connected
            and (self.running or self.scrcpy_path is not None)
        )
