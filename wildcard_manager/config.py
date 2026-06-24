from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import AppSettings


class SettingsStore:
    def __init__(self, app_dir: Path):
        self.app_dir = app_dir
        self.path = app_dir / "config.json"

    def load(self) -> AppSettings:
        if not self.path.exists():
            settings = AppSettings.default(self.app_dir)
            self.save(settings)
            return settings

        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)

        defaults = asdict(AppSettings.default(self.app_dir))
        defaults.update({k: v for k, v in data.items() if k in defaults})
        settings = AppSettings(**defaults)
        return self._normalize_portable_paths(settings)

    def _normalize_portable_paths(self, settings: AppSettings) -> AppSettings:
        default_settings = AppSettings.default(self.app_dir)
        if not Path(settings.library_root).exists() and Path(default_settings.library_root).exists():
            settings.library_root = default_settings.library_root
        if not Path(settings.thumbnail_root).exists() and Path(default_settings.thumbnail_root).exists():
            settings.thumbnail_root = default_settings.thumbnail_root
        return settings

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            data = asdict(settings)
            for key in (
                "window_width",
                "window_height",
                "window_maximized",
                "splitter_sizes",
                "detail_splitter_sizes",
                "last_folder",
                "sort_mode",
                "thumbnail_size",
            ):
                data.pop(key, None)
            json.dump(data, handle, ensure_ascii=False, indent=2)


class UIStateStore:
    def __init__(self, app_dir: Path):
        self.app_dir = app_dir
        self.path = app_dir / "ui_state.json"

    def load_into(self, settings: AppSettings) -> AppSettings:
        if not self.path.exists():
            return settings
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        for key, value in data.items():
            if hasattr(settings, key):
                setattr(settings, key, value)
        return settings

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "window_width": settings.window_width,
            "window_height": settings.window_height,
            "window_maximized": settings.window_maximized,
            "splitter_sizes": settings.splitter_sizes,
            "detail_splitter_sizes": settings.detail_splitter_sizes,
            "last_folder": settings.last_folder,
            "sort_mode": settings.sort_mode,
            "sort_key": settings.sort_key,
            "sort_order": settings.sort_order,
            "thumbnail_size": settings.thumbnail_size,
        }
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
