"""Sequential transfer queue and Qt table model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import threading
from pathlib import Path
import time
from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QThreadPool, Signal

from .transfer import (
    DestinationExists,
    TransferCanceled,
    TransferDirection,
    TransferSpec,
    transfer_file,
)
from .workers import FunctionTask


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class TransferJob:
    job_id: int
    spec: TransferSpec
    state: JobState = JobState.PENDING
    bytes_done: int = 0
    total_bytes: int = 0
    message: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def source_name(self) -> str:
        return self.spec.target_name or self.spec.source.rstrip("/").rsplit("/", 1)[-1]


class TransferQueue(QObject):
    changed = Signal()
    _progress = Signal(int, int, int)
    conflict_needed = Signal(object, str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.jobs: list[TransferJob] = []
        self._next_id = 1
        self._active: Optional[TransferJob] = None
        self._worker: Optional[FunctionTask] = None
        self._progress.connect(self._update_progress)

    @property
    def current_file(self) -> Optional[str]:
        return self._active.source_name if self._active else None

    @property
    def completed_count(self) -> int:
        return sum(job.state == JobState.DONE for job in self.jobs)

    @property
    def remaining_count(self) -> int:
        return sum(job.state in (JobState.PENDING, JobState.RUNNING) for job in self.jobs)

    @property
    def overall_progress(self) -> tuple[int, int]:
        return (
            sum(job.bytes_done for job in self.jobs),
            sum(job.total_bytes for job in self.jobs),
        )

    @property
    def is_running(self) -> bool:
        return self._active is not None and self._active.state == JobState.RUNNING

    def add_jobs(self, specs: list[TransferSpec]) -> list[TransferJob]:
        added = []
        for spec in specs:
            job = TransferJob(self._next_id, spec)
            size_hint = spec.size_hint
            if size_hint is None and spec.direction == TransferDirection.COMPUTER_TO_PHONE:
                try:
                    size_hint = Path(spec.source).stat().st_size
                except OSError:
                    size_hint = None
            if size_hint is not None:
                job.total_bytes = size_hint
            self._next_id += 1
            self.jobs.append(job)
            added.append(job)
        if added:
            self.changed.emit()
            self._start_next()
        return added

    def resolve_conflict(self, job_id: int, choice: str, rename: Optional[str] = None) -> None:
        job = self._active
        if job is None or job.job_id != job_id or job.state != JobState.RUNNING:
            return
        if choice == "skip":
            job.state = JobState.CANCELED
            job.message = "Skipped at destination conflict."
            self._active = None
            self.changed.emit()
            self._start_next()
        elif choice == "overwrite":
            job.spec.overwrite = True
            self._run_active()
        elif choice == "rename" and rename:
            job.spec.target_name = rename
            job.spec.overwrite = False
            self._run_active()

    def cancel_current(self) -> None:
        job = self._active
        if job is None:
            return
        job.cancel_event.set()
        if self._worker is None:
            job.state = JobState.CANCELED
            job.message = "Canceled."
            self._active = None
            self.changed.emit()
            self._start_next()

    def cancel_all(self) -> None:
        active = self._active
        for job in self.jobs:
            if job.state == JobState.PENDING:
                job.state = JobState.CANCELED
                job.message = "Canceled with queue."
        if active is not None:
            active.cancel_event.set()
            if self._worker is None:
                active.state = JobState.CANCELED
                active.message = "Canceled with queue."
                self._active = None
        self.changed.emit()
        if self._active is None:
            self._start_next()

    def clear_finished(self) -> None:
        self.jobs = [
            job for job in self.jobs
            if job.state in (JobState.PENDING, JobState.RUNNING)
        ]
        self.changed.emit()

    def _start_next(self) -> None:
        if self._active is not None:
            return
        self._active = next((job for job in self.jobs if job.state == JobState.PENDING), None)
        if self._active is None:
            self.changed.emit()
            return
        self._active.state = JobState.RUNNING
        self._active.cancel_event = threading.Event()
        self.changed.emit()
        self._run_active()

    def _run_active(self) -> None:
        job = self._active
        if job is None:
            return
        job.bytes_done = 0
        job.message = ""
        last_report = [0.0]

        def report(done: int, total: int, job_id: int = job.job_id) -> None:
            now = time.monotonic()
            if done in (0, total) or now - last_report[0] >= 0.1:
                last_report[0] = now
                self._progress.emit(job_id, done, total)

        task = FunctionTask(
            transfer_file,
            job.spec,
            job.cancel_event,
            report,
        )
        self._worker = task
        task.signals.completed.connect(
            lambda result, error, job_id=job.job_id: self._finish_job(job_id, result, error)
        )
        QThreadPool.globalInstance().start(task)

    def _update_progress(self, job_id: int, done: int, total: int) -> None:
        job = next((item for item in self.jobs if item.job_id == job_id), None)
        if job is None:
            return
        job.bytes_done = done
        job.total_bytes = total
        self.changed.emit()

    def _finish_job(self, job_id: int, result: object, error: object) -> None:
        job = self._active
        self._worker = None
        if job is None or job.job_id != job_id:
            return
        if error is None:
            _, total = result
            job.bytes_done = total
            job.total_bytes = total
            job.state = JobState.DONE
            job.message = "Completed."
        elif job.cancel_event.is_set():
            job.state = JobState.CANCELED
            job.message = str(error)
        elif isinstance(error, DestinationExists):
            self.conflict_needed.emit(job, error.path)
            return
        elif isinstance(error, TransferCanceled):
            job.state = JobState.CANCELED
            job.message = str(error)
        else:
            job.state = JobState.FAILED
            job.message = str(error) or type(error).__name__
        self._active = None
        self.changed.emit()
        self._start_next()


class TransferQueueModel(QAbstractTableModel):
    HEADERS = ("File", "Direction", "State", "Progress", "Details")

    def __init__(self, queue: TransferQueue, parent=None) -> None:
        super().__init__(parent)
        self.queue = queue
        self.queue.changed.connect(self._reset)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.queue.jobs)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.queue.jobs):
            return None
        job = self.queue.jobs[index.row()]
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        values = (
            job.source_name,
            job.spec.direction.value,
            job.state.value,
            f"{job.bytes_done} / {job.total_bytes} bytes",
            job.message,
        )
        return values[index.column()]

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return None

    def _reset(self) -> None:
        self.beginResetModel()
        self.endResetModel()
