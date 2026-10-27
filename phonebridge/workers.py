"""Small helpers for running blocking work outside the Qt GUI thread."""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal


_ACTIVE_LOCK = threading.Lock()
_ACTIVE_TASKS: dict[int, "FunctionTask"] = {}


class _TaskSignals(QObject):
    completed = Signal(object, object)


class FunctionTask(QRunnable):
    """Run a callable in a QThreadPool and report its result or exception."""

    def __init__(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.signals = _TaskSignals()
        self._detached = threading.Event()
        self._function = function
        self._args = args
        self._kwargs = kwargs
        # Register before submission so shutdown also catches tasks that have
        # been queued but have not entered run() yet.
        with _ACTIVE_LOCK:
            _ACTIVE_TASKS[id(self)] = self

    def detach(self) -> None:
        """Prevent a background result from reaching a closing UI."""
        self._detached.set()
        try:
            self.signals.completed.disconnect()
        except (RuntimeError, TypeError):
            pass

    def run(self) -> None:
        key = id(self)
        signals = self.signals
        try:
            try:
                result = self._function(*self._args, **self._kwargs)
                error = None
            except Exception as caught:
                result = None
                error = caught

            if not self._detached.is_set():
                try:
                    signals.completed.emit(result, error)
                except RuntimeError:
                    # Qt may already be tearing down the application. A result
                    # that no longer has a live receiver is intentionally dropped.
                    pass
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_TASKS.pop(key, None)


def detach_all_tasks() -> None:
    """Detach queued and running jobs from UI objects during application close."""
    with _ACTIVE_LOCK:
        tasks = tuple(_ACTIVE_TASKS.values())
    for task in tasks:
        task.detach()
