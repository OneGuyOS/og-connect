"""Local image viewer and simulated disconnect state checks."""

from __future__ import annotations

import secrets
import tempfile
import threading
from pathlib import Path
import unittest

from PySide6.QtCore import QEventLoop, QItemSelectionModel, QObject, QPoint, QPointF, QTimer, Qt, Signal
from PySide6.QtGui import QImage, QColor, QWheelEvent
from PySide6.QtWidgets import QApplication

from phonebridge.device import ConnectionState, PhoneSession
from phonebridge.adb import AdbDevice
from phonebridge.panes import ComputerPane
from phonebridge.viewer import ImageViewer, ViewerState
from phonebridge.window import MainWindow


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Manager(QObject):
    state_changed = Signal(object, object)

    def __init__(self, session=None):
        super().__init__()
        self.session = session
        self.state = ConnectionState.DISCONNECTED


def wait_for(predicate, timeout=5.0):
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


class ViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT, prefix=".viewer-test-")
        self.root = Path(self.temp.name)
        self.first = self.root / "first.png"
        self.second = self.root / "second.png"
        for path, color in ((self.first, QColor("red")), (self.second, QColor("blue"))):
            image = QImage(320, 160, QImage.Format.Format_RGB32)
            image.fill(color)
            self.assertTrue(image.save(str(path)))
        self.manager = Manager()
        self.viewer = ImageViewer(self.manager)

    def tearDown(self):
        self.viewer.close()
        self.temp.cleanup()

    def test_loading_fit_zoom_rotate_and_navigation(self):
        paths = [str(self.first), str(self.second)]
        self.viewer.open_image_set(paths, paths[0], False)
        self.assertEqual(self.viewer.state, ViewerState.LOADING)
        self.assertIn("Loading image", self.viewer.message.text())
        self.viewer.show()
        self.assertTrue(wait_for(lambda: self.viewer.state == ViewerState.LOADED))
        self.assertEqual(self.viewer._image.size().width(), 320)
        self.assertTrue(self.viewer._fit_mode)

        self.viewer.zoom_in_button.click()
        self.assertGreater(self.viewer._zoom, 1.0)
        self.viewer.rotate_right_button.click()
        self.assertEqual(self.viewer._rotation, 90)
        self.viewer.rotate_left_button.click()
        self.assertEqual(self.viewer._rotation, 0)
        wheel = QWheelEvent(
            QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        previous_zoom = self.viewer._zoom
        self.viewer.canvas.wheelEvent(wheel)
        self.assertGreater(self.viewer._zoom, previous_zoom)
        self.viewer.fit_button.click()
        self.assertTrue(self.viewer._fit_mode)
        self.assertEqual(self.viewer._zoom, 1.0)
        self.assertAlmostEqual(
            self.viewer.canvas.transform().m11(),
            self.viewer.canvas.transform().m22(),
            places=5,
        )

        self.viewer.next_button.click()
        self.assertTrue(wait_for(lambda: self.viewer.state == ViewerState.LOADED and self.viewer._index == 1))
        self.assertEqual(self.viewer._paths[self.viewer._index], paths[1])
        self.viewer.previous_button.click()
        self.assertTrue(wait_for(lambda: self.viewer.state == ViewerState.LOADED and self.viewer._index == 0))

        self.viewer.close()
        self.viewer.open_image_set(paths, paths[1], False)
        self.viewer.show()
        self.assertTrue(wait_for(lambda: self.viewer.state == ViewerState.LOADED and self.viewer._index == 1))

    def test_corrupt_and_unsupported_states(self):
        corrupt = self.root / "broken.png"
        corrupt.write_bytes(b"not a png image")
        unsupported = self.root / "document.unrecognized"
        unsupported.write_bytes(b"not an image")

        self.viewer.open_image_set([str(corrupt)], str(corrupt), False)
        self.assertTrue(wait_for(lambda: self.viewer.state != ViewerState.LOADING))
        self.assertEqual(self.viewer.state, ViewerState.CORRUPT)
        self.assertTrue(self.viewer.message.text())
        self.assertIs(self.viewer.content.currentWidget(), self.viewer._state_widget)

        self.viewer.open_image_set([str(unsupported)], str(unsupported), False)
        self.assertTrue(wait_for(lambda: self.viewer.state != ViewerState.LOADING))
        self.assertEqual(self.viewer.state, ViewerState.UNSUPPORTED)
        self.assertTrue(self.viewer.message.text())

    def test_phone_disconnect_while_loading_is_simulated(self):
        serial = "sim-" + secrets.token_hex(8)
        session = PhoneSession(AdbDevice(serial, "device", "usb"), None, None)
        self.manager.session = session
        entered = threading.Event()

        import phonebridge.viewer as viewer_module
        original_loader = viewer_module._load_image

        def wait_for_cancel(_path, is_phone, _serial, cancel):
            self.assertTrue(is_phone)
            entered.set()
            cancel.wait(3)
            raise viewer_module.ViewerCanceled("Canceled after simulated disconnect")

        viewer_module._load_image = wait_for_cancel
        try:
            path = "/storage/emulated/0/simulated.png"
            self.viewer.open_image_set([path], path, True, serial)
            self.assertTrue(wait_for(entered.is_set))
            self.manager.session = None
            self.manager.state_changed.emit(ConnectionState.DISCONNECTED, None)
            self.assertEqual(self.viewer.state, ViewerState.PHONE_DISCONNECTED)
            self.assertEqual(self.viewer.message.text(), "Phone disconnected")
            self.assertIs(self.viewer.content.currentWidget(), self.viewer._state_widget)
            self.assertTrue(wait_for(lambda: self.viewer._task is None))
            self.assertEqual(self.viewer.state, ViewerState.PHONE_DISCONNECTED)
        finally:
            viewer_module._load_image = original_loader

    def test_computer_open_signal_uses_folder_order_and_preserves_selection(self):
        manager = Manager()
        window = MainWindow(manager)
        pane = window.computer_pane
        pane.navigate(str(self.root))
        pane.view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        window.show()
        try:
            self.assertTrue(wait_for(lambda: pane._model.rowCount(pane.view.rootIndex()) >= 2))
            selected_path = str(self.second)
            index = pane._model.index(selected_path)
            self.assertTrue(index.isValid())
            pane.view.setCurrentIndex(index)
            pane.view.selectionModel().select(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
            ordered_paths = pane._image_paths(selected_path)
            self.assertEqual(ordered_paths, [str(self.second), str(self.first)])

            position = pane.view.visualRect(index).center()
            pane._show_context_menu(position)
            menu = pane._context_menu
            action = next(action for action in menu.actions() if action.text() == "Open image")
            action.trigger()
            menu.close()
            self.assertTrue(wait_for(lambda: window._viewer is not None and window._viewer.state == ViewerState.LOADED))
            self.assertEqual(pane.current_path, str(self.root))
            selected = pane.view.selectionModel().selectedRows(0)
            self.assertEqual(pane._model.filePath(selected[0]), selected_path)

            window._viewer.close()
            pane._show_context_menu(position)
            menu = pane._context_menu
            action = next(action for action in menu.actions() if action.text() == "Open image")
            action.trigger()
            menu.close()
            self.assertTrue(wait_for(lambda: window._viewer.state == ViewerState.LOADED))
            selected = pane.view.selectionModel().selectedRows(0)
            self.assertEqual(pane._model.filePath(selected[0]), selected_path)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
