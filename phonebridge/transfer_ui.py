"""Visible transfer queue page."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .transfer_queue import TransferQueue, TransferQueueModel


class TransferQueuePanel(QWidget):
    def __init__(self, queue: TransferQueue, parent=None) -> None:
        super().__init__(parent)
        self.queue = queue
        self.model = TransferQueueModel(queue, self)

        self.current_label = QLabel("Current file: —")
        self.transport_label = QLabel("Active transport: —")
        self.summary_label = QLabel("Completed: 0    Remaining: 0")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)

        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.horizontalHeader().setStretchLastSection(True)

        self.cancel_current_button = QPushButton("Cancel current")
        self.cancel_all_button = QPushButton("Cancel all")
        self.clear_button = QPushButton("Clear finished")
        buttons = QHBoxLayout()
        buttons.addWidget(self.cancel_current_button)
        buttons.addWidget(self.cancel_all_button)
        buttons.addStretch(1)
        buttons.addWidget(self.clear_button)

        summary = QHBoxLayout()
        summary.addWidget(self.current_label, 1)
        summary.addWidget(self.transport_label)
        summary.addWidget(self.summary_label)

        layout = QVBoxLayout(self)
        layout.addLayout(summary)
        layout.addWidget(self.progress)
        layout.addWidget(self.view)
        layout.addLayout(buttons)

        self.cancel_current_button.clicked.connect(queue.cancel_current)
        self.cancel_all_button.clicked.connect(queue.cancel_all)
        self.clear_button.clicked.connect(queue.clear_finished)
        queue.changed.connect(self._update_summary)
        self._update_summary()

    def _update_summary(self) -> None:
        current = self.queue.current_file
        self.current_label.setText(f"Current file: {current or '—'}")
        self.transport_label.setText(
            "Active transport: USB (ADB)" if self.queue.is_running else "Active transport: —"
        )
        self.summary_label.setText(
            f"Completed: {self.queue.completed_count}    Remaining: {self.queue.remaining_count}"
        )
        done, total = self.queue.overall_progress
        self.progress.setValue(0 if total == 0 else min(1000, int(done * 1000 / total)))
        self.progress.setFormat(f"{done} / {total} bytes")
        self.cancel_current_button.setEnabled(self.queue.current_file is not None)
        self.cancel_all_button.setEnabled(self.queue.remaining_count > 0)
        self.clear_button.setEnabled(
            any(job.state.value in ("done", "failed", "canceled") for job in self.queue.jobs)
        )
