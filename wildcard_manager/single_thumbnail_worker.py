from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from .api import ThumbnailApiClient
from .models import AppSettings, WildcardEntry
from .repository import WildcardRepository


class SingleThumbnailWorker(QObject):
    finished = Signal(object, str)
    failed = Signal(str)

    def __init__(self, db_path: Path, settings: AppSettings, entry: WildcardEntry):
        super().__init__()
        self.db_path = db_path
        self.settings = settings
        self.entry = entry

    @Slot()
    def run(self) -> None:
        try:
            api = ThumbnailApiClient()
            path = api.generate_thumbnail(self.settings, self.entry)
            txt_path = Path(self.entry.abs_path)
            if txt_path.exists():
                repo = WildcardRepository(self.db_path)
                updated = repo.refresh_entry(self.settings, txt_path)
                self.finished.emit(updated, path.name)
            else:
                self.entry.thumbnail_path = str(path)
                self.finished.emit(self.entry, path.name)
        except Exception as exc:
            self.failed.emit(str(exc))
