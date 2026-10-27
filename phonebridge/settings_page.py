"""Read-only host/device status and editable Phonebridge preferences."""

from __future__ import annotations

import platform
import shutil
import subprocess

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import adb
from .settings_store import AppSettings, CONFLICT_BEHAVIORS, SettingsStore
from .workers import FunctionTask


SCRCPY_MISSING = "scrcpy not installed — required for Phone Screen (future feature)"


def _read_host_status() -> tuple[str, str, str]:
    adb_path = "not found"
    adb_version = "unavailable"
    try:
        adb_path = adb.get_adb_path()
        result = subprocess.run(
            [adb_path, "version"], capture_output=True, text=True, timeout=10
        )
        adb_version = (result.stdout or result.stderr).strip().splitlines()[0]
        if result.returncode:
            adb_version = f"unavailable ({adb_version})"
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, IndexError):
        pass

    scrcpy_path = shutil.which("scrcpy")
    if not scrcpy_path:
        scrcpy_version = SCRCPY_MISSING
    else:
        try:
            result = subprocess.run(
                [scrcpy_path, "--version"], capture_output=True,
                text=True, timeout=10,
            )
            output = (result.stdout or result.stderr).strip()
            scrcpy_version = output.splitlines()[0] if output else "version unavailable"
            if result.returncode:
                scrcpy_version = f"version unavailable ({scrcpy_version})"
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, IndexError):
            scrcpy_version = "version unavailable"
    return adb_path, adb_version, scrcpy_version


class SettingsPage(QWidget):
    preferences_changed = Signal(object)

    def __init__(self, store: SettingsStore, home_dashboard, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.home_dashboard = home_dashboard
        self._host_task: FunctionTask | None = None
        self._adb_path = "not checked"
        self._adb_version = "not checked"
        self._scrcpy_version = "not checked"
        self._phone_model = "—"
        self._android_version = "—"
        self._serial = "—"

        self.adb_label = QLabel("Checking…")
        self.adb_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.scrcpy_label = QLabel("Checking…")
        self.phone_model_label = QLabel("—")
        self.android_label = QLabel("—")
        self.serial_label = QLabel("—")
        self.support_label = QLabel(
            "Samsung Galaxy S-series (tested on SM-S731U1, Android 17)"
        )

        status_form = QFormLayout()
        status_form.addRow("ADB executable and version:", self.adb_label)
        status_form.addRow("scrcpy:", self.scrcpy_label)
        status_form.addRow("Connected phone model:", self.phone_model_label)
        status_form.addRow("Android version:", self.android_label)
        status_form.addRow("ADB serial:", self.serial_label)
        status_form.addRow("Supported devices:", self.support_label)

        self.show_home_checkbox = QCheckBox("Show Home tab on startup")
        self.show_home_checkbox.setChecked(store.settings.show_home_on_startup)
        self.conflict_combo = QComboBox()
        self.conflict_combo.addItems(CONFLICT_BEHAVIORS)
        self.conflict_combo.setCurrentText(store.settings.default_conflict_behavior)
        self.battery_interval = QSpinBox()
        self.battery_interval.setRange(15, 300)
        self.battery_interval.setSuffix(" seconds")
        self.battery_interval.setValue(store.settings.battery_refresh_seconds)

        preferences_form = QFormLayout()
        preferences_form.addRow(self.show_home_checkbox)
        preferences_form.addRow("If destination exists:", self.conflict_combo)
        preferences_form.addRow("Battery refresh interval:", self.battery_interval)
        self.save_status_label = QLabel("Preferences save automatically.")

        self.refresh_button = QPushButton("Refresh status")
        self.copy_button = QPushButton("Copy diagnostics")
        buttons = QHBoxLayout()
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addLayout(status_form)
        layout.addLayout(buttons)
        layout.addWidget(QLabel("Preferences"))
        layout.addLayout(preferences_form)
        layout.addWidget(self.save_status_label)
        layout.addStretch(1)

        self.show_home_checkbox.toggled.connect(self._save_preferences)
        self.conflict_combo.currentTextChanged.connect(self._save_preferences)
        self.battery_interval.valueChanged.connect(self._save_preferences)
        self.refresh_button.clicked.connect(self.refresh_status)
        self.copy_button.clicked.connect(self.copy_diagnostics)
        self.home_dashboard.phone_info_changed.connect(self.set_phone_info)
        self.set_phone_info(
            self.home_dashboard.phone_model,
            self.home_dashboard.android_version,
            self.home_dashboard.adb_serial,
        )
        self.refresh_status(refresh_device=False)

    def _save_preferences(self, _value=None) -> None:
        settings = AppSettings(
            show_home_on_startup=self.show_home_checkbox.isChecked(),
            default_conflict_behavior=self.conflict_combo.currentText(),
            battery_refresh_seconds=self.battery_interval.value(),
        )
        saved = self.store.save(settings)
        self.save_status_label.setText(
            "Preferences saved."
            if saved else "Preferences could not be saved; current values apply for this session."
        )
        self.preferences_changed.emit(self.store.settings)

    def set_phone_info(self, model: str, android_version: str, serial: str) -> None:
        self._phone_model = model or "—"
        self._android_version = android_version or "—"
        self._serial = serial or "—"
        self.phone_model_label.setText(self._phone_model)
        self.android_label.setText(self._android_version)
        self.serial_label.setText(self._serial)

    def refresh_status(self, refresh_device: bool = True) -> None:
        if refresh_device:
            self.home_dashboard.refresh_device_info()
        if self._host_task is not None:
            return
        task = FunctionTask(_read_host_status)
        self._host_task = task
        task.signals.completed.connect(self._finish_host_status)
        QThreadPool.globalInstance().start(task)

    def _finish_host_status(self, result: object, error: object) -> None:
        self._host_task = None
        if error is not None:
            self._adb_path, self._adb_version, self._scrcpy_version = (
                "not found", "unavailable", SCRCPY_MISSING
            )
        else:
            self._adb_path, self._adb_version, self._scrcpy_version = result  # type: ignore[misc]
        self.adb_label.setText(f"{self._adb_path} — {self._adb_version}")
        if self._scrcpy_version == SCRCPY_MISSING:
            self.scrcpy_label.setText(SCRCPY_MISSING)
        else:
            self.scrcpy_label.setText(f"Installed — {self._scrcpy_version}")

    def copy_diagnostics(self) -> str:
        operating_system = f"{platform.system()} {platform.release()}".strip()
        diagnostics = "\n".join((
            f"OS: {operating_system}",
            f"ADB: {self._adb_path} — {self._adb_version}",
            f"scrcpy: {self._scrcpy_version}",
            f"Phone model: {self._phone_model}",
            f"Android version: {self._android_version}",
            f"ADB serial: {self._serial}",
        ))
        QApplication.clipboard().setText(diagnostics)
        return diagnostics
