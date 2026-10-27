"""__main__.py - Entry point: python -m phonebridge."""

import sys

from PySide6.QtWidgets import QApplication

from phonebridge.device import DeviceManager
from phonebridge.window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    manager = DeviceManager()
    window = MainWindow(manager)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
