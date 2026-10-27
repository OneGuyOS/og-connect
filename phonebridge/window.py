"""Main window with connection status and independent file browsers."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QThreadPool, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import adb
from .device import ConnectionState, DeviceManager, PhoneSession
from .panes import ComputerPane, PhonePane
from .transfer import TransferDirection, TransferSpec
from .transfer_queue import TransferQueue
from .transfer_ui import TransferQueuePanel
from .viewer import ImageViewer
from .settings_page import SettingsPage
from .settings_store import AppSettings, SettingsStore
from .workers import FunctionTask, detach_all_tasks


def _read_home_snapshot(
    serial: str, include_android_version: bool
) -> tuple[str | None, int | None, str]:
    """Read dashboard data in a worker thread; this function never writes to the phone."""
    android_version = None
    if include_android_version:
        try:
            android_version = adb.shell(
                serial, "getprop ro.build.version.release", timeout=10
            ).stdout.strip() or None
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            pass

    try:
        output = adb.shell(serial, "dumpsys battery", timeout=10).stdout
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return android_version, None, "Unknown"

    level_match = re.search(r"(?m)^\s*level:\s*(\d+)\s*$", output)
    status_match = re.search(r"(?m)^\s*status:\s*(\d+)\s*$", output)
    plugged_match = re.search(r"(?m)^\s*plugged:\s*(\d+)\s*$", output)
    level = int(level_match.group(1)) if level_match else None
    status = int(status_match.group(1)) if status_match else None
    plugged = int(plugged_match.group(1)) if plugged_match else None

    if status == 2:
        charge_state = "Charging"
        source = {1: "AC", 2: "USB", 4: "Wireless"}.get(plugged)
        if source is None:
            power_sources = (
                (r"(?m)^\s*AC powered:\s*true\s*$", "AC"),
                (r"(?m)^\s*USB powered:\s*true\s*$", "USB"),
                (r"(?m)^\s*Wireless powered:\s*true\s*$", "Wireless"),
            )
            source = next(
                (
                    name
                    for pattern, name in power_sources
                    if re.search(pattern, output, re.IGNORECASE)
                ),
                None,
            )
        if source:
            charge_state = f"{charge_state} ({source})"
    elif status in (3, 4, 5):
        charge_state = "Not charging"
    else:
        charge_state = "Unknown"
    return android_version, level, charge_state


class HomeDashboard(QWidget):
    """Connection and battery summary; ADB reads run outside the GUI thread."""

    phone_info_changed = Signal(str, str, str)

    def __init__(
        self, manager: DeviceManager, transfer_queue: TransferQueue, parent=None,
        refresh_interval_seconds: int = 30,
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._transfer_queue = transfer_queue
        self._serial: str | None = None
        self._request_task: FunctionTask | None = None
        self._request_generation = 0
        self._request_serial: str | None = None
        self._request_expected_generation = 0
        self._last_request_at = 0.0
        self._include_android_version = False
        self.refresh_interval_seconds = min(300, max(15, int(refresh_interval_seconds)))
        self.phone_model = ""
        self.android_version = ""
        self.adb_serial = ""

        self.connection_label = QLabel("No phone connected")
        self.model_label = QLabel("Model: —")
        self.android_label = QLabel("Android version: —")
        self.serial_label = QLabel("ADB serial: —")
        self.battery_label = QLabel("Battery: —")
        self.battery_bar = QProgressBar()
        self.battery_bar.setRange(0, 100)
        self.battery_bar.setTextVisible(False)
        self.battery_bar.setFixedHeight(10)
        self.battery_bar.setMaximumWidth(320)
        self.charging_label = QLabel("Charging state: Unknown")
        self.transport_label = QLabel("Transport: USB (ADB)")
        self.support_label = QLabel(
            "Support: Samsung Galaxy S-series (tested on SM-S731U1)"
        )
        for label in (self.model_label, self.android_label, self.serial_label):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(self.connection_label)
        layout.addWidget(self.model_label)
        layout.addWidget(self.android_label)
        layout.addWidget(self.serial_label)
        layout.addSpacing(8)
        layout.addWidget(self.battery_label)
        layout.addWidget(self.battery_bar)
        layout.addWidget(self.charging_label)
        layout.addSpacing(8)
        layout.addWidget(self.transport_label)
        layout.addWidget(self.support_label)
        layout.addStretch(1)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(self.refresh_interval_seconds * 1000)
        self._refresh_timer.timeout.connect(self._refresh_if_due)
        self._transfer_queue.changed.connect(self._refresh_if_due)
        self.set_connection(manager.state, manager.session)

    def set_connection(self, state: object, session: object) -> None:
        if state == ConnectionState.USB and isinstance(session, PhoneSession):
            serial = session.device.serial
            if serial != self._serial:
                self._request_generation += 1
                self._serial = serial
                self.android_version = ""
                self.android_label.setText("Android version: —")
                self._clear_battery()
                self._include_android_version = True
            self.connection_label.setText("Connected over USB")
            self.phone_model = session.device.model or "Unknown"
            self.adb_serial = serial
            self.model_label.setText(f"Model: {self.phone_model}")
            self.serial_label.setText(f"ADB serial: {serial}")
            self._emit_phone_info()
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()
            self._refresh_if_due(force=self._include_android_version)
            return

        self._request_generation += 1
        self._serial = None
        self._refresh_timer.stop()
        self._include_android_version = False
        self.phone_model = ""
        self.android_version = ""
        self.adb_serial = ""
        if state == ConnectionState.UNAUTHORIZED:
            self.connection_label.setText("Phone unauthorized")
        else:
            self.connection_label.setText("No phone connected")
        self.model_label.setText("Model: —")
        self.android_label.setText("Android version: —")
        self.serial_label.setText("ADB serial: —")
        self._clear_battery()
        self._emit_phone_info()

    def _emit_phone_info(self) -> None:
        self.phone_info_changed.emit(
            self.phone_model, self.android_version, self.adb_serial
        )

    def set_refresh_interval(self, seconds: int) -> None:
        self.refresh_interval_seconds = min(300, max(15, int(seconds)))
        self._refresh_timer.setInterval(self.refresh_interval_seconds * 1000)

    def refresh_device_info(self) -> None:
        if self._serial is None:
            return
        self._include_android_version = True
        self._refresh_if_due(force=True)

    def _clear_battery(self) -> None:
        self.battery_label.setText("Battery: —")
        self.battery_bar.setValue(0)
        self.battery_bar.hide()
        self.charging_label.setText("Charging state: Unknown")

    def _refresh_if_due(self, force: bool = False) -> None:
        if self._serial is None or self._request_task is not None:
            return
        if self._transfer_queue.is_running:
            return
        now = time.monotonic()
        if (
            not force
            and not self._include_android_version
            and now - self._last_request_at < self.refresh_interval_seconds
        ):
            return
        serial = self._serial
        generation = self._request_generation
        include_version = self._include_android_version
        self._include_android_version = False
        self._last_request_at = now
        task = FunctionTask(_read_home_snapshot, serial, include_version)
        self._request_task = task
        self._request_serial = serial
        self._request_expected_generation = generation
        task.signals.completed.connect(self._finish_refresh_result)
        QThreadPool.globalInstance().start(task)

    def _finish_refresh_result(self, result: object, error: object) -> None:
        self._request_task = None
        serial = self._request_serial
        generation = self._request_expected_generation
        self._request_serial = None
        if generation != self._request_generation or serial != self._serial:
            if self._serial is not None:
                self._refresh_if_due(force=self._include_android_version)
            return
        if error is not None:
            android_version, level, charge_state = None, None, "Unknown"
        else:
            android_version, level, charge_state = result  # type: ignore[misc]
        if android_version:
            self.android_version = android_version
            self.android_label.setText(f"Android version: {android_version}")
        self._emit_phone_info()
        if level is None or not 0 <= level <= 100:
            self._clear_battery()
            if self._include_android_version:
                self._refresh_if_due(force=True)
            return
        self.battery_label.setText(f"Battery: {level}%")
        self.battery_bar.setValue(level)
        self.battery_bar.show()
        self.charging_label.setText(f"Charging state: {charge_state}")
        if self._include_android_version:
            self._refresh_if_due(force=True)


class MainWindow(QMainWindow):
    def __init__(self, manager: DeviceManager, settings_store: SettingsStore | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Phonebridge")
        self.resize(1100, 700)
        self._manager = manager
        self.settings_store = settings_store or SettingsStore()

        self._status = QLabel()
        self._status.setMargin(8)
        self._status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._transport_status = QLabel("Active transport: —")
        self.computer_pane = ComputerPane(manager)
        self.phone_pane = PhonePane(manager)

        self.transfer_queue = TransferQueue(self)
        self.transfer_panel = TransferQueuePanel(self.transfer_queue)
        self.home_dashboard = HomeDashboard(
            manager, self.transfer_queue,
            refresh_interval_seconds=self.settings_store.settings.battery_refresh_seconds,
        )
        self.settings_page = SettingsPage(self.settings_store, self.home_dashboard)
        self._conflict_prompts = {}
        self._rename_attempts: dict[int, int] = {}
        self._rename_original_names: dict[int, str] = {}
        self._viewer: ImageViewer | None = None

        files_page = QWidget()
        files_layout = QVBoxLayout(files_page)
        files_layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.computer_pane)
        splitter.addWidget(self.phone_pane)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        files_layout.addWidget(splitter)

        self.pages = QTabWidget()
        self.pages.addTab(self.home_dashboard, "Home")
        self.pages.addTab(self.settings_page, "Settings")
        self.pages.addTab(files_page, "Files")
        self.pages.addTab(self.transfer_panel, "Transfers")

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        header = QHBoxLayout()
        header.addWidget(self._status, 1)
        header.addWidget(self._transport_status)
        layout.addLayout(header)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(central)

        self.computer_pane.copy_to_phone_requested.connect(self._copy_to_phone)
        self.phone_pane.copy_to_computer_requested.connect(self._copy_to_computer)
        self.computer_pane.open_image_requested.connect(
            lambda path, paths: self._open_image(path, paths, False)
        )
        self.phone_pane.open_image_requested.connect(
            lambda path, paths: self._open_image(path, paths, True)
        )
        self.transfer_queue.conflict_needed.connect(self._ask_destination_conflict)
        self.transfer_queue.changed.connect(self._update_transport)
        manager.state_changed.connect(self._update_status)
        manager.state_changed.connect(self.home_dashboard.set_connection)
        self.settings_page.preferences_changed.connect(self._apply_preferences)
        if not self.settings_store.settings.show_home_on_startup:
            self.pages.setCurrentWidget(files_page)
        self._update_status(manager.state, manager.session)
        self._update_transport()

    def _apply_preferences(self, settings: object) -> None:
        if not isinstance(settings, AppSettings):
            return
        self.home_dashboard.set_refresh_interval(settings.battery_refresh_seconds)

    def _open_image(self, path: str, paths: list[str], is_phone: bool) -> None:
        session = self._manager.session if is_phone else None
        serial = session.device.serial if session is not None else None
        if self._viewer is None:
            self._viewer = ImageViewer(self._manager, self)
        self._viewer.open_image_set(paths, path, is_phone, serial)
        self._viewer.show()
        self._viewer.raise_()
        self._viewer.activateWindow()

    def _update_transport(self) -> None:
        text = "USB (ADB)" if self.transfer_queue.is_running else "—"
        self._transport_status.setText(f"Active transport: {text}")
        active_ids = {
            job.job_id for job in self.transfer_queue.jobs
            if job.state.value in ("pending", "running")
        }
        for job_id in tuple(self._rename_attempts):
            if job_id not in active_ids:
                self._rename_attempts.pop(job_id, None)
                self._rename_original_names.pop(job_id, None)

    def _copy_to_phone(self, sources: list[str]) -> None:
        session = self._manager.session
        if session is None:
            return
        specs = []
        for source in sources:
            try:
                size_hint = Path(source).stat().st_size
            except OSError:
                size_hint = None
            specs.append(TransferSpec(
                source=source,
                destination_dir=self.phone_pane.current_path,
                direction=TransferDirection.COMPUTER_TO_PHONE,
                serial=session.device.serial,
                size_hint=size_hint,
            ))
        self.transfer_queue.add_jobs(specs)

    def _copy_to_computer(self, sources: list[str]) -> None:
        session = self._manager.session
        if session is None:
            return
        specs = [
            TransferSpec(
                source=source,
                destination_dir=self.computer_pane.current_path,
                direction=TransferDirection.PHONE_TO_COMPUTER,
                serial=session.device.serial,
                size_hint=size_hint,
            )
            for source, size_hint in sources
        ]
        self.transfer_queue.add_jobs(specs)

    def _ask_destination_conflict(self, job: object, target: str) -> None:
        job_id = job.job_id
        if job_id in self._conflict_prompts:
            return
        behavior = self.settings_store.settings.default_conflict_behavior
        if behavior == "Skip":
            self.transfer_queue.resolve_conflict(job_id, "skip")
            return
        if behavior == "Overwrite":
            self.transfer_queue.resolve_conflict(job_id, "overwrite")
            return
        if behavior == "Rename":
            attempt = self._rename_attempts.get(job_id, 0) + 1
            self._rename_attempts[job_id] = attempt
            original = self._rename_original_names.get(job_id)
            if original is None:
                original = job.spec.target_name or (
                    PurePosixPath(job.spec.source).name
                    if job.spec.direction == TransferDirection.PHONE_TO_COMPUTER
                    else Path(job.spec.source).name
                )
                self._rename_original_names[job_id] = original
            name_path = PurePosixPath(original)
            suffix = "".join(name_path.suffixes)
            stem = original[:-len(suffix)] if suffix else original
            self.transfer_queue.resolve_conflict(
                job_id, "rename", f"{stem} ({attempt}){suffix}"
            )
            return
        box = QMessageBox(self)
        box.setWindowTitle("Destination already exists")
        box.setText(f"A file already exists at:\n{target}")
        box.setInformativeText("Choose how this queued file should be handled.")
        skip = box.addButton("Skip", QMessageBox.ButtonRole.RejectRole)
        overwrite = box.addButton("Overwrite", QMessageBox.ButtonRole.DestructiveRole)
        rename = box.addButton("Rename", QMessageBox.ButtonRole.ActionRole)
        state = {"choice": None}
        self._conflict_prompts[job_id] = box

        def choose(button) -> None:
            if button == skip:
                state["choice"] = "skip"
            elif button == overwrite:
                state["choice"] = "overwrite"
            elif button == rename:
                state["choice"] = "rename"

        def closed(_result: int) -> None:
            self._conflict_prompts.pop(job_id, None)
            choice = state["choice"]
            if choice == "rename":
                self._ask_rename(job, target)
            elif choice == "overwrite":
                self.transfer_queue.resolve_conflict(job_id, "overwrite")
            else:
                self.transfer_queue.resolve_conflict(job_id, "skip")

        box.buttonClicked.connect(choose)
        box.finished.connect(closed)
        box.open()

    def _ask_rename(self, job: object, target: str) -> None:
        job_id = job.job_id
        initial_name = (
            PurePosixPath(target).name
            if job.spec.direction == TransferDirection.COMPUTER_TO_PHONE
            else Path(target).name
        )
        dialog = QInputDialog(self)
        dialog.setWindowTitle("Rename destination")
        dialog.setLabelText("New file name:")
        dialog.setTextValue(initial_name)
        self._conflict_prompts[job_id] = dialog

        def accepted() -> None:
            name = dialog.textValue().strip()
            if name:
                self.transfer_queue.resolve_conflict(job_id, "rename", name)
            else:
                self.transfer_queue.resolve_conflict(job_id, "skip")
            self._conflict_prompts.pop(job_id, None)

        def rejected() -> None:
            self.transfer_queue.resolve_conflict(job_id, "skip")
            self._conflict_prompts.pop(job_id, None)

        dialog.accepted.connect(accepted)
        dialog.rejected.connect(rejected)
        dialog.open()

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

    def closeEvent(self, event) -> None:
        timer = getattr(self._manager, "_timer", None)
        if timer is not None:
            timer.stop()
        self.transfer_queue.cancel_all()
        if self._viewer is not None:
            self._viewer.close()
        detach_all_tasks()
        super().closeEvent(event)
