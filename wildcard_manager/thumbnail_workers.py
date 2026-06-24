from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QRunnable, QSize, Signal, Slot
from PySide6.QtGui import QImage, QImageReader

# サムネイル生成側（SingleThumbnailWorker / shutil.copy2 等）と一覧表示側が
# 非同期で競合した場合（ファイルが書き込み中 / まだ存在しない）に備えたリトライ設定。
THUMBNAIL_READ_RETRY_COUNT = 3
THUMBNAIL_READ_RETRY_DELAY_SEC = 0.15


class _CancellableRunnable(QRunnable):
    """キャンセル可能な QRunnable のベースクラス（H9 修正）。

    QRunnable は標準でキャンセルAPIを持たないため、フラグベースの
    キャンセル機構を提供する。``run`` 内の重い処理前に ``is_cancelled``
    をチェックすることで早期リターンできる。
    """

    def __init__(self) -> None:
        super().__init__()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled


class ThumbnailLoadSignals(QObject):
    loaded = Signal(str, int, object)


class ThumbnailLoadTask(_CancellableRunnable):
    def __init__(self, abs_path: str, thumbnail_path: str, thumbnail_size: int, signals: ThumbnailLoadSignals):
        super().__init__()
        self.abs_path = abs_path
        self.thumbnail_path = thumbnail_path
        self.thumbnail_size = thumbnail_size
        self.signals = signals

    def _try_read_once(self) -> QImage | None:
        if self.is_cancelled:
            return None
        width = max(80, self.thumbnail_size)
        height = int(width * 1.5)
        # 書き込み中（0byte / 一部のみ書き込まれた状態）のファイルを弾く。
        try:
            if os.path.getsize(self.thumbnail_path) <= 0:
                return None
        except OSError:
            return None
        reader = QImageReader(self.thumbnail_path)
        reader.setAutoTransform(True)
        reader.setScaledSize(QSize(width, height))
        image = reader.read()
        if image.isNull():
            return None
        return image

    @Slot()
    def run(self) -> None:
        image: QImage | None = None
        for attempt in range(THUMBNAIL_READ_RETRY_COUNT):
            if self.is_cancelled:
                # キャンセル時は None をemit しない（プレースホルダーのまま）
                return
            image = self._try_read_once()
            if image is not None:
                break
            if attempt < THUMBNAIL_READ_RETRY_COUNT - 1:
                # サムネイル生成側の書き込み完了待ち。短い間隔で数回だけ再試行する。
                time.sleep(THUMBNAIL_READ_RETRY_DELAY_SEC)
        if self.is_cancelled:
            return
        self.signals.loaded.emit(self.abs_path, self.thumbnail_size, image)


class EntryContentLoadSignals(QObject):
    loaded = Signal(str, str)
    failed = Signal(str, str)


class EntryContentLoadTask(_CancellableRunnable):
    def __init__(self, abs_path: str, repo, signals: EntryContentLoadSignals):
        super().__init__()
        self.abs_path = abs_path
        self.repo = repo
        self.signals = signals

    @Slot()
    def run(self) -> None:
        if self.is_cancelled:
            return
        try:
            content = self.repo.load_entry_content(Path(self.abs_path))
            if self.is_cancelled:
                return
            self.signals.loaded.emit(self.abs_path, content)
        except Exception as exc:
            if self.is_cancelled:
                return
            self.signals.failed.emit(self.abs_path, str(exc))


class PreviewLoadSignals(QObject):
    loaded = Signal(str, str, int, int, int, object)
    failed = Signal(str, str, int, int, int)


class PreviewLoadTask(_CancellableRunnable):
    def __init__(self, abs_path: str, thumbnail_path: str, width: int, height: int, token: int, signals: PreviewLoadSignals):
        super().__init__()
        self.abs_path = abs_path
        self.thumbnail_path = thumbnail_path
        self.width = width
        self.height = height
        self.token = token
        self.signals = signals

    @Slot()
    def run(self) -> None:
        if self.is_cancelled:
            return
        try:
            reader = QImageReader(self.thumbnail_path)
            reader.setAutoTransform(True)
            source_size = reader.size()
            if source_size.isValid() and source_size.width() > 0 and source_size.height() > 0:
                scaled = source_size.scaled(QSize(self.width, self.height), Qt.KeepAspectRatio)
                if scaled.width() > 0 and scaled.height() > 0:
                    reader.setScaledSize(scaled)
            if self.is_cancelled:
                return
            image = reader.read()
            if self.is_cancelled:
                return
            if image.isNull():
                self.signals.failed.emit(self.abs_path, self.thumbnail_path, self.width, self.height, self.token)
                return
            self.signals.loaded.emit(self.abs_path, self.thumbnail_path, self.width, self.height, self.token, image)
        except Exception:
            if self.is_cancelled:
                return
            self.signals.failed.emit(self.abs_path, self.thumbnail_path, self.width, self.height, self.token)
