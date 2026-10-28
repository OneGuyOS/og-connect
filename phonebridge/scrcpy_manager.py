"""Own the external scrcpy process used for USB screen mirroring."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Optional

from PySide6.QtCore import QObject, Signal


class ScrcpyManager(QObject):
    """Launch and reap one scrcpy child without embedding its video window."""

    process_exited = Signal(int)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._process: subprocess.Popen[bytes] | None = None
        self._monitor: threading.Thread | None = None
        self._lock = threading.RLock()
        self._stop_lock = threading.Lock()

    @staticmethod
    def _command(
        serial: str,
        view_only: bool = False,
        window_position: tuple[int, int] | None = None,
        dex_mode: bool = False,
    ) -> list[str]:
        if not serial:
            raise ValueError("A connected USB device serial is required to start scrcpy.")
        command = ["scrcpy", "-s", serial]
        command.append("--audio-source=playback")
        if dex_mode:
            command.append("--new-display=1920x1080/284")
        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or session_type == "wayland"
        x11 = not wayland and (
            session_type == "x11" or not os.environ.get("WAYLAND_DISPLAY")
        )
        if x11 and window_position is not None:
            x, y = window_position
            command.extend(["--window-x", str(int(x)), "--window-y", str(int(y))])
        if view_only:
            command.append("--no-control")
        return command

    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        with self._lock:
            return self._process

    def start(
        self,
        serial: str,
        view_only: bool = False,
        window_position: tuple[int, int] | None = None,
        dex_mode: bool = False,
    ) -> subprocess.Popen[bytes]:
        """Start scrcpy once, returning the existing child if it is already alive."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self._process
            old_monitor = self._monitor
            self._process = None
            self._monitor = None
        if old_monitor is not None and old_monitor is not threading.current_thread():
            old_monitor.join(timeout=0.5)

        if shutil.which("scrcpy") is None:
            raise FileNotFoundError(
                "scrcpy was not found on PATH. Install it with: apt install scrcpy"
            )
        command = self._command(serial, view_only, window_position, dex_mode)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as error:
            raise FileNotFoundError(
                "scrcpy was not found on PATH. Install it with: apt install scrcpy"
            ) from error

        monitor = threading.Thread(
            target=self._monitor_process,
            args=(process,),
            name=f"scrcpy-monitor-{process.pid}",
            daemon=True,
        )
        with self._lock:
            self._process = process
            self._monitor = monitor
        monitor.start()
        return process

    def _monitor_process(self, process: subprocess.Popen[bytes]) -> None:
        return_code = process.wait()
        with self._lock:
            if self._process is process:
                self._process = None
                self._monitor = None
        self.process_exited.emit(return_code)

    def stop(self) -> None:
        """Terminate only the stored child, escalating to SIGKILL after two seconds."""
        with self._stop_lock:
            with self._lock:
                process = self._process
                monitor = self._monitor
            if process is None:
                return
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait()
            if monitor is not None and monitor is not threading.current_thread():
                monitor.join()
            with self._lock:
                if self._process is process:
                    self._process = None
                    self._monitor = None
