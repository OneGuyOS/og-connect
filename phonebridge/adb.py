"""Small, Qt-free wrapper around the adb executable."""

from __future__ import annotations

from dataclasses import dataclass
import os
import shlex
import shutil
import subprocess
from typing import Optional, Sequence


class AdbNotFoundError(FileNotFoundError):
    """Raised when the adb executable cannot be located."""


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    transport: str
    model: Optional[str] = None
    product: Optional[str] = None
    device: Optional[str] = None

    @property
    def is_usable(self) -> bool:
        return self.state == "device" and self.transport == "usb"


def get_adb_path() -> str:
    """Return the configured adb executable or raise a clear error."""
    adb_path = os.environ.get("ADB") or shutil.which("adb")
    if not adb_path:
        raise AdbNotFoundError(
            "ADB executable not found. Install 'adb', add it to PATH, or set $ADB."
        )
    if not os.path.isfile(adb_path) or not os.access(adb_path, os.X_OK):
        raise AdbNotFoundError(f"ADB path {adb_path!r} is not an executable file.")
    return adb_path


def list_devices() -> list[AdbDevice]:
    """Run `adb devices -l` and parse connected device details."""
    result = subprocess.run(
        [get_adb_path(), "devices", "-l"],
        check=True,
        capture_output=True,
        text=True,
    )
    devices: list[AdbDevice] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices attached") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue

        serial = parts[0]
        state = parts[1]
        qualifiers = parts[2:]
        if state == "no" and qualifiers[:1] == ["permissions"]:
            state = "no permissions"
            qualifiers = qualifiers[1:]

        attributes: dict[str, str] = {}
        for item in qualifiers:
            key, separator, value = item.partition(":")
            if separator:
                attributes[key] = value
        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                transport="tcp" if ":" in serial else "usb",
                model=attributes.get("model"),
                product=attributes.get("product"),
                device=attributes.get("device"),
            )
        )
    return devices


def _serial_args(serial: str) -> list[str]:
    if not serial:
        raise ValueError("An explicit ADB serial is required.")
    return ["-s", serial]


def shell(serial: str, command: str) -> subprocess.CompletedProcess[str]:
    """Run one command through the selected device's Android shell."""
    return subprocess.run(
        [get_adb_path(), *_serial_args(serial), "shell", command],
        check=True,
        capture_output=True,
        text=True,
    )


def exec_out_popen(serial: str, argv: Sequence[str]) -> subprocess.Popen[bytes]:
    """Start `adb exec-out` for the selected device and return its process."""
    if not argv:
        raise ValueError("At least one command argument is required.")
    return subprocess.Popen(
        [get_adb_path(), *_serial_args(serial), "exec-out", *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
