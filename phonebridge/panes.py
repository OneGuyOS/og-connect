"""Independent Computer and USB Phone browsing panes."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
from typing import Optional

from PySide6.QtCore import QDir, QModelIndex, Qt, QThreadPool
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileSystemModel,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTableView,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import adb
from .device import ConnectionState, DeviceManager, PhoneSession
from .files_model import PhoneFileModel, parse_ls_output
from .workers import FunctionTask


PHONE_ROOT = "/storage/emulated/0"


def _list_phone_directory(serial: str, path: str):
    command = f"ls -la {shlex.quote(path)}"
    return parse_ls_output(adb.shell(serial, command, timeout=10).stdout)


class ComputerPane(QWidget):
    """Browse the home directory with QFileSystemModel's live updates."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.home = str(Path.home().resolve())
        self.current_path = self.home
        self._model = self._new_model()

        title = QLabel("Computer")
        self.up_button = QPushButton("Go up")
        self.refresh_button = QPushButton("Refresh")
        self.path_label = QLabel(self.current_path)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.view = QTreeView()
        self.view.setModel(self._model)
        self.view.setRootIndex(self._model.index(self.current_path))
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.activated.connect(self._activate)

        bar = QHBoxLayout()
        bar.addWidget(title)
        bar.addWidget(self.up_button)
        bar.addWidget(self.refresh_button)
        bar.addWidget(self.path_label, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.view)

        self.up_button.clicked.connect(self.go_up)
        self.refresh_button.clicked.connect(self.refresh)
        self._update_up_button()

    def _new_model(self) -> QFileSystemModel:
        model = QFileSystemModel(self)
        model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        model.setRootPath(self.home)
        return model

    def _activate(self, index: QModelIndex) -> None:
        if self._model.isDir(index):
            self.navigate(self._model.filePath(index))

    def navigate(self, path: str) -> None:
        resolved = os.path.abspath(path)
        if os.path.commonpath((self.home, resolved)) != self.home or not os.path.isdir(resolved):
            return
        self.current_path = resolved
        root_index = self._model.setRootPath(resolved)
        self.view.setRootIndex(root_index)
        self.path_label.setText(resolved)
        self._update_up_button()

    def go_up(self) -> None:
        if self.current_path != self.home:
            self.navigate(str(Path(self.current_path).parent))

    def refresh(self) -> None:
        old_model = self._model
        self._model = self._new_model()
        self.view.setModel(self._model)
        self.view.setRootIndex(self._model.index(self.current_path))
        old_model.deleteLater()

    def _update_up_button(self) -> None:
        self.up_button.setEnabled(self.current_path != self.home)


class PhonePane(QWidget):
    """Browse `/storage/emulated/0` over the active USB ADB session."""

    def __init__(self, manager: DeviceManager, parent=None) -> None:
        super().__init__(parent)
        self._manager = manager
        self.current_path = PHONE_ROOT
        session = manager.session
        self._serial: Optional[str] = session.device.serial if session else None
        self._has_seen_session = session is not None
        self._generation = 0
        self._task: Optional[FunctionTask] = None
        self._refresh_pending = False
        self._sort_column = 0
        self._descending = False
        self._model = PhoneFileModel(self)

        title = QLabel("Phone")
        self.up_button = QPushButton("Go up")
        self.refresh_button = QPushButton("Refresh")
        self.path_label = QLabel(self.current_path)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.view = QTableView()
        self.view.setModel(self._model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setSortingEnabled(False)
        self.view.horizontalHeader().sectionClicked.connect(self._sort_by_column)
        self.view.activated.connect(self._activate)

        self.status = QLabel("Phone disconnected")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.content = QStackedWidget()
        self.content.addWidget(self.view)
        self.content.addWidget(self.status)

        bar = QHBoxLayout()
        bar.addWidget(title)
        bar.addWidget(self.up_button)
        bar.addWidget(self.refresh_button)
        bar.addWidget(self.path_label, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.content)

        self.up_button.clicked.connect(self.go_up)
        self.refresh_button.clicked.connect(self.refresh)
        manager.state_changed.connect(self._connection_changed)
        self._show_connected(session is not None)
        if session is not None:
            self.refresh()

    def _connection_changed(self, state: object, session: object) -> None:
        connected = state == ConnectionState.USB and isinstance(session, PhoneSession)
        new_serial = session.device.serial if connected else None
        if not connected:
            self._generation += 1
            self._refresh_pending = False
            self.current_path = PHONE_ROOT
            self._serial = None
            self.path_label.setText("Phone disconnected")
            self._model.set_entries([])
            self._show_connected(False)
            return

        changed_phone = new_serial != self._serial
        should_load = not self._has_seen_session
        self._serial = new_serial
        self._has_seen_session = True
        if changed_phone:
            self._generation += 1
            self.current_path = PHONE_ROOT
            self.path_label.setText(self.current_path)
            self._model.set_entries([])
        self._show_connected(True)
        # The first detected USB session gets an initial listing. Later reconnects
        # wait for the user's Refresh action.
        if should_load:
            self.refresh()

    def _show_connected(self, connected: bool) -> None:
        self.content.setCurrentWidget(self.view if connected else self.status)
        self.up_button.setEnabled(connected and self.current_path != PHONE_ROOT)
        self.refresh_button.setEnabled(connected)

    def refresh(self) -> None:
        session = self._manager.session
        if session is None or self._serial != session.device.serial:
            return
        if self._task is not None:
            self._refresh_pending = True
            return
        self._refresh_pending = False
        self.path_label.setText(self.current_path)
        generation = self._generation
        serial = session.device.serial
        path = self.current_path
        task = FunctionTask(_list_phone_directory, serial, path)
        self._task = task
        task.signals.completed.connect(
            lambda result, error, task=task, generation=generation, serial=serial, path=path:
                self._listing_finished(task, generation, serial, path, result, error)
        )
        QThreadPool.globalInstance().start(task)

    def _listing_finished(
        self,
        task: FunctionTask,
        generation: int,
        serial: str,
        path: str,
        result: object,
        error: object,
    ) -> None:
        if self._task is task:
            self._task = None
        session = self._manager.session
        if (
            self._refresh_pending
            and session is not None
            and serial == session.device.serial
        ):
            self._refresh_pending = False
            self.refresh()
            return
        if (
            generation != self._generation
            or session is None
            or serial != session.device.serial
        ):
            if (
                generation != self._generation
                and session is not None
                and serial == session.device.serial
                and self.current_path != path
            ):
                self.refresh()
            return
        if error is not None:
            self.status.setText(f"Could not read phone storage: {error}")
            self.content.setCurrentWidget(self.status)
            return
        self._model.set_entries(result)  # type: ignore[arg-type]
        self.content.setCurrentWidget(self.view)

    def _activate(self, index: QModelIndex) -> None:
        entry = self._model.entry_at(index.row())
        if entry is not None and entry.is_dir:
            self.navigate(entry.name)

    def navigate(self, name: str) -> None:
        if self._manager.session is None or self._serial is None:
            return
        self.current_path = str(Path(self.current_path, name))
        self.path_label.setText(self.current_path)
        self.up_button.setEnabled(self.current_path != PHONE_ROOT)
        self._generation += 1
        self._model.set_entries([])
        self.refresh()

    def go_up(self) -> None:
        if self.current_path != PHONE_ROOT:
            self.current_path = str(Path(self.current_path).parent)
            self.path_label.setText(self.current_path)
            self.up_button.setEnabled(self.current_path != PHONE_ROOT)
            self._generation += 1
            self._model.set_entries([])
            self.refresh()

    def _sort_by_column(self, column: int) -> None:
        if column == self._sort_column:
            self._descending = not self._descending
        else:
            self._sort_column = column
            self._descending = False
        self._model.sort_by(column, self._descending)
        self.view.horizontalHeader().setSortIndicator(
            column,
            Qt.SortOrder.DescendingOrder if self._descending else Qt.SortOrder.AscendingOrder,
        )
