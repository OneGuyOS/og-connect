"""User-scoped preferences for Phonebridge."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile


CONFLICT_BEHAVIORS = ("Ask", "Skip", "Overwrite", "Rename")


@dataclass(frozen=True)
class AppSettings:
    show_home_on_startup: bool = True
    default_conflict_behavior: str = "Ask"
    battery_refresh_seconds: int = 30


class SettingsStore:
    """Load and safely save the small, non-sensitive user preference file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".config" / "phonebridge" / "settings.json"
        self.settings = AppSettings()
        self.load()

    @staticmethod
    def _validated(data: object) -> AppSettings:
        if not isinstance(data, dict):
            return AppSettings()
        show_home = data.get("show_home_on_startup", True)
        if not isinstance(show_home, bool):
            show_home = True
        behavior = data.get("default_conflict_behavior", "Ask")
        if behavior not in CONFLICT_BEHAVIORS:
            behavior = "Ask"
        interval = data.get("battery_refresh_seconds", 30)
        if isinstance(interval, bool) or not isinstance(interval, int) or not 15 <= interval <= 300:
            interval = 30
        return AppSettings(show_home, behavior, interval)

    def load(self) -> AppSettings:
        missing = not self.path.exists()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raw = {}
        self.settings = self._validated(raw)
        if missing or raw == {}:
            self.save()
        return self.settings

    def save(self, settings: AppSettings | None = None) -> bool:
        if settings is not None:
            self.settings = self._validated({
                "show_home_on_startup": settings.show_home_on_startup,
                "default_conflict_behavior": settings.default_conflict_behavior,
                "battery_refresh_seconds": settings.battery_refresh_seconds,
            })
        payload = {
            "show_home_on_startup": self.settings.show_home_on_startup,
            "default_conflict_behavior": self.settings.default_conflict_behavior,
            "battery_refresh_seconds": self.settings.battery_refresh_seconds,
        }
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=".settings-", suffix=".tmp", delete=False,
            ) as temporary:
                json.dump(payload, temporary, indent=2)
                temporary.write("\n")
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)
            return True
        except OSError:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return False
