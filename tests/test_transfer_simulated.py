"""Offline ADB simulations for streaming transfer and queue behavior."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from phonebridge.device import DeviceManager
from phonebridge.transfer import (
    DestinationExists,
    TransferCanceled,
    TransferDirection,
    TransferError,
    TransferSpec,
    transfer_file,
)
from phonebridge.transfer_queue import JobState, TransferQueue
from phonebridge.window import MainWindow
import phonebridge.transfer as transfer_module


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_KEYS = (
    "ADB",
    "PHONEBRIDGE_TEST_PHONE_ROOT",
    "PHONEBRIDGE_TEST_SERIAL",
    "PHONEBRIDGE_TEST_SLOW",
    "PHONEBRIDGE_TEST_FREE_KIB",
)


def wait_for(predicate, timeout: float = 15.0) -> bool:
    app = QApplication.instance()
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)

    def check():
        if predicate():
            timer.stop()
            loop.quit()

    timer.timeout.connect(check)
    timer.start()
    QTimer.singleShot(int(timeout * 1000), loop.quit)
    check()
    if not predicate():
        loop.exec()
    timer.stop()
    return bool(predicate())


class SimulatedTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.previous_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT, prefix=".transfer-test-")
        self.root = Path(self.temp.name)
        self.phone_root = self.root / "simulated-phone"
        self.phone_root.mkdir()
        self.serial = "sim-" + secrets.token_hex(8)
        self.fake_adb = self.root / "fake-adb"
        shutil.copyfile(Path(__file__).with_name("fake_adb.py"), self.fake_adb)
        self.fake_adb.chmod(0o755)
        os.environ["ADB"] = str(self.fake_adb)
        os.environ["PHONEBRIDGE_TEST_PHONE_ROOT"] = str(self.phone_root)
        os.environ["PHONEBRIDGE_TEST_SERIAL"] = self.serial
        os.environ.pop("PHONEBRIDGE_TEST_SLOW", None)
        os.environ.pop("PHONEBRIDGE_TEST_FREE_KIB", None)

    def tearDown(self):
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def _spec(self, source: Path, direction=TransferDirection.COMPUTER_TO_PHONE, destination=None):
        if destination is None:
            destination = "/storage/emulated/0" if direction == TransferDirection.COMPUTER_TO_PHONE else str(self.root / "computer")
        Path(destination).mkdir(parents=True, exist_ok=True) if direction == TransferDirection.PHONE_TO_COMPUTER else None
        return TransferSpec(str(source), str(destination), direction, self.serial)

    def test_both_directions_content_conflict_and_resume(self):
        data = (bytes(range(256)) * 8192) + b"phonebridge-end"
        computer_file = self.root / "roundtrip.bin"
        computer_file.write_bytes(data)
        to_phone = self._spec(computer_file)
        remote_path, count = transfer_file(to_phone, threading.Event(), lambda *_: None)
        self.assertEqual(count, len(data))
        self.assertEqual((self.phone_root / "roundtrip.bin").read_bytes(), data)

        computer_destination = self.root / "computer-copy"
        computer_destination.mkdir()
        to_computer = self._spec(
            Path(remote_path), TransferDirection.PHONE_TO_COMPUTER, computer_destination
        )
        local_path, count = transfer_file(to_computer, threading.Event(), lambda *_: None)
        self.assertEqual(count, len(data))
        self.assertEqual(Path(local_path).read_bytes(), data)

        resume_target = computer_destination / "resume-download.bin"
        resume_target.with_name(resume_target.name + ".phonebridge-part").write_bytes(data[:700_000])
        resume_download = TransferSpec(
            remote_path,
            str(computer_destination),
            TransferDirection.PHONE_TO_COMPUTER,
            self.serial,
            target_name=resume_target.name,
        )
        transfer_file(resume_download, threading.Event(), lambda *_: None)
        self.assertEqual(resume_target.read_bytes(), data)

        mismatch_target = computer_destination / "mismatch-download.bin"
        mismatch_target.with_name(mismatch_target.name + ".phonebridge-part").write_bytes(b"wrong prefix")
        mismatch_download = TransferSpec(
            remote_path,
            str(computer_destination),
            TransferDirection.PHONE_TO_COMPUTER,
            self.serial,
            target_name=mismatch_target.name,
        )
        transfer_file(mismatch_download, threading.Event(), lambda *_: None)
        self.assertEqual(mismatch_target.read_bytes(), data)

        existing = self.phone_root / "roundtrip.bin"
        existing.write_bytes(b"preserve this until a choice is made")
        with self.assertRaises(DestinationExists):
            transfer_file(to_phone, threading.Event(), lambda *_: None)
        self.assertEqual(existing.read_bytes(), b"preserve this until a choice is made")

        to_phone.overwrite = True
        transfer_file(to_phone, threading.Event(), lambda *_: None)
        self.assertEqual(existing.read_bytes(), data)

        partial_source = self.root / "resumable.bin"
        partial_data = os.urandom(4 * 1024 * 1024)
        partial_source.write_bytes(partial_data)
        partial_spec = self._spec(partial_source)
        cancel = threading.Event()

        def cancel_after_prefix(done, _total):
            if done >= 64 * 1024:
                cancel.set()

        with self.assertRaises(TransferCanceled):
            transfer_file(partial_spec, cancel, cancel_after_prefix)
        part = self.phone_root / "resumable.bin.phonebridge-part"
        self.assertGreater(part.stat().st_size, 0)
        self.assertFalse((self.phone_root / "resumable.bin").exists())
        transfer_file(partial_spec, threading.Event(), lambda *_: None)
        self.assertEqual((self.phone_root / "resumable.bin").read_bytes(), partial_data)

        mismatch_source = self.root / "mismatch.bin"
        mismatch_data = b"correct source bytes" * 1024
        mismatch_source.write_bytes(mismatch_data)
        (self.phone_root / "mismatch.bin.phonebridge-part").write_bytes(b"x" * 1000)
        transfer_file(self._spec(mismatch_source), threading.Event(), lambda *_: None)
        self.assertEqual((self.phone_root / "mismatch.bin").read_bytes(), mismatch_data)

    def test_queue_order_cancel_current_and_cancel_all(self):
        os.environ["PHONEBRIDGE_TEST_SLOW"] = "1"
        large_a = self.root / "large-a.bin"
        large_b = self.root / "large-b.bin"
        small = self.root / "next.bin"
        large_a.write_bytes(os.urandom(24 * 1024 * 1024))
        large_b.write_bytes(os.urandom(24 * 1024 * 1024))
        small.write_bytes(b"next in queue")

        queue = TransferQueue()
        first, second = queue.add_jobs([self._spec(large_a), self._spec(small)])
        part_a = self.phone_root / "large-a.bin.phonebridge-part"
        self.assertTrue(wait_for(lambda: part_a.exists() and part_a.stat().st_size > 0))
        queue.cancel_current()
        self.assertTrue(wait_for(lambda: first.state == JobState.CANCELED and second.state == JobState.DONE))
        self.assertTrue(part_a.exists())
        self.assertFalse((self.phone_root / "large-a.bin").exists())
        self.assertEqual((self.phone_root / "next.bin").read_bytes(), small.read_bytes())

        os.environ.pop("PHONEBRIDGE_TEST_SLOW", None)
        transfer_file(self._spec(large_a), threading.Event(), lambda *_: None)
        self.assertEqual((self.phone_root / "large-a.bin").read_bytes(), large_a.read_bytes())

        ordered_queue = TransferQueue()
        ordered_a = self.root / "ordered-a.bin"
        ordered_b = self.root / "ordered-b.bin"
        ordered_a.write_bytes(b"first")
        ordered_b.write_bytes(b"second")
        done_order = []
        seen_done = set()

        def remember_done():
            for item in ordered_queue.jobs:
                if item.state == JobState.DONE and item.job_id not in seen_done:
                    seen_done.add(item.job_id)
                    done_order.append(item.source_name)

        ordered_queue.changed.connect(remember_done)
        ordered_jobs = ordered_queue.add_jobs([self._spec(ordered_a), self._spec(ordered_b)])
        self.assertTrue(wait_for(lambda: all(job.state == JobState.DONE for job in ordered_jobs)))
        self.assertEqual(done_order, ["ordered-a.bin", "ordered-b.bin"])

        os.environ["PHONEBRIDGE_TEST_SLOW"] = "1"
        queue2 = TransferQueue()
        current, pending = queue2.add_jobs([self._spec(large_b), self._spec(small)])
        part_b = self.phone_root / "large-b.bin.phonebridge-part"
        self.assertTrue(wait_for(lambda: part_b.exists() and part_b.stat().st_size > 0))
        queue2.cancel_all()
        self.assertTrue(wait_for(lambda: current.state == JobState.CANCELED and pending.state == JobState.CANCELED))
        self.assertTrue(part_b.exists())
        self.assertFalse((self.phone_root / "large-b.bin").exists())

    def test_killed_adb_process_fails_without_final_file(self):
        os.environ["PHONEBRIDGE_TEST_SLOW"] = "1"
        source = self.root / "killed.bin"
        source.write_bytes(os.urandom(24 * 1024 * 1024))
        spec = self._spec(source)
        captured = {}
        original_open = transfer_module._open_remote_writer

        def capture_writer(serial, command):
            process = original_open(serial, command)
            captured["process"] = process
            return process

        transfer_module._open_remote_writer = capture_writer
        queue = TransferQueue()
        try:
            job = queue.add_jobs([spec])[0]
            part = self.phone_root / "killed.bin.phonebridge-part"
            self.assertTrue(wait_for(lambda: "process" in captured and part.exists() and part.stat().st_size > 0))
            captured["process"].terminate()
            self.assertTrue(wait_for(lambda: job.state == JobState.FAILED))
        finally:
            transfer_module._open_remote_writer = original_open
        self.assertTrue(job.message)
        self.assertTrue(part.exists())
        self.assertFalse((self.phone_root / "killed.bin").exists())

    def test_local_and_phone_free_space_checks(self):
        source = self.root / "space-check.bin"
        source.write_bytes(b"requires space")
        local_destination = self.root / "computer-space"
        local_destination.mkdir()
        phone_spec = self._spec(source)
        computer_spec = self._spec(
            Path("/storage/emulated/0/existing.bin"),
            TransferDirection.PHONE_TO_COMPUTER,
            local_destination,
        )
        (self.phone_root / "existing.bin").write_bytes(b"on phone")
        usage = shutil.disk_usage(local_destination)._replace(free=0)
        with patch("phonebridge.transfer.shutil.disk_usage", return_value=usage):
            with self.assertRaisesRegex(TransferError, "Not enough computer disk space"):
                transfer_file(computer_spec, threading.Event(), lambda *_: None)
        self.assertFalse((local_destination / "existing.bin.phonebridge-part").exists())

        os.environ["PHONEBRIDGE_TEST_FREE_KIB"] = "0"
        with self.assertRaisesRegex(TransferError, "Not enough phone storage"):
            transfer_file(phone_spec, threading.Event(), lambda *_: None)
        self.assertFalse((self.phone_root / "space-check.bin.phonebridge-part").exists())

    def test_destination_dialog_requires_an_explicit_choice(self):
        manager = DeviceManager()
        window = MainWindow(manager)
        try:
            self.assertTrue(wait_for(lambda: manager.session is not None))
            source = self.root / "dialog.bin"
            source.write_bytes(b"new data")
            destination = self.phone_root / source.name
            destination.write_bytes(b"old data")

            window._copy_to_phone([str(source)])
            job = window.transfer_queue.jobs[-1]
            self.assertTrue(wait_for(lambda: job.job_id in window._conflict_prompts))
            self.assertEqual(destination.read_bytes(), b"old data")
            box = window._conflict_prompts[job.job_id]
            next(button for button in box.buttons() if button.text() == "Overwrite").click()
            self.assertTrue(wait_for(lambda: job.state == JobState.DONE))
            self.assertEqual(destination.read_bytes(), b"new data")

            destination.write_bytes(b"keep for skip")
            window._copy_to_phone([str(source)])
            skipped = window.transfer_queue.jobs[-1]
            self.assertTrue(wait_for(lambda: skipped.job_id in window._conflict_prompts))
            box = window._conflict_prompts[skipped.job_id]
            next(button for button in box.buttons() if button.text() == "Skip").click()
            self.assertTrue(wait_for(lambda: skipped.state == JobState.CANCELED))
            self.assertEqual(destination.read_bytes(), b"keep for skip")

            window._copy_to_phone([str(source)])
            renamed = window.transfer_queue.jobs[-1]
            self.assertTrue(wait_for(lambda: renamed.job_id in window._conflict_prompts))
            box = window._conflict_prompts[renamed.job_id]
            next(button for button in box.buttons() if button.text() == "Rename").click()
            self.assertTrue(wait_for(lambda: renamed.job_id in window._conflict_prompts))
            dialog = window._conflict_prompts[renamed.job_id]
            dialog.setTextValue("renamed-dialog.bin")
            dialog.accept()
            self.assertTrue(wait_for(lambda: renamed.state == JobState.DONE))
            self.assertEqual((self.phone_root / "renamed-dialog.bin").read_bytes(), b"new data")
        finally:
            manager._timer.stop()
            window.close()

    def test_switching_pages_does_not_stop_queue(self):
        os.environ["PHONEBRIDGE_TEST_SLOW"] = "1"
        manager = DeviceManager()
        window = MainWindow(manager)
        try:
            self.assertTrue(wait_for(lambda: manager.session is not None))
            source = self.root / "page-switch.bin"
            source.write_bytes(os.urandom(2 * 1024 * 1024))
            job = window.transfer_queue.add_jobs([self._spec(source)])[0]
            window.pages.setCurrentIndex(1)
            window.pages.setCurrentIndex(0)
            self.assertTrue(wait_for(lambda: job.state == JobState.DONE))
            self.assertEqual((self.phone_root / source.name).read_bytes(), source.read_bytes())
        finally:
            manager._timer.stop()
            window.close()


if __name__ == "__main__":
    unittest.main()
