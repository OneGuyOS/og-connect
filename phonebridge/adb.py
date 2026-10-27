"""adb.py - Thin wrapper around the adb executable.

Does not import Qt. Pure standard library subprocess wrapper for ADB commands.
"""

from dataclasses import dataclass
import os
import shutil
import subprocess
from typing import Optional, Sequence


class AdbNotFoundError(FileNotFoundError):
    """Raised when the adb binary cannot be located."""


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
        """A device is usable only if state == 'device' and transport == 'usb'."""
        return self.state == "device" and self.transport == "usb"


def get_adb_path() -> str:
    """Locate adb via $ADB env var, else shutil.which('adb').

    Raises AdbNotFoundError if missing or not executable.
    """
    adb_path = os.environ.get("ADB")
    if not adb_path:
        adb_path = shutil.which("adb")

    if not adb_path:
        raise AdbNotFoundError(
            "ADB executable not found. Ensure 'adb' is installed and in PATH, "
            "or set the $ADB environment variable."
        )

    if not os.path.isfile(adb_path) or not os.access(adb_path, os.X_OK):
        raise AdbNotFoundError(f"ADB path '{adb_path}' is not an executable file.")

    return adb_path


def list_devices() -> list[AdbDevice]:
    """Runs `adb devices -l` and parses serial, state, transport, model, product, device.

    A serial containing ':' means TCP/IP ('tcp'). Everything else is 'usb'.
    """
    adb = get_adb_path()
    res = subprocess.run([adb, "devices", "-l"], capture_output=True, text=True, check=True)

    devices: list[AdbDevice] = []
    for raw_line in res.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices attached") or line.startswith("*"):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        serial = parts[0]
        transport = "tcp" if ":" in serial else "usb"

        # Handle two-word states such as 'no permissions'
        if len(parts) >= 3 and parts[1] == "no" and parts[2] == "permissions":
            state = "no permissions"
            qualifiers = parts[3:]
        else:
            state = parts[1]
            qualifiers = parts[2:]

        model: Optional[str] = None
        product: Optional[str] = None
        device: Optional[str] = None

        for item in qualifiers:
            if item.startswith("model:"):
                model = item.split(":", 1)[1]
            elif item.startswith("product:"):
                product = item.split(":", 1)[1]
            elif item.startswith("device:"):
                device = item.split(":", 1)[1]

        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                transport=transport,
                model=model,
                product=product,
                device=device,
            )
        )

    return devices


def shell(
    serial: str,
    command: str | Sequence[str],
    timeout: Optional[float] = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Execute `adb -s <serial> shell <command>`.

    Every call must take an explicit serial and pass `-s <serial>`.
    """
    if not serial:
        raise ValueError("An explicit device serial must be provided.")

    adb = get_adb_path()
    if isinstance(command, str):
        cmd = [adb, "-s", serial, "shell", command]
    else:
        cmd = [adb, "-s", serial, "shell", *command]

    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def exec_out_popen(
    serial: str,
    argv: Sequence[str] | str,
    **kwargs,
) -> subprocess.Popen:
    """Execute `adb -s <serial> exec-out <argv...>` via subprocess.Popen.

    Every call must take an explicit serial and pass `-s <serial>`.
    """
    if not serial:
        raise ValueError("An explicit device serial must be provided.")

    adb = get_adb_path()
    if isinstance(argv, str):
        cmd_args = [argv]
    else:
        cmd_args = list(argv)

    cmd = [adb, "-s", serial, "exec-out", *cmd_args]
    if "stdout" not in kwargs:
        kwargs["stdout"] = subprocess.PIPE

    return subprocess.Popen(cmd, **kwargs)
