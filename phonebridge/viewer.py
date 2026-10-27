"""Asynchronous local and USB image viewer."""

from __future__ import annotations

from enum import Enum
import os
from pathlib import Path
import select
import shlex
import subprocess
import threading
from typing import Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QThreadPool, QTimer
from PySide6.QtGui import QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import adb
from .device import ConnectionState, DeviceManager, PhoneSession
from .workers import FunctionTask


IMAGE_EXTENSIONS = frozenset(
    {
        ".avif", ".bmp", ".dib", ".gif", ".heic", ".heif", ".ico", ".jpe",
        ".jpeg", ".jpg", ".pbm", ".pgm", ".png", ".ppm", ".svg", ".svgz",
        ".tif", ".tiff", ".webp", ".xbm", ".xpm",
    }
)
_CHUNK_SIZE = 256 * 1024
LOAD_TIMEOUT_MS = 60_000


class ViewerState(str, Enum):
    LOADING = "loading"
    LOADED = "loaded"
    UNSUPPORTED = "unsupported format"
    CORRUPT = "corrupt file"
    PHONE_DISCONNECTED = "phone disconnected"
    LOAD_ERROR = "load error"


class UnsupportedImageError(RuntimeError):
    pass


class CorruptImageError(RuntimeError):
    pass


class ViewerCanceled(RuntimeError):
    pass


def is_image_candidate(path: str) -> bool:
    return Path(path).suffix.casefold() in IMAGE_EXTENSIONS


def _load_image(path: str, is_phone: bool, serial: Optional[str], cancel: threading.Event) -> QImage:
    if is_phone:
        if not serial:
            raise ViewerCanceled("Phone disconnected.")
        image_bytes = _read_phone_image(serial, path, cancel)
        buffer = QBuffer()
        buffer.setData(QByteArray(image_bytes))
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            raise CorruptImageError("Could not open image data.")
        reader = QImageReader(buffer, _format_hint(path))
        reader.setAutoTransform(True)
        image = _decode(reader, path)
        buffer.close()
        return image

    reader = QImageReader(path)
    reader.setAutoTransform(True)
    return _decode(reader, path)


def _format_hint(path: str) -> QByteArray:
    extension = Path(path).suffix.casefold().lstrip(".")
    supported = {bytes(item).decode("ascii", errors="ignore").casefold() for item in QImageReader.supportedImageFormats()}
    return QByteArray(extension.encode("ascii")) if extension in supported else QByteArray()


def _decode(reader: QImageReader, path: str) -> QImage:
    suffix = Path(path).suffix.casefold()
    supported = {bytes(item).decode("ascii", errors="ignore").casefold() for item in QImageReader.supportedImageFormats()}
    format_name = bytes(reader.format()).decode("ascii", errors="ignore").casefold()
    if not reader.canRead():
        if format_name and format_name not in supported:
            raise UnsupportedImageError(f"Unsupported image format: {format_name}")
        if not format_name and suffix.lstrip(".") not in supported:
            raise UnsupportedImageError(f"Unsupported image format: {suffix or 'unknown'}")
        raise CorruptImageError(f"The image is corrupt or unreadable: {Path(path).name}")
    image = reader.read()
    if image.isNull():
        if format_name and format_name not in supported:
            raise UnsupportedImageError(f"Unsupported image format: {format_name}")
        raise CorruptImageError(f"The image is corrupt or unreadable: {Path(path).name}")
    return image


def _read_phone_image(serial: str, path: str, cancel: threading.Event) -> bytes:
    process = adb.exec_out_popen(serial, ["cat", shlex.quote(path)])
    if process.stdout is None:
        _stop_reader(process)
        raise CorruptImageError("ADB did not provide image data.")
    data = bytearray()
    fd = process.stdout.fileno()
    try:
        while True:
            if cancel.is_set():
                _stop_reader(process)
                raise ViewerCanceled("Image load canceled.")
            readable, _, _ = select.select([fd], [], [], 0.15)
            if not readable:
                continue
            chunk = os.read(fd, _CHUNK_SIZE)
            if not chunk:
                break
            data.extend(chunk)
        while process.poll() is None:
            if cancel.wait(0.05):
                _stop_reader(process)
                raise ViewerCanceled("Image load canceled.")
        error_text = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        if process.returncode != 0:
            raise CorruptImageError(error_text.strip() or "Could not read the phone image.")
        return bytes(data)
    finally:
        for stream in (process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()


def _stop_reader(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for stream in (process.stdout, process.stderr):
        if stream and not stream.closed:
            stream.close()


class ImageCanvas(QGraphicsView):
    def __init__(self, viewer: "ImageViewer") -> None:
        super().__init__()
        self.viewer = viewer
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self.setRenderHints(self.renderHints() | QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._item: Optional[QGraphicsPixmapItem] = None

    def set_image(self, image: QImage) -> None:
        self.scene.clear()
        self._item = self.scene.addPixmap(QPixmap.fromImage(image))
        self._item.setTransformOriginPoint(self._item.boundingRect().center())
        self.scene.setSceneRect(self._item.sceneBoundingRect())

    def fit_image(self) -> None:
        if self._item is not None:
            self.scene.setSceneRect(self._item.sceneBoundingRect())
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.viewer.zoom(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
            event.accept()
            return
        super().wheelEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.viewer._fit_mode and self._item is not None:
            QTimer.singleShot(0, self.fit_image)


class ImageViewer(QDialog):
    """Non-modal image viewer; source lists are snapshots from a pane folder."""

    def __init__(self, manager: DeviceManager, parent=None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._paths: list[str] = []
        self._index = 0
        self._is_phone = False
        self._serial: Optional[str] = None
        self._generation = 0
        self._task: Optional[FunctionTask] = None
        self._cancel: Optional[threading.Event] = None
        self._image: Optional[QImage] = None
        self._rotation = 0
        self._zoom = 1.0
        self._fit_mode = True
        self.state = ViewerState.LOADING

        self.previous_button = QPushButton("Previous")
        self.next_button = QPushButton("Next")
        self.zoom_out_button = QPushButton("Zoom out")
        self.zoom_in_button = QPushButton("Zoom in")
        self.rotate_left_button = QPushButton("Rotate left")
        self.rotate_right_button = QPushButton("Rotate right")
        self.fit_button = QPushButton("Fit")
        toolbar = QHBoxLayout()
        for button in (
            self.previous_button,
            self.next_button,
            self.zoom_out_button,
            self.zoom_in_button,
            self.rotate_left_button,
            self.rotate_right_button,
            self.fit_button,
        ):
            toolbar.addWidget(button)

        self.canvas = ImageCanvas(self)
        self.message = QLabel("Loading image…")
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.activity = QProgressBar()
        self.activity.setRange(0, 0)
        self.activity.setTextVisible(False)
        self.state_page = QVBoxLayout()
        self.state_page.addStretch(1)
        self.state_page.addWidget(self.message)
        self.state_page.addWidget(self.activity)
        self.state_page.addStretch(1)
        state_widget = QWidget()
        state_widget.setLayout(self.state_page)
        self._state_widget = state_widget
        self.content = QStackedWidget()
        self.content.addWidget(self.canvas)
        self.content.addWidget(state_widget)
        self.footer = QLabel("Loading image…")

        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(self.content, 1)
        layout.addWidget(self.footer)
        self.resize(900, 650)

        self.previous_button.clicked.connect(lambda: self.move(-1))
        self.next_button.clicked.connect(lambda: self.move(1))
        self.zoom_in_button.clicked.connect(lambda: self.zoom(1.25))
        self.zoom_out_button.clicked.connect(lambda: self.zoom(1 / 1.25))
        self.rotate_left_button.clicked.connect(lambda: self.rotate(-90))
        self.rotate_right_button.clicked.connect(lambda: self.rotate(90))
        self.fit_button.clicked.connect(self.fit)
        self._update_controls()
        manager.state_changed.connect(self._connection_changed)

    def open_image_set(
        self,
        paths: list[str],
        current_path: str,
        is_phone: bool,
        serial: Optional[str] = None,
    ) -> None:
        self._paths = list(dict.fromkeys(paths))
        if current_path not in self._paths:
            self._paths.append(current_path)
        self._index = self._paths.index(current_path)
        self._is_phone = is_phone
        self._serial = serial
        self._update_controls()
        self._load_current()

    def move(self, amount: int) -> None:
        if not self._paths or (self._is_phone and not self._phone_available()):
            return
        target = self._index + amount
        if 0 <= target < len(self._paths):
            self._index = target
            self._load_current()

    def zoom(self, factor: float) -> None:
        if self._image is None:
            return
        new_zoom = min(20.0, max(0.05, self._zoom * factor))
        actual_factor = new_zoom / self._zoom
        self._zoom = new_zoom
        self._fit_mode = False
        self.canvas.scale(actual_factor, actual_factor)

    def rotate(self, degrees: int) -> None:
        if self._image is None or self.canvas._item is None:
            return
        self._rotation = (self._rotation + degrees) % 360
        self.canvas._item.setRotation(self._rotation)
        self.canvas.scene.setSceneRect(self.canvas._item.sceneBoundingRect())
        if self._fit_mode:
            self.canvas.fit_image()

    def fit(self) -> None:
        if self._image is None:
            return
        self._fit_mode = True
        self._zoom = 1.0
        self.canvas.resetTransform()
        self.canvas.fit_image()

    def _load_current(self) -> None:
        if self._is_phone and not self._phone_available():
            self._show_state(ViewerState.PHONE_DISCONNECTED, "Phone disconnected")
            return
        self._generation += 1
        generation = self._generation
        if self._cancel is not None:
            self._cancel.set()
        cancel = threading.Event()
        self._cancel = cancel
        self._image = None
        self._show_state(ViewerState.LOADING, f"Loading image: {Path(self._paths[self._index]).name}")
        task = FunctionTask(
            _load_image,
            self._paths[self._index],
            self._is_phone,
            self._serial,
            cancel,
        )
        self._task = task
        task.signals.completed.connect(
            lambda result, error, task=task, generation=generation:
                self._load_finished(task, generation, result, error)
        )
        QThreadPool.globalInstance().start(task)
        QTimer.singleShot(LOAD_TIMEOUT_MS, lambda generation=generation: self._load_timeout(generation))

    def _load_timeout(self, generation: int) -> None:
        if generation != self._generation or self.state != ViewerState.LOADING:
            return
        self._generation += 1
        if self._cancel is not None:
            self._cancel.set()
        self._task = None
        self._show_state(ViewerState.LOAD_ERROR, "Image loading timed out. Try opening it again.")

    def _load_finished(self, task, generation: int, result, error) -> None:
        if self._task is task:
            self._task = None
        if generation != self._generation:
            return
        if self._is_phone and not self._phone_available():
            self._show_state(ViewerState.PHONE_DISCONNECTED, "Phone disconnected")
            return
        if isinstance(error, UnsupportedImageError):
            self._show_state(ViewerState.UNSUPPORTED, f"Unsupported format — {error}")
            return
        if error is not None:
            self._show_state(ViewerState.CORRUPT, f"Corrupt or unreadable image — {error}")
            return
        self._image = result
        self._rotation = 0
        self._zoom = 1.0
        self._fit_mode = True
        self.canvas.set_image(self._image)
        self.content.setCurrentWidget(self.canvas)
        message = f"Loaded: {Path(self._paths[self._index]).name}"
        self.state = ViewerState.LOADED
        self.message.setText(message)
        self.footer.setText(message)
        self._update_controls()
        QTimer.singleShot(0, self.canvas.fit_image)

    def _show_state(self, state: ViewerState, message: str) -> None:
        self.state = state
        self.message.setText(message)
        self.footer.setText(message)
        self.activity.setVisible(state == ViewerState.LOADING)
        self.content.setCurrentWidget(self._state_widget)
        self._update_controls()

    def _phone_available(self) -> bool:
        session = self._manager.session
        return session is not None and session.device.serial == self._serial

    def _connection_changed(self, state: object, session: object) -> None:
        if not self._is_phone:
            return
        if (
            state != ConnectionState.USB
            or not isinstance(session, PhoneSession)
            or session.device.serial != self._serial
        ):
            self._generation += 1
            if self._cancel is not None:
                self._cancel.set()
            self._task = None
            self._image = None
            self._show_state(ViewerState.PHONE_DISCONNECTED, "Phone disconnected")
        else:
            self._update_controls()

    def _update_controls(self) -> None:
        available = not self._is_phone or self._phone_available()
        has_image = self._image is not None and self.state == ViewerState.LOADED
        self.previous_button.setEnabled(available and self._index > 0)
        self.next_button.setEnabled(available and self._index + 1 < len(self._paths))
        for button in (
            self.zoom_out_button,
            self.zoom_in_button,
            self.rotate_left_button,
            self.rotate_right_button,
            self.fit_button,
        ):
            button.setEnabled(has_image)

    def closeEvent(self, event) -> None:
        self._generation += 1
        if self._cancel is not None:
            self._cancel.set()
        self._cancel = None
        self._task = None
        self._image = None
        self.canvas.scene.clear()
        self.canvas._item = None
        super().closeEvent(event)
