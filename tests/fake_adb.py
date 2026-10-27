#!/usr/bin/env python3
"""ADB executable used only by transfer tests; its device tree is local."""

import os
from pathlib import Path
import shlex
import sys
import time


DEVICE_ROOT = "/storage/emulated/0"


def translate(text: str) -> str:
    return text.replace(DEVICE_ROOT, os.environ["PHONEBRIDGE_TEST_PHONE_ROOT"])


def stream_to_remote(command: str) -> int:
    words = shlex.split(command)
    if len(words) != 3 or words[0] != "cat" or words[1] not in (">", ">>"):
        return -1
    path = Path(translate(words[2]))
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if words[1] == ">>" else "wb"
    slow = os.environ.get("PHONEBRIDGE_TEST_SLOW") == "1"
    with path.open(mode, buffering=0) as output:
        while True:
            data = sys.stdin.buffer.read(32 * 1024)
            if not data:
                break
            output.write(data)
            if slow:
                time.sleep(0.003)
    return 0


def stream_from_remote(command: str) -> int:
    words = shlex.split(command)
    if len(words) != 2 or words[0] != "cat":
        return -1
    path = Path(translate(words[1]))
    slow = os.environ.get("PHONEBRIDGE_TEST_SLOW") == "1"
    with path.open("rb", buffering=0) as source:
        while True:
            data = source.read(32 * 1024)
            if not data:
                break
            sys.stdout.buffer.write(data)
            if slow:
                sys.stdout.buffer.flush()
                time.sleep(0.003)
    return 0


def main() -> int:
    args = sys.argv[1:]
    serial = os.environ["PHONEBRIDGE_TEST_SERIAL"]
    if args == ["devices", "-l"]:
        print("List of devices attached")
        print(f"{serial} device model:Simulated product:simulated device:simulated")
        return 0
    if len(args) < 3 or args[:2] != ["-s", serial]:
        print("explicit simulated serial required", file=sys.stderr)
        return 2
    mode = args[2]
    if mode == "shell":
        command = args[3]
        if command.startswith("cat > ") or command.startswith("cat >> "):
            return stream_to_remote(command)
        if command.startswith("df -Pk ") and "PHONEBRIDGE_TEST_FREE_KIB" in os.environ:
            print("Filesystem 1024-blocks Used Available Capacity Mounted on")
            print(f"simulated 1000 999 {os.environ['PHONEBRIDGE_TEST_FREE_KIB']} 99% /storage/emulated/0")
            return 0
        if command == "dumpsys battery":
            print("level: 88\nstatus: 3")
            return 0
        os.execv("/bin/sh", ["sh", "-c", translate(command)])
    if mode == "exec-out":
        if len(args) == 6 and args[3:5] == ["sh", "-c"]:
            return stream_from_remote(args[5])
        if len(args) >= 5 and args[3] == "cat":
            return stream_from_remote(" ".join(args[3:]))
        print("unsupported simulated exec-out command", file=sys.stderr)
        return 2
    print("unsupported simulated adb command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
