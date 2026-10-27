"""Small helpers for running blocking work outside the Qt GUI thread."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal


class _TaskSignals(QObject):
    completed = Signal(object, object)


class FunctionTask(QRunnable):
    """Run a callable in a QThreadPool and report its result or exception."""

    def __init__(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.signals = _TaskSignals()
        self._function = function
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            self.signals.completed.emit(self._function(*self._args, **self._kwargs), None)
        except Exception as error:
            self.signals.completed.emit(None, error)
