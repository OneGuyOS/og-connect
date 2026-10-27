"""Streaming, resumable transfers over an explicit USB ADB session."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import posixpath
import select
import shlex
import shutil
import subprocess
import threading
import ctypes
import errno
from typing import Callable, Optional

from . import adb


CHUNK_SIZE = 256 * 1024


class TransferDirection(str, Enum):
    COMPUTER_TO_PHONE = "Computer → Phone"
    PHONE_TO_COMPUTER = "Phone → Computer"


class TransferError(RuntimeError):
    pass


class TransferCanceled(TransferError):
    pass


class DestinationExists(TransferError):
    def __init__(self, path: str) -> None:
        super().__init__(f"Destination already exists: {path}")
        self.path = path


@dataclass
class TransferSpec:
    source: str
    destination_dir: str
    direction: TransferDirection
    serial: str
    target_name: Optional[str] = None
    overwrite: bool = False
    size_hint: Optional[int] = None


ProgressCallback = Callable[[int, int], None]


def transfer_file(
    spec: TransferSpec,
    cancel: threading.Event,
    progress: ProgressCallback,
) -> tuple[str, int]:
    """Transfer one regular file, returning its final path and byte count."""
    _check_cancel(cancel)
    name = spec.target_name or posixpath.basename(spec.source.rstrip("/"))
    if not name or name in (".", "..") or "/" in name:
        raise TransferError("The destination file name is invalid.")
    if spec.direction == TransferDirection.PHONE_TO_COMPUTER:
        return _phone_to_computer(spec, name, cancel, progress)
    return _computer_to_phone(spec, name, cancel, progress)


def _phone_to_computer(
    spec: TransferSpec,
    name: str,
    cancel: threading.Event,
    progress: ProgressCallback,
) -> tuple[str, int]:
    destination_dir = Path(spec.destination_dir)
    if not destination_dir.is_dir():
        raise TransferError(f"Computer destination is not a directory: {destination_dir}")
    total = _remote_size(spec.serial, spec.source)
    target = destination_dir / name
    part = Path(f"{target}.phonebridge-part")
    _check_destination(target, spec.overwrite)
    if target.is_dir():
        raise TransferError(f"A directory already exists at the destination: {target}")
    offset = _part_size(part)
    if offset > total:
        part.unlink()
        offset = 0
    _check_local_space(destination_dir, total - offset)
    if not part.exists():
        part.touch()

    process = _open_remote_reader(spec.serial, spec.source)
    try:
        if offset:
            try:
                with part.open("rb") as existing:
                    matches = _compare_prefix(process, existing, offset, cancel)
            except TransferCanceled:
                raise
            except Exception:
                matches = False
            if not matches:
                _stop_process(process)
                part.unlink(missing_ok=True)
                part.touch()
                offset = 0
                _check_local_space(destination_dir, total)
                process = _open_remote_reader(spec.serial, spec.source)

        progress(offset, total)
        mode = "ab" if offset else "wb"
        with part.open(mode) as output:
            try:
                done = offset
                while done < total:
                    _check_cancel(cancel, process)
                    chunk = _read_stream(process, min(CHUNK_SIZE, total - done), cancel)
                    if not chunk:
                        break
                    _write_file(output, chunk)
                    done += len(chunk)
                    progress(done, total)
                extra = _read_stream(process, 1, cancel)
                if extra:
                    raise TransferError("Phone file grew while it was being transferred.")
                _wait_reader(process, cancel)
                if done != total:
                    raise TransferError(f"Phone file ended early ({done} of {total} bytes).")
                output.flush()
                os.fsync(output.fileno())
            finally:
                if not output.closed:
                    output.flush()
                    os.fsync(output.fileno())
    except Exception:
        _stop_process(process)
        raise

    _check_cancel(cancel)
    if part.stat().st_size != total:
        raise TransferError("Temporary file size does not match the phone source.")
    _check_destination(target, spec.overwrite)
    if spec.overwrite:
        os.replace(part, target)
    else:
        _rename_no_replace(part, target)
    _fsync_directory(destination_dir)
    return str(target), total


def _computer_to_phone(
    spec: TransferSpec,
    name: str,
    cancel: threading.Event,
    progress: ProgressCallback,
) -> tuple[str, int]:
    source = Path(spec.source)
    if not source.is_file():
        raise TransferError(f"Computer source is not a regular file: {source}")
    total = source.stat().st_size
    target = posixpath.join(spec.destination_dir.rstrip("/"), name)
    part = f"{target}.phonebridge-part"
    _check_remote_destination(spec.serial, target, spec.overwrite)

    offset = 0
    if _remote_exists(spec.serial, part):
        part_size = _remote_size(spec.serial, part)
        if part_size <= total:
            try:
                reader = _open_remote_reader(spec.serial, part)
                with source.open("rb") as source_file:
                    matches = _compare_prefix(reader, source_file, part_size, cancel)
                _expect_reader_end(reader, cancel)
                _wait_reader(reader, cancel)
                if matches:
                    offset = part_size
                else:
                    _remote_remove(spec.serial, part)
            except TransferCanceled:
                raise
            except Exception:
                _remote_remove(spec.serial, part)
        else:
            _remote_remove(spec.serial, part)

    _check_remote_space(spec.serial, spec.destination_dir, total - offset)
    _check_cancel(cancel)
    progress(offset, total)
    command = f"cat {'>>' if offset else '>'} {shlex.quote(part)}"
    writer = _open_remote_writer(spec.serial, command)
    try:
        with source.open("rb") as source_file:
            source_file.seek(offset)
            done = offset
            while done < total:
                _check_cancel(cancel, writer)
                chunk = source_file.read(min(CHUNK_SIZE, total - done))
                if not chunk:
                    raise TransferError(f"Computer file ended early ({done} of {total} bytes).")
                _write_process(writer, chunk, cancel)
                done += len(chunk)
                progress(done, total)
            if source_file.read(1):
                raise TransferError("Computer file grew while it was being transferred.")
        _close_writer(writer, cancel)
    except Exception:
        _stop_process(writer)
        raise

    _check_cancel(cancel)
    part_size = _remote_size(spec.serial, part)
    if part_size != total:
        raise TransferError(f"Phone temporary file has {part_size} of {total} bytes.")

    _check_remote_destination(spec.serial, target, spec.overwrite)
    _check_cancel(cancel)
    move_option = "-fT" if spec.overwrite else "-nT"
    try:
        adb.shell(
            spec.serial,
            f"mv {move_option} {shlex.quote(part)} {shlex.quote(target)}",
            timeout=15,
        )
    except subprocess.CalledProcessError:
        if not spec.overwrite and _remote_exists(spec.serial, target):
            raise DestinationExists(target)
        raise
    if not spec.overwrite and _remote_exists(spec.serial, part):
        raise DestinationExists(target)
    final_size = _remote_size(spec.serial, target)
    if final_size != total:
        raise TransferError("Final phone file size does not match the source.")
    return target, total


def _check_local_space(directory: Path, required: int) -> None:
    free = shutil.disk_usage(directory).free
    if free < required:
        raise TransferError(
            f"Not enough computer disk space: need {required} bytes, have {free} bytes."
        )


def _check_remote_space(serial: str, directory: str, required: int) -> None:
    result = adb.shell(serial, f"df -Pk {shlex.quote(directory)}", timeout=15).stdout
    rows = [line.split() for line in result.splitlines() if line.split()]
    values = next((row for row in reversed(rows) if len(row) >= 5 and row[3].isdigit()), None)
    if values is None:
        raise TransferError("Could not determine free space on the phone (df output was unrecognized).")
    free = int(values[3]) * 1024
    if free < required:
        raise TransferError(f"Not enough phone storage: need {required} bytes, have {free} bytes.")


def _check_destination(target: Path, overwrite: bool) -> None:
    if os.path.lexists(target) and not overwrite:
        raise DestinationExists(str(target))


def _check_remote_destination(serial: str, target: str, overwrite: bool) -> None:
    if _remote_exists(serial, target) and not overwrite:
        raise DestinationExists(target)
    if _remote_is_directory(serial, target):
        raise TransferError(f"A directory already exists at the destination: {target}")


def _remote_exists(serial: str, path: str) -> bool:
    quoted = shlex.quote(path)
    output = adb.shell(
        serial,
        f"if [ -e {quoted} ] || [ -L {quoted} ]; then printf 1; else printf 0; fi",
        timeout=15,
    ).stdout
    return output.strip() == "1"


def _remote_is_directory(serial: str, path: str) -> bool:
    quoted = shlex.quote(path)
    output = adb.shell(
        serial,
        f"if [ -d {quoted} ]; then printf 1; else printf 0; fi",
        timeout=15,
    ).stdout
    return output.strip() == "1"


def _remote_size(serial: str, path: str) -> int:
    quoted = shlex.quote(path)
    try:
        output = adb.shell(serial, f"stat -c %s {quoted}", timeout=15).stdout.strip()
        return int(output.splitlines()[-1])
    except (ValueError, OSError, subprocess.SubprocessError):
        output = adb.shell(serial, f"wc -c < {quoted}", timeout=15).stdout.strip()
        try:
            return int(output)
        except ValueError as error:
            raise TransferError(f"Could not determine phone file size: {path}") from error


def _part_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _remote_remove(serial: str, path: str) -> None:
    adb.shell(serial, f"rm -f {shlex.quote(path)}", timeout=15)


def _open_remote_reader(serial: str, path: str) -> subprocess.Popen[bytes]:
    command = f"cat {shlex.quote(path)}"
    return subprocess.Popen(
        [adb.get_adb_path(), "-s", serial, "exec-out", "sh", "-c", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def _open_remote_writer(serial: str, command: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [adb.get_adb_path(), "-s", serial, "shell", command],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )


def _compare_prefix(
    process: subprocess.Popen[bytes],
    expected_file,
    length: int,
    cancel: threading.Event,
) -> bool:
    remaining = length
    while remaining:
        _check_cancel(cancel, process)
        amount = min(CHUNK_SIZE, remaining)
        expected = _read_file_exact(expected_file, amount)
        actual = _read_exact(process, amount, cancel)
        if len(expected) != amount or actual != expected:
            return False
        remaining -= amount
    return True


def _read_file_exact(source, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = source.read(length - len(result))
        if not chunk:
            break
        result.extend(chunk)
    return bytes(result)


def _read_exact(
    process: subprocess.Popen[bytes], length: int, cancel: threading.Event
) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = _read_stream(process, length - len(result), cancel)
        if not chunk:
            break
        result.extend(chunk)
    return bytes(result)


def _read_stream(
    process: subprocess.Popen[bytes], limit: int, cancel: threading.Event
) -> bytes:
    if process.stdout is None:
        raise TransferError("ADB reader has no output stream.")
    fd = process.stdout.fileno()
    while True:
        _check_cancel(cancel, process)
        readable, _, _ = select.select([fd], [], [], 0.2)
        if not readable:
            continue
        try:
            return os.read(fd, limit)
        except BlockingIOError:
            continue


def _expect_reader_end(process: subprocess.Popen[bytes], cancel: threading.Event) -> None:
    extra = _read_stream(process, 1, cancel)
    if extra:
        raise TransferError("Phone temporary file changed while checking its prefix.")


def _wait_reader(process: subprocess.Popen[bytes], cancel: threading.Event) -> None:
    if process.poll() is None:
        _wait_process(process, cancel)
    error = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    for stream in (process.stdout, process.stderr):
        if stream and not stream.closed:
            stream.close()
    if process.returncode != 0:
        raise TransferError(f"ADB could not read the phone file: {error.strip() or process.returncode}")


def _write_file(output, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = output.write(view)
        if written is None or written <= 0:
            raise TransferError("Could not write the temporary computer file.")
        view = view[written:]


def _write_process(process: subprocess.Popen[bytes], data: bytes, cancel: threading.Event) -> None:
    if process.stdin is None:
        raise TransferError("ADB writer has no input stream.")
    fd = process.stdin.fileno()
    os.set_blocking(fd, False)
    view = memoryview(data)
    while view:
        _check_cancel(cancel, process)
        _, writable, _ = select.select([], [fd], [], 0.2)
        if not writable:
            continue
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            continue
        if written <= 0:
            raise TransferError("ADB stopped accepting file data.")
        view = view[written:]


def _close_writer(process: subprocess.Popen[bytes], cancel: threading.Event) -> None:
    if process.stdin and not process.stdin.closed:
        process.stdin.close()
    _wait_process(process, cancel)
    if process.returncode != 0:
        raise TransferError(f"ADB could not write the phone temporary file (exit {process.returncode}).")


def _wait_process(process: subprocess.Popen[bytes], cancel: threading.Event) -> None:
    while process.poll() is None:
        if cancel.wait(0.1):
            _stop_process(process)
            raise TransferCanceled("Transfer canceled.")


def _check_cancel(
    cancel: threading.Event,
    process: Optional[subprocess.Popen[bytes]] = None,
) -> None:
    if cancel.is_set():
        if process is not None:
            _stop_process(process)
        raise TransferCanceled("Transfer canceled.")


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for stream in (process.stdout, process.stderr, process.stdin):
        if stream and not stream.closed:
            stream.close()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically commit without replacing a destination created concurrently."""
    at_fdcwd = -100
    rename_noreplace = 1
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError:
        renameat2 = None
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            at_fdcwd,
            os.fsencode(source),
            at_fdcwd,
            os.fsencode(destination),
            rename_noreplace,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise DestinationExists(str(destination))
        if error_number not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(error_number, os.strerror(error_number), str(destination))
    # Same-filesystem hard-link creation is also atomic and never replaces.
    os.link(source, destination)
    source.unlink()
