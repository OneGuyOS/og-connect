"""Comprehensive unit tests for phonebridge Phase 1."""

import os
from unittest import TestCase
from unittest.mock import MagicMock, patch
import subprocess

from PySide6.QtWidgets import QApplication

from phonebridge.adb import (
    AdbDevice,
    AdbNotFoundError,
    get_adb_path,
    list_devices,
    shell,
    exec_out_popen,
)
from phonebridge.device import (
    ConnectionState,
    DeviceManager,
    PhoneSession,
    _read_battery,
)
from phonebridge.window import MainWindow, format_status

# Ensure a QApplication exists for Qt tests
_app = QApplication.instance() or QApplication([])


class TestAdb(TestCase):
    def test_get_adb_path_env_var(self):
        with patch.dict(os.environ, {"ADB": "/usr/bin/adb"}):
            self.assertEqual(get_adb_path(), "/usr/bin/adb")

    def test_get_adb_path_not_found(self):
        with patch.dict(os.environ, {"ADB": "/nonexistent/path/adb"}):
            with self.assertRaises(AdbNotFoundError):
                get_adb_path()

        with patch.dict(os.environ, {}, clear=True):
            with patch("shutil.which", return_value=None):
                with self.assertRaises(AdbNotFoundError):
                    get_adb_path()

    def test_list_devices_parsing(self):
        mock_output = (
            "List of devices attached\n"
            "* daemon not running; starting now at tcp:5037\n"
            "* daemon started successfully\n"
            "USB123456              device usb:1-1 product:redfin model:Pixel_5 device:redfin transport_id:1\n"
            "192.168.1.50:5555      device product:cheetah model:Pixel_7_Pro device:cheetah transport_id:2\n"
            "UNAUTH0001             unauthorized usb:1-2 transport_id:3\n"
            "OFFLINE001             offline usb:1-3 transport_id:4\n"
            "NOPERM0001             no permissions (missing udev rules?); see [http://developer.android.com/tools/device.html] usb:1-4 transport_id:5\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["adb", "devices", "-l"],
                returncode=0,
                stdout=mock_output,
            )
            devices = list_devices()

        self.assertEqual(len(devices), 5)

        # 1. Usable USB device
        d0 = devices[0]
        self.assertEqual(d0.serial, "USB123456")
        self.assertEqual(d0.state, "device")
        self.assertEqual(d0.transport, "usb")
        self.assertEqual(d0.model, "Pixel_5")
        self.assertEqual(d0.product, "redfin")
        self.assertEqual(d0.device, "redfin")
        self.assertTrue(d0.is_usable)

        # 2. TCP device (must be transport tcp and not usable)
        d1 = devices[1]
        self.assertEqual(d1.serial, "192.168.1.50:5555")
        self.assertEqual(d1.state, "device")
        self.assertEqual(d1.transport, "tcp")
        self.assertFalse(d1.is_usable)

        # 3. Unauthorized USB device
        d2 = devices[2]
        self.assertEqual(d2.serial, "UNAUTH0001")
        self.assertEqual(d2.state, "unauthorized")
        self.assertEqual(d2.transport, "usb")
        self.assertFalse(d2.is_usable)

        # 4. Offline USB device
        d3 = devices[3]
        self.assertEqual(d3.serial, "OFFLINE001")
        self.assertEqual(d3.state, "offline")
        self.assertEqual(d3.transport, "usb")
        self.assertFalse(d3.is_usable)

        # 5. No permissions device
        d4 = devices[4]
        self.assertEqual(d4.serial, "NOPERM0001")
        self.assertEqual(d4.state, "no permissions")
        self.assertEqual(d4.transport, "usb")
        self.assertFalse(d4.is_usable)

    def test_shell_requires_serial(self):
        with self.assertRaises(ValueError):
            shell("", "dumpsys battery")

    def test_shell_command_args(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok")
            shell("SERIAL123", "echo test")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertIn("-s", args)
            self.assertIn("SERIAL123", args)
            self.assertIn("shell", args)
            self.assertIn("echo test", args)

    def test_exec_out_popen_requires_serial(self):
        with self.assertRaises(ValueError):
            exec_out_popen("", ["ls"])

    def test_exec_out_popen_args(self):
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            exec_out_popen("SERIAL123", ["cat", "/proc/version"])
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            self.assertIn("-s", args)
            self.assertIn("SERIAL123", args)
            self.assertIn("exec-out", args)
            self.assertIn("cat", args)


class TestDevice(TestCase):
    def test_read_battery_success(self):
        dumpsys_output = (
            "Current Battery Service state:\n"
            "  AC powered: false\n"
            "  USB powered: true\n"
            "  Wireless powered: false\n"
            "  status: 2\n"
            "  level: 85\n"
            "  scale: 100\n"
        )
        with patch("phonebridge.device.shell") as mock_shell:
            mock_shell.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=dumpsys_output
            )
            level, charging = _read_battery("SERIAL1")
            self.assertEqual(level, 85)
            self.assertTrue(charging)

    def test_read_battery_failure_returns_nones(self):
        with patch("phonebridge.device.shell", side_effect=Exception("adb connection failed")):
            level, charging = _read_battery("SERIAL1")
            self.assertIsNone(level)
            self.assertIsNone(charging)

    def test_device_manager_usb_connection(self):
        dev = AdbDevice("SERIAL_USB_1", "device", "usb", model="Pixel_7")
        with patch("phonebridge.device.list_devices", return_value=[dev]):
            with patch("phonebridge.device._read_battery", return_value=(92, True)):
                mgr = DeviceManager()
                self.assertEqual(mgr.state, ConnectionState.USB)
                self.assertIsNotNone(mgr.session)
                self.assertEqual(mgr.session.device.serial, "SERIAL_USB_1")
                self.assertEqual(mgr.session.battery_level, 92)
                self.assertTrue(mgr.session.charging)
                mgr.stop()

    def test_device_manager_prefers_previously_selected_usb_device(self):
        dev1 = AdbDevice("SERIAL_1", "device", "usb", model="Phone_1")
        dev2 = AdbDevice("SERIAL_2", "device", "usb", model="Phone_2")

        with patch("phonebridge.device._read_battery", return_value=(50, False)):
            with patch("phonebridge.device.list_devices", return_value=[dev1]):
                mgr = DeviceManager()
                self.assertEqual(mgr.session.device.serial, "SERIAL_1")

            # Now both dev1 and dev2 are attached in list [dev2, dev1]
            with patch("phonebridge.device.list_devices", return_value=[dev2, dev1]):
                mgr.poll()
                # Should retain SERIAL_1 because it was selected last time
                self.assertEqual(mgr.session.device.serial, "SERIAL_1")
                mgr.stop()

    def test_device_manager_unauthorized(self):
        unauth_dev = AdbDevice("UNAUTH_1", "unauthorized", "usb")
        with patch("phonebridge.device.list_devices", return_value=[unauth_dev]):
            mgr = DeviceManager()
            self.assertEqual(mgr.state, ConnectionState.UNAUTHORIZED)
            self.assertIsNone(mgr.session)
            mgr.stop()

    def test_device_manager_disconnected(self):
        # Empty list
        with patch("phonebridge.device.list_devices", return_value=[]):
            mgr = DeviceManager()
            self.assertEqual(mgr.state, ConnectionState.DISCONNECTED)
            self.assertIsNone(mgr.session)
            mgr.stop()

        # Only TCP device attached (which is not usable)
        tcp_dev = AdbDevice("192.168.1.5:5555", "device", "tcp", model="Pixel_Tcp")
        with patch("phonebridge.device.list_devices", return_value=[tcp_dev]):
            mgr = DeviceManager()
            self.assertEqual(mgr.state, ConnectionState.DISCONNECTED)
            self.assertIsNone(mgr.session)
            mgr.stop()


class TestWindow(TestCase):
    def test_format_status_strings(self):
        # Disconnected
        self.assertEqual(
            format_status(ConnectionState.DISCONNECTED, None),
            "No phone connected",
        )

        # Unauthorized
        self.assertEqual(
            format_status(ConnectionState.UNAUTHORIZED, None),
            "Phone unauthorized — check the prompt on your phone",
        )

        # Connected USB charging
        dev = AdbDevice("USB1", "device", "usb", model="Galaxy_S24")
        session_charging = PhoneSession(dev, battery_level=80, charging=True)
        self.assertEqual(
            format_status(ConnectionState.USB, session_charging),
            "Connected over USB: Galaxy_S24 — battery 80% (charging)",
        )

        # Connected USB not charging
        session_not_charging = PhoneSession(dev, battery_level=79, charging=False)
        self.assertEqual(
            format_status(ConnectionState.USB, session_not_charging),
            "Connected over USB: Galaxy_S24 — battery 79% (not charging)",
        )

        # Connected USB battery unknown
        session_no_batt = PhoneSession(dev, battery_level=None, charging=None)
        self.assertEqual(
            format_status(ConnectionState.USB, session_no_batt),
            "Connected over USB: Galaxy_S24 — battery unknown",
        )

    def test_window_updates_on_signal(self):
        dev = AdbDevice("TEST1", "device", "usb", model="Pixel_6")
        with patch("phonebridge.device.list_devices", return_value=[]):
            mgr = DeviceManager()
            window = MainWindow(mgr)
            self.assertEqual(window._status_label.text(), "No phone connected")

            # Simulate transition to unauthorized
            mgr.state_changed.emit(ConnectionState.UNAUTHORIZED, None)
            self.assertEqual(
                window._status_label.text(),
                "Phone unauthorized — check the prompt on your phone",
            )

            # Simulate transition to USB
            session = PhoneSession(dev, battery_level=65, charging=True)
            mgr.state_changed.emit(ConnectionState.USB, session)
            self.assertEqual(
                window._status_label.text(),
                "Connected over USB: Pixel_6 — battery 65% (charging)",
            )
            mgr.stop()
            window.close()
