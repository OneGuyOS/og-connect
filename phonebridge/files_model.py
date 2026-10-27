"""Rows and parsing helpers for Android `ls -la` output."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt


@dataclass(frozen=True)
class PhoneFile:
    name: str
    is_dir: bool
    size: int
    modified: str


def parse_ls_output(output: str) -> list[PhoneFile]:
    """Parse common Android and GNU long-listing formats, skipping bad lines."""
    entries: list[PhoneFile] = []
    for line in output.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or not fields[0] or fields[0][0] not in "-dlbcps":
            continue
        try:
            size = int(fields[4])
        except ValueError:
            continue

        timestamp_and_name = fields[5]
        android_match = re.match(
            r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)\s+(.+)$",
            timestamp_and_name,
        )
        if android_match:
            modified = f"{android_match.group(1)} {android_match.group(2)}"
            name = android_match.group(3)
        else:
            unix_match = re.match(
                r"^([A-Z][a-z]{2}\s+\d{1,2}\s+(?:\d{2}:\d{2}(?::\d{2})?|\d{4}))\s+(.+)$",
                timestamp_and_name,
            )
            if not unix_match:
                continue
            modified, name = unix_match.groups()
        if not name or name in (".", ".."):
            continue
        entries.append(
            PhoneFile(name=name, is_dir=fields[0][0] == "d", size=size, modified=modified)
        )
    return entries


class PhoneFileModel(QAbstractTableModel):
    HEADERS = ("Name", "Size", "Modified")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._entries: list[PhoneFile] = []
        self._sort_column = 0
        self._descending = False

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._entries)):
            return None
        entry = self._entries[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return entry
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if index.column() == 0:
            return entry.name
        if index.column() == 1:
            return "" if entry.is_dir else str(entry.size)
        if index.column() == 2:
            return entry.modified
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return None

    def entry_at(self, row: int) -> Optional[PhoneFile]:
        return self._entries[row] if 0 <= row < len(self._entries) else None

    def set_entries(self, entries: list[PhoneFile]) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self._sort_entries()
        self.endResetModel()

    def sort_by(self, column: int, descending: bool = False) -> None:
        if column not in (0, 1, 2):
            return
        self.beginResetModel()
        self._sort_column = column
        self._descending = descending
        self._sort_entries()
        self.endResetModel()

    def _sort_entries(self) -> None:
        def key(entry: PhoneFile):
            if self._sort_column == 1:
                value = entry.size
            elif self._sort_column == 2:
                value = entry.modified.casefold()
            else:
                value = entry.name.casefold()
            return (not entry.is_dir, value)

        self._entries.sort(key=key, reverse=self._descending)
        # Descending sort should still keep directories above files.
        if self._descending:
            directories = [entry for entry in self._entries if entry.is_dir]
            files = [entry for entry in self._entries if not entry.is_dir]
            self._entries = directories + files
