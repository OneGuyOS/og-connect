"""Launch Phonebridge."""

import sys

from PySide6.QtWidgets import QApplication

from .device import DeviceManager
from .window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    manager = DeviceManager()
    window = MainWindow(manager)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
