"""Main window with connection status and independent file browsers."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .device import ConnectionState, DeviceManager, PhoneSession
from .panes import ComputerPane, PhonePane
from .transfer import TransferDirection, TransferSpec
from .transfer_queue import TransferQueue
from .transfer_ui import TransferQueuePanel


class MainWindow(QMainWindow):
    def __init__(self, manager: DeviceManager) -> None:
        super().__init__()
        self.setWindowTitle("Phonebridge")
        self.resize(1100, 700)
        self._manager = manager

        self._status = QLabel()
        self._status.setMargin(8)
        self._status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._transport_status = QLabel("Active transport: —")
        self.computer_pane = ComputerPane(manager)
        self.phone_pane = PhonePane(manager)

        self.transfer_queue = TransferQueue(self)
        self.transfer_panel = TransferQueuePanel(self.transfer_queue)
        self._conflict_prompts = {}

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
        self.transfer_queue.conflict_needed.connect(self._ask_destination_conflict)
        self.transfer_queue.changed.connect(self._update_transport)
        manager.state_changed.connect(self._update_status)
        self._update_status(manager.state, manager.session)
        self._update_transport()

    def _update_transport(self) -> None:
        text = "USB (ADB)" if self.transfer_queue.is_running else "—"
        self._transport_status.setText(f"Active transport: {text}")

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
