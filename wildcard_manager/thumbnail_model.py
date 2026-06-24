from __future__ import annotations

import os
import time
from collections import OrderedDict

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QRectF, QSize, Qt, QThreadPool, QTimer, Slot
from PySide6.QtGui import QFont, QFontMetrics, QIcon, QImage, QPainter, QColor, QPainterPath, QPen, QPixmap, QTextLayout
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem, QStyledItemDelegate

from .models import WildcardEntry
from .thumbnail_workers import ThumbnailLoadSignals, ThumbnailLoadTask

CARD_FRAME_PADDING = 14
CARD_ICON_RATIO = 1.5


def _make_rounded_pixmap(pixmap: QPixmap, radius: int) -> QPixmap:
    """角丸クリッピングを適用した新しい QPixmap を返す。

    QPixmap ではなく QImage.Format_ARGB32_Premultiplied を描画先に使うことで
    アルファチャンネルを確実に確保し、角の透明化を正しく行う。
    （ユーザー作業分: 旧実装では QPixmap.fill(Qt.transparent) でも
    環境によってアルファが乗らないことがあったため QImage 経由に変更）
    """
    if pixmap.isNull():
        return pixmap
    img = QImage(pixmap.size(), QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(img.rect()), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pixmap)
    p.end()
    return QPixmap.fromImage(img)


CARD_TITLE_HEIGHT = 56


def render_placeholder_pixmap(thumbnail_size: int) -> QPixmap:
    width = max(80, thumbnail_size)
    height = int(width * 1.5)
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#111317"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#232a31"))
    painter.drawRoundedRect(4, 4, width - 8, height - 8, 14, 14)
    painter.setPen(QPen(QColor("#75808a")))
    font = painter.font()
    font.setPointSize(max(8, min(14, width // 10)))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "NO\nTHUMB")
    painter.end()
    return pixmap


def wrap_text_lines(text: str, metrics: QFontMetrics, width: int, max_lines: int = 2) -> list[str]:
    compact = " ".join(text.split())
    if not compact:
        return [""]
    if width <= 8:
        return [metrics.elidedText(compact, Qt.ElideRight, 8)]

    layout = QTextLayout(compact, metrics.font())
    layout.beginLayout()

    lines: list[str] = []
    while len(lines) < max_lines:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        start = line.textStart()
        length = line.textLength()
        line_text = compact[start:start + length].rstrip()
        if len(lines) == max_lines - 1 and start + length < len(compact):
            line_text = metrics.elidedText(line_text, Qt.ElideRight, width)
        lines.append(line_text)
    layout.endLayout()

    if not lines:
        return [metrics.elidedText(compact, Qt.ElideRight, width)]
    return lines


class ThumbnailListModel(QAbstractListModel):
    # 読み込み失敗後、自動で再試行するまでの最短間隔（連続リペイントでの
    # 再試行スパムを防ぐ）。F5（set_entries の明示的呼び出し）はこの
    # クールダウンを無視して必ず再評価する。
    FAILED_RETRY_COOLDOWN_SEC = 1.5
    MAX_AUTO_RETRIES = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: list[WildcardEntry] = []
        self.thumbnail_size = 260
        # icon_cache には「読み込みに成功したサムネイル」だけを保持する。
        # 失敗結果はここに入れない（失敗の永久キャッシュを防ぐ）。
        self.icon_cache: OrderedDict[tuple[str, int], QIcon] = OrderedDict()
        self.placeholder_cache: dict[int, QIcon] = {}
        self.max_icon_cache = 720
        self.pending_loads: set[tuple[str, int]] = set()
        # 直近の読み込みが失敗したキーの集合と、最後に試行した時刻。
        self.failed_keys: set[tuple[str, int]] = set()
        self._last_attempt: dict[tuple[str, int], float] = {}
        # 画面の再描画を待たずに自動復旧させるためのバックグラウンド再試行回数。
        self._auto_retry_counts: dict[tuple[str, int], int] = {}
        self.row_by_path: dict[str, int] = {}
        # L8 修正: グローバル QThreadPool を直接 setMaxThreadCount すると
        # 他コンポーネント（PreviewLoadTask 等）にも影響する。
        # 専用の QThreadPool インスタンスを持つことで分離する。
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(min(8, os.cpu_count() or 8))
        self.loader_signals = ThumbnailLoadSignals(self)
        self.loader_signals.loaded.connect(self._handle_loaded_thumbnail)

    def set_entries(self, entries: list[WildcardEntry]) -> None:
        if len(self.entries) == len(entries) and all(
            self.entries[i].abs_path == entries[i].abs_path for i in range(len(entries))
        ):
            changed_rows: list[int] = []
            for i, entry in enumerate(entries):
                old = self.entries[i]
                fields_changed = (old.thumbnail_path != entry.thumbnail_path
                        or old.first_line != entry.first_line
                        or old.has_thumbnail != entry.has_thumbnail
                        or old.stem != entry.stem)
                # entry自体の値が変わっていなくても、直前の読み込みが失敗していた
                # 場合はF5（=set_entriesの再呼び出し）で必ず再評価する。
                # これがないと、サムネイル生成が一覧表示より遅れて完了した際に
                # 失敗状態のまま二度とリトライされなくなる（F5を押しても直らない）。
                previously_failed = self._has_failed_thumbnail(entry.abs_path)
                if fields_changed or previously_failed:
                    self.clear_cache_for_path(entry.abs_path)
                    self.entries[i] = entry
                    changed_rows.append(i)
            if changed_rows:
                first = self.index(changed_rows[0], 0)
                last = self.index(changed_rows[-1], 0)
                self.dataChanged.emit(first, last, [Qt.DecorationRole, Qt.DisplayRole, Qt.SizeHintRole])
            return
        self.beginResetModel()
        self.entries = list(entries)
        self.row_by_path = {entry.abs_path: index for index, entry in enumerate(self.entries)}
        self.endResetModel()

    def _has_failed_thumbnail(self, abs_path: str) -> bool:
        return any(key[0] == abs_path for key in self.failed_keys)

    def set_thumbnail_size(self, size: int) -> None:
        if self.thumbnail_size == size:
            return
        self.thumbnail_size = size
        self.icon_cache.clear()
        self.placeholder_cache.clear()
        self.pending_loads.clear()
        self.failed_keys.clear()
        self._last_attempt.clear()
        self._auto_retry_counts.clear()
        if self.entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.entries) - 1, 0), [Qt.DecorationRole, Qt.SizeHintRole])

    def clear_cache_for_path(self, abs_path: str) -> None:
        self.icon_cache = OrderedDict((key, value) for key, value in self.icon_cache.items() if key[0] != abs_path)
        self.pending_loads = {key for key in self.pending_loads if key[0] != abs_path}
        self.failed_keys = {key for key in self.failed_keys if key[0] != abs_path}
        self._last_attempt = {key: ts for key, ts in self._last_attempt.items() if key[0] != abs_path}
        self._auto_retry_counts = {key: c for key, c in self._auto_retry_counts.items() if key[0] != abs_path}

    def replace_entry(self, updated: WildcardEntry) -> None:
        row = self.row_by_path.get(updated.abs_path)
        if row is None or row >= len(self.entries):
            return
        self.entries[row] = updated
        self.row_by_path[updated.abs_path] = row
        # キャッシュをクリアして再ロードさせる（beginResetModelは選択を解除するので避ける）
        self.clear_cache_for_path(updated.abs_path)
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, [Qt.DecorationRole, Qt.DisplayRole, Qt.SizeHintRole])

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        entry = self.entries[index.row()]
        if role == Qt.DisplayRole:
            return entry.stem
        if role == Qt.DecorationRole:
            return self._get_icon(entry)
        if role == Qt.SizeHintRole:
            width = max(80, self.thumbnail_size) + CARD_FRAME_PADDING
            height = int(max(80, self.thumbnail_size) * CARD_ICON_RATIO) + CARD_TITLE_HEIGHT + CARD_FRAME_PADDING
            return QSize(width, height)
        if role == Qt.UserRole:
            return entry.abs_path
        return None

    def entry_at(self, index: QModelIndex) -> WildcardEntry | None:
        if not index.isValid():
            return None
        row = index.row()
        if not (0 <= row < len(self.entries)):
            return None
        return self.entries[row]

    def _get_icon(self, entry: WildcardEntry) -> QIcon:
        key = (entry.abs_path, self.thumbnail_size)
        cached = self.icon_cache.get(key)
        if cached is not None:
            self.icon_cache.move_to_end(key)
            return cached
        self._queue_thumbnail_load(entry, priority=0)
        return self._get_placeholder_icon()

    def _queue_thumbnail_load(self, entry: WildcardEntry, priority: int = 0, force: bool = False) -> None:
        key = (entry.abs_path, self.thumbnail_size)
        if key in self.icon_cache or key in self.pending_loads:
            return
        if not entry.thumbnail_path or not entry.has_thumbnail:
            return
        if not force and key in self.failed_keys:
            last = self._last_attempt.get(key, 0.0)
            if (time.monotonic() - last) < self.FAILED_RETRY_COOLDOWN_SEC:
                # 直近で失敗したばかり。リペイントの度に再試行してディスクを
                # 叩き続けないよう、クールダウン中は静かにプレースホルダーを返す。
                return
        self.pending_loads.add(key)
        self._last_attempt[key] = time.monotonic()
        self.thread_pool.start(
            ThumbnailLoadTask(entry.abs_path, entry.thumbnail_path, self.thumbnail_size, self.loader_signals),
            priority,
        )

    def refresh_thumbnails(self) -> None:
        """F5（再読み込み）から呼び出し、失敗していたサムネイルを
        クールダウンを無視して強制的に再評価する。"""
        if not self.entries:
            return
        targets = [entry for entry in self.entries if self._has_failed_thumbnail(entry.abs_path)]
        if not targets:
            return
        for entry in targets:
            self.clear_cache_for_path(entry.abs_path)
            self._queue_thumbnail_load(entry, priority=0, force=True)
        rows = sorted(self.row_by_path[e.abs_path] for e in targets if e.abs_path in self.row_by_path)
        if rows:
            self.dataChanged.emit(self.index(rows[0], 0), self.index(rows[-1], 0), [Qt.DecorationRole])

    def preload_rows(self, start: int, end: int, priority: int = 0) -> None:
        if not self.entries:
            return
        first = max(0, min(start, end))
        last = min(len(self.entries) - 1, max(start, end))
        for row in range(first, last + 1):
            self._queue_thumbnail_load(self.entries[row], priority)

    def _schedule_auto_retry(self, key: tuple[str, int]) -> None:
        """ユーザー操作（F5など）なしでも一定回数までバックグラウンドで自動的に
        再試行する。一時的な読み込み失敗からの自動復旧用。"""
        count = self._auto_retry_counts.get(key, 0)
        if count >= self.MAX_AUTO_RETRIES:
            return
        self._auto_retry_counts[key] = count + 1
        delay_ms = int(self.FAILED_RETRY_COOLDOWN_SEC * 1000)

        def _retry():
            row = self.row_by_path.get(key[0])
            if row is None or not (0 <= row < len(self.entries)):
                return
            entry = self.entries[row]
            if (entry.abs_path, self.thumbnail_size) != key:
                return  # サムネイルサイズが変わった等、状況が変化していたら何もしない
            if key not in self.failed_keys:
                return  # その間に別経路で成功済みなら何もしない
            self._queue_thumbnail_load(entry, priority=0, force=True)

        QTimer.singleShot(delay_ms, _retry)

    @Slot(str, int, object)
    def _handle_loaded_thumbnail(self, abs_path: str, thumbnail_size: int, image: QImage | None) -> None:
        key = (abs_path, thumbnail_size)
        self.pending_loads.discard(key)
        if image is None:
            # 失敗結果は icon_cache に入れない。failed_keys にだけ記録し、
            # 表示自体はプレースホルダー（_get_placeholder_icon）に任せる。
            # こうすることで、次にこの行が再評価された時（リペイント／F5／
            # set_entriesの再呼び出し）に必ずもう一度読み込みを試せる。
            self.failed_keys.add(key)
            self._schedule_auto_retry(key)
        else:
            self.failed_keys.discard(key)
            self._last_attempt.pop(key, None)
            self._auto_retry_counts.pop(key, None)
            icon = QIcon(QPixmap.fromImage(image))
            self.icon_cache[key] = icon
            self.icon_cache.move_to_end(key)
            while len(self.icon_cache) > self.max_icon_cache:
                self.icon_cache.popitem(last=False)
        row = self.row_by_path.get(abs_path)
        if row is not None and 0 <= row < len(self.entries):
            index = self.index(row, 0)
            self.dataChanged.emit(index, index, [Qt.DecorationRole])

    def _get_placeholder_icon(self) -> QIcon:
        cached = self.placeholder_cache.get(self.thumbnail_size)
        if cached is not None:
            return cached
        icon = QIcon(render_placeholder_pixmap(self.thumbnail_size))
        self.placeholder_cache[self.thumbnail_size] = icon
        return icon


class ThumbnailItemDelegate(QStyledItemDelegate):
    """カード表示用の旧デリゲート。

    M3 修正で「未使用」として削除していたが、ユーザーが角丸対応の
    スケーリングロジックを加えた上で残置したいため復元。
    現状 MainWindow は ThumbnailCardDelegate のみ使用しているが、
    将来的な切り替え備えて保持する。
    """
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        rect = option.rect.adjusted(4, 4, -4, -4)
        thumb_width = max(80, int(index.model().thumbnail_size))
        thumb_height = int(thumb_width * 1.28)
        icon_rect = QRect(
            rect.center().x() - thumb_width // 2,
            rect.top(),
            thumb_width,
            thumb_height,
        )
        icon = index.data(Qt.DecorationRole)
        if isinstance(icon, QIcon):
            # ユーザー作業分: pixmap を icon_rect に合わせてスケール + 角丸化してから描画
            pix = icon.pixmap(icon_rect.size())
            if not pix.isNull():
                scaled = pix.scaled(icon_rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                rounded = _make_rounded_pixmap(scaled, 10)
                dx = icon_rect.x() + (icon_rect.width() - rounded.width()) // 2
                dy = icon_rect.y() + (icon_rect.height() - rounded.height()) // 2
                painter.drawPixmap(dx, dy, rounded)

        text_rect = QRect(rect.left() + 6, icon_rect.bottom() + 10, rect.width() - 12, rect.bottom() - icon_rect.bottom() - 12)
        metrics = QFontMetrics(option.font)
        side_padding = metrics.averageCharWidth()
        lines = wrap_text_lines(str(index.data(Qt.DisplayRole) or ""), metrics, max(20, text_rect.width() - side_padding * 2), 2)
        painter.setPen(option.palette.color(option.palette.Text))
        line_height = metrics.lineSpacing()
        total_height = len(lines) * line_height
        top = text_rect.top() + max(0, (text_rect.height() - total_height) // 2)
        for line in lines:
            painter.drawText(
                QRect(text_rect.left() + side_padding, top, max(20, text_rect.width() - side_padding * 2), line_height),
                Qt.AlignHCenter | Qt.AlignVCenter,
                line,
            )
            top += line_height

        if option.state & QStyle.State_Selected:
            painter.setPen(QPen(QColor("white"), 4))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, 12, 12)
        painter.restore()


class ThumbnailCardDelegate(QStyledItemDelegate):
    def _card_metrics(self, index: QModelIndex) -> tuple[int, int, int]:
        thumb_width = max(80, int(getattr(index.model(), "thumbnail_size", 260)))
        thumb_height = int(thumb_width * CARD_ICON_RATIO)
        return thumb_width, thumb_height, CARD_TITLE_HEIGHT

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        thumb_width, thumb_height, title_height = self._card_metrics(index)
        return QSize(thumb_width + CARD_FRAME_PADDING, thumb_height + title_height + CARD_FRAME_PADDING)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        rect = option.rect.adjusted(4, 4, -4, -4)
        thumb_width, thumb_height, title_height = self._card_metrics(index)
        is_selected = bool(option.state & QStyle.State_Selected)
        is_hovered = bool(option.state & QStyle.State_MouseOver)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#2a3a50") if is_hovered else QColor("#171b21"))
        painter.drawRoundedRect(rect, 14, 14)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.state = opt.state & ~QStyle.State_Selected & ~QStyle.State_MouseOver
        opt.rect = rect.adjusted(12, 12, -12, -12)
        opt.decorationPosition = QStyleOptionViewItem.Top
        opt.decorationAlignment = Qt.AlignHCenter
        opt.displayAlignment = Qt.AlignHCenter | Qt.AlignTop
        opt.textElideMode = Qt.ElideRight
        opt.features |= QStyleOptionViewItem.WrapText
        opt.decorationSize = QSize(thumb_width, thumb_height)
        style = option.widget.style() if option.widget is not None else QApplication.style()
        # ユーザー作業分: style.drawControl に渡す前にアイコンへ角丸を適用する。
        # drawControl 呼び出し後に painter.setClipPath() しても style 内部の
        # save()/restore() で clip が巻き戻されるため、ここで pixmap を加工する。
        if not opt.icon.isNull():
            pix = opt.icon.pixmap(opt.decorationSize)
            if not pix.isNull():
                opt.icon = QIcon(_make_rounded_pixmap(pix, 10))
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, option.widget)

        if is_selected:
            painter.setPen(QPen(QColor("white"), 4))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, 12, 12)
        painter.restore()
