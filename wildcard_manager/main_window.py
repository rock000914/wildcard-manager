from __future__ import annotations

import json
import re
import subprocess
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QPoint, QRect, QRunnable, QSignalBlocker, QSize, Qt, QThread, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QClipboard, QCloseEvent, QColor, QFont, QFontMetrics, QIcon, QImage, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSpinBox,
    QStatusBar,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .api import ThumbnailApiClient
from .config import SettingsStore, UIStateStore
from .models import AppSettings, WildcardEntry
from .repository import CONFLICT_POLICIES, OperationCancelledError, WildcardRepository, strip_lora_tags


SORT_LABELS = {
    "name_asc": "名前順",
    "name_desc": "名前逆順",
    "path_asc": "パス順",
    "folder_asc": "フォルダ順",
}

THUMBNAIL_SIZE_MIN = 80
THUMBNAIL_SIZE_MAX = 720
THUMBNAIL_SIZE_STEP = 16
CARD_ICON_RATIO = 1.20
# Title area for the default Qt item renderer.
CARD_TITLE_HEIGHT = 56
CARD_FRAME_PADDING = 14
UI_FONT_FAMILIES = ["Yu Gothic UI", "Yu Gothic", "Meiryo UI", "Meiryo", "Segoe UI", "Noto Sans CJK JP", "MS Gothic"]


def elide(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


_NATURAL_SPLIT = re.compile(r"(\d+)")


def natural_sort_key(text: str) -> tuple[object, ...]:
    parts = _NATURAL_SPLIT.split(text.lower())
    key: list[object] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def render_placeholder_pixmap(thumbnail_size: int) -> QPixmap:
    width = max(80, thumbnail_size)
    height = int(width * 1.28)
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#111317"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#232a31"))
    painter.drawRoundedRect(4, 4, width - 8, height - 8, 14, 14)
    painter.setPen(QPen(QColor("#75808a")))
    font = QFont()
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

    lines: list[str] = []
    rest = compact
    for line_index in range(max_lines):
        if not rest:
            break
        if line_index == max_lines - 1:
            lines.append(metrics.elidedText(rest, Qt.ElideRight, width))
            break

        cut = len(rest)
        for idx in range(1, len(rest) + 1):
            if metrics.horizontalAdvance(rest[:idx]) > width:
                cut = max(1, idx - 1)
                break
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()

    return lines or [metrics.elidedText(compact, Qt.ElideRight, width)]


def parse_prompt_tags(text: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for chunk in text.replace("\n", ",").split(","):
        clean = chunk.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        tags.append(clean)
    return tags


class ThumbnailLoadSignals(QObject):
    loaded = Signal(str, int, object)


class ThumbnailLoadTask(QRunnable):
    def __init__(self, abs_path: str, thumbnail_path: str, thumbnail_size: int, signals: ThumbnailLoadSignals):
        super().__init__()
        self.abs_path = abs_path
        self.thumbnail_path = thumbnail_path
        self.thumbnail_size = thumbnail_size
        self.signals = signals

    @Slot()
    def run(self) -> None:
        width = max(80, self.thumbnail_size)
        height = int(width * 1.28)
        reader = QImageReader(self.thumbnail_path)
        reader.setAutoTransform(True)
        reader.setScaledSize(QSize(width, height))
        image = reader.read()
        if image.isNull():
            image = None
        self.signals.loaded.emit(self.abs_path, self.thumbnail_size, image)


class EntryContentLoadSignals(QObject):
    loaded = Signal(str, str)
    failed = Signal(str, str)


class EntryContentLoadTask(QRunnable):
    def __init__(self, abs_path: str, repo: WildcardRepository, signals: EntryContentLoadSignals):
        super().__init__()
        self.abs_path = abs_path
        self.repo = repo
        self.signals = signals

    @Slot()
    def run(self) -> None:
        try:
            content = self.repo.load_entry_content(Path(self.abs_path))
            self.signals.loaded.emit(self.abs_path, content)
        except Exception as exc:
            self.signals.failed.emit(self.abs_path, str(exc))


class PreviewLoadSignals(QObject):
    loaded = Signal(str, str, int, int, int, object)
    failed = Signal(str, str, int, int, int)


class PreviewLoadTask(QRunnable):
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
        try:
            reader = QImageReader(self.thumbnail_path)
            reader.setAutoTransform(True)
            source_size = reader.size()
            if source_size.isValid() and source_size.width() > 0 and source_size.height() > 0:
                scaled = source_size.scaled(QSize(self.width, self.height), Qt.KeepAspectRatio)
                if scaled.width() > 0 and scaled.height() > 0:
                    reader.setScaledSize(scaled)
            image = reader.read()
            if image.isNull():
                self.signals.failed.emit(self.abs_path, self.thumbnail_path, self.width, self.height, self.token)
                return
            self.signals.loaded.emit(self.abs_path, self.thumbnail_path, self.width, self.height, self.token, image)
        except Exception:
            self.signals.failed.emit(self.abs_path, self.thumbnail_path, self.width, self.height, self.token)


class ThumbnailListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: list[WildcardEntry] = []
        self.thumbnail_size = 260
        self.icon_cache: OrderedDict[tuple[str, int], QIcon] = OrderedDict()
        self.placeholder_cache: dict[int, QIcon] = {}
        self.max_icon_cache = 720
        self.pending_loads: set[tuple[str, int]] = set()
        self.row_by_path: dict[str, int] = {}
        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(max(8, self.thread_pool.maxThreadCount()))
        self.loader_signals = ThumbnailLoadSignals(self)
        self.loader_signals.loaded.connect(self._handle_loaded_thumbnail)

    def set_entries(self, entries: list[WildcardEntry]) -> None:
        self.beginResetModel()
        self.entries = list(entries)
        self.row_by_path = {entry.abs_path: index for index, entry in enumerate(self.entries)}
        self.endResetModel()

    def set_thumbnail_size(self, size: int) -> None:
        if self.thumbnail_size == size:
            return
        self.thumbnail_size = size
        self.icon_cache.clear()
        self.placeholder_cache.clear()
        self.pending_loads.clear()
        if self.entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.entries) - 1, 0), [Qt.DecorationRole, Qt.SizeHintRole])

    def clear_cache_for_path(self, abs_path: str) -> None:
        self.icon_cache = OrderedDict((key, value) for key, value in self.icon_cache.items() if key[0] != abs_path)

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

    def _queue_thumbnail_load(self, entry: WildcardEntry, priority: int = 0) -> None:
        key = (entry.abs_path, self.thumbnail_size)
        if key in self.icon_cache or key in self.pending_loads:
            return
        if not entry.thumbnail_path or not Path(entry.thumbnail_path).exists():
            return
        self.pending_loads.add(key)
        self.thread_pool.start(
            ThumbnailLoadTask(entry.abs_path, entry.thumbnail_path, self.thumbnail_size, self.loader_signals),
            priority,
        )

    def preload_rows(self, start: int, end: int, priority: int = 0) -> None:
        if not self.entries:
            return
        first = max(0, min(start, end))
        last = min(len(self.entries) - 1, max(start, end))
        for row in range(first, last + 1):
            self._queue_thumbnail_load(self.entries[row], priority)

    @Slot(str, int, object)
    def _handle_loaded_thumbnail(self, abs_path: str, thumbnail_size: int, image: QImage | None) -> None:
        key = (abs_path, thumbnail_size)
        self.pending_loads.discard(key)
        if image is None:
            icon = self._get_placeholder_icon()
        else:
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
            icon.paint(painter, icon_rect, Qt.AlignCenter)

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


class TagChip(QWidget):
    removed = Signal(str)

    def __init__(self, tag: str, editable: bool = False, parent=None):
        super().__init__(parent)
        self.tag = tag
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(5)
        layout.setSizeConstraint(QLayout.SetFixedSize)
        self.label = QLabel(tag)
        self.label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.label.setWordWrap(False)
        self.close_button = QPushButton("x")
        self.close_button.setFixedSize(16, 16)
        self.close_button.clicked.connect(lambda: self.removed.emit(self.tag))
        layout.addWidget(self.label)
        layout.addWidget(self.close_button)
        self.setStyleSheet(
            """
            TagChip {
                background: #2b5078;
                border: 1px solid #4f7ea8;
                border-radius: 5px;
            }
            TagChip QLabel {
                color: #f2f7fc;
                font-size: 12px;
            }
            TagChip QPushButton {
                background: transparent;
                border: none;
                color: #dceaf7;
                font-weight: bold;
                padding: 0;
                margin: 0;
            }
            TagChip QPushButton:hover {
                color: white;
            }
            """
        )
        self.set_editable(editable)

    def set_tag(self, tag: str) -> None:
        self.tag = tag
        self.label.setText(tag)
        self.updateGeometry()

    def set_editable(self, editable: bool) -> None:
        self.close_button.setVisible(editable)
        self.close_button.setEnabled(editable)
        self.close_button.setCursor(Qt.PointingHandCursor if editable else Qt.ArrowCursor)
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.label.font())
        text_width = metrics.horizontalAdvance(self.tag)
        text_height = metrics.height()
        button_width = self.close_button.width() if self.close_button.isVisible() else 0
        button_gap = 6 if self.close_button.isVisible() else 0
        left_right_padding = 16
        top_bottom_padding = 8
        width = text_width + button_width + button_gap + left_right_padding
        height = max(text_height, self.close_button.height() if self.close_button.isVisible() else 0) + top_bottom_padding
        return QSize(width, height)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


class TagEditorWidget(QWidget):
    tagsChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._editable = False
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.help_label = QLabel("タグ一覧（ダブルクリックで編集 / ×で削除）")
        self.help_label.setStyleSheet("color: #9fb2c7; padding: 0 2px 4px 2px;")
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListView.IconMode)
        self.list_widget.setFlow(QListView.LeftToRight)
        self.list_widget.setWrapping(True)
        self.list_widget.setResizeMode(QListView.Adjust)
        self.list_widget.setMovement(QListView.Static)
        self.list_widget.setSpacing(2)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list_widget.itemDoubleClicked.connect(self._edit_item)
        tags_height = self._tags_view_height(4)
        self.list_widget.setFixedHeight(tags_height)
        self.list_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.list_widget.setStyleSheet(
            "QListWidget {"
            " background: #121a27;"
            " border: 1px solid #2f5274;"
            " border-radius: 8px;"
            " padding: 4px;"
            " color: #d7e1eb;"
            "}"
        )
        self.empty_label = QLabel("タグなし")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("color: #708090; background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.help_label)
        layout.addWidget(self.list_widget)
        self._refresh_empty_state()
        self.set_editable(False)

    def _tags_view_height(self, rows: int) -> int:
        metrics = QFontMetrics(self.list_widget.font())
        chip_height = max(22, metrics.height() + 10)
        return rows * chip_height + max(0, rows - 1) * self.list_widget.spacing() + 8

    def sizeHint(self) -> QSize:
        help_height = self.help_label.sizeHint().height()
        return QSize(320, help_height + self._tags_view_height(4) + 2)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def set_tags(self, tags: list[str]) -> None:
        self.list_widget.clear()
        for tag in tags:
            self._append_tag(tag)
        self._refresh_empty_state()
        self.tagsChanged.emit()

    def tags(self) -> list[str]:
        values: list[str] = []
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            tag = str(item.data(Qt.UserRole) or "").strip()
            if tag:
                values.append(tag)
        return values

    def set_editable(self, editable: bool) -> None:
        self._editable = editable
        for row in range(self.list_widget.count()):
            widget = self.list_widget.itemWidget(self.list_widget.item(row))
            if isinstance(widget, TagChip):
                widget.set_editable(editable)

    def _append_tag(self, tag: str) -> None:
        clean = tag.strip()
        if not clean:
            return
        if clean in self.tags():
            return
        item = QListWidgetItem()
        item.setData(Qt.UserRole, clean)
        chip = TagChip(clean, self._editable)
        chip.removed.connect(self._remove_tag)
        item.setSizeHint(chip.sizeHint())
        self.list_widget.addItem(item)
        self.list_widget.setItemWidget(item, chip)
        self._refresh_empty_state()
        self.tagsChanged.emit()

    def _remove_tag(self, tag: str) -> None:
        if not self._editable:
            parent = self.window()
            if hasattr(parent, "_set_edit_mode"):
                parent._set_edit_mode(True)
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if str(item.data(Qt.UserRole)) == tag:
                self.list_widget.takeItem(row)
                self._refresh_empty_state()
                self.tagsChanged.emit()
                return

    def _edit_item(self, item: QListWidgetItem) -> None:
        if not self._editable:
            parent = self.window()
            if hasattr(parent, "_set_edit_mode"):
                parent._set_edit_mode(True)
            else:
                self.set_editable(True)
        current = str(item.data(Qt.UserRole) or "")
        text, ok = QInputDialog.getText(self, "タグ編集", "タグ名", text=current)
        if not ok:
            return
        updated = text.strip()
        if not updated:
            self._remove_tag(current)
            return
        item.setData(Qt.UserRole, updated)
        widget = self.list_widget.itemWidget(item)
        if isinstance(widget, TagChip):
            widget.set_tag(updated)
            widget.set_editable(self._editable)
        item.setSizeHint(widget.sizeHint() if widget is not None else item.sizeHint())
        self.tagsChanged.emit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_empty_state()

    def _refresh_empty_state(self) -> None:
        if self.list_widget.count() == 0:
            self.empty_label.setParent(self.list_widget.viewport())
            self.empty_label.setGeometry(self.list_widget.viewport().rect())
            self.empty_label.show()
        else:
            self.empty_label.hide()


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
            repo = WildcardRepository(self.db_path)
            path = api.generate_thumbnail(self.settings, self.entry)
            updated = repo.refresh_entry(self.settings, Path(self.entry.abs_path))
            self.finished.emit(updated, path.name)
        except Exception as exc:
            self.failed.emit(str(exc))


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

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#171b21"))
        painter.drawRoundedRect(rect, 14, 14)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.rect = rect.adjusted(8, 8, -8, -8)
        opt.decorationPosition = QStyleOptionViewItem.Top
        opt.decorationAlignment = Qt.AlignHCenter
        opt.displayAlignment = Qt.AlignHCenter | Qt.AlignTop
        opt.textElideMode = Qt.ElideRight
        opt.features |= QStyleOptionViewItem.WrapText
        opt.decorationSize = QSize(thumb_width, thumb_height)
        style = option.widget.style() if option.widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, option.widget)

        if option.state & QStyle.State_Selected:
            painter.setPen(QPen(QColor("white"), 4))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, 12, 12)
        painter.restore()


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, repo: WildcardRepository, api: ThumbnailApiClient, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.api = api
        self.setWindowTitle("設定")
        self.resize(860, 680)

        self.library_root = self._path_row(settings.library_root)
        self.thumbnail_root = self._path_row(settings.thumbnail_root)
        self.source_wildcard_root = self._path_row(settings.source_wildcard_root)
        self.source_thumbnail_root = self._path_row(settings.source_thumbnail_root)

        self.api_base_url = QLineEdit(settings.api_base_url)
        self.api_test_button = QPushButton("接続確認")
        self.api_test_button.clicked.connect(self._test_api_connection)
        self.api_timeout_sec = QLineEdit(str(settings.api_timeout_sec))
        self.generation_prompt_prefix = QPlainTextEdit(settings.generation_prompt_prefix)
        self.generation_prompt_prefix.setMaximumHeight(90)
        self.generation_negative_prompt = QPlainTextEdit(settings.generation_negative_prompt)
        self.generation_negative_prompt.setMaximumHeight(100)
        self.generation_width = QLineEdit(str(settings.generation_width))
        self.generation_height = QLineEdit(str(settings.generation_height))
        self.generation_steps = QLineEdit(str(settings.generation_steps))
        self.generation_cfg_scale = QLineEdit(str(settings.generation_cfg_scale))
        self.generation_sampler_name = QLineEdit(settings.generation_sampler_name)
        self.generation_checkpoint_name = QComboBox()
        self.generation_checkpoint_name.setEditable(True)
        self.generation_checkpoint_name.setInsertPolicy(QComboBox.NoInsert)
        self.generation_checkpoint_name.setMinimumContentsLength(32)
        self.generation_checkpoint_name.addItem(settings.generation_checkpoint_name or "")
        self.generation_checkpoint_name.setCurrentText(settings.generation_checkpoint_name)
        self.refresh_checkpoints_button = QPushButton("候補更新")
        self.refresh_checkpoints_button.clicked.connect(self._refresh_checkpoint_list)
        self.checkpoint_status = QLabel("API から候補を取得できます")
        self.checkpoint_status.setWordWrap(True)
        self.checkpoint_status.setStyleSheet("color: #e7b65c;")
        self.generation_extra_payload_json = QPlainTextEdit(settings.generation_extra_payload_json)
        self.generation_extra_payload_json.setMinimumHeight(140)
        self.thumbnail_size = QLineEdit(str(settings.thumbnail_size))

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        file_tab = QWidget()
        file_form = QFormLayout(file_tab)
        file_form.addRow("保存先 wildcard", self.library_root["widget"])
        file_form.addRow("保存先 thumbnail", self.thumbnail_root["widget"])
        file_form.addRow("取込元 wildcard", self.source_wildcard_root["widget"])
        file_form.addRow("取込元 thumbnail", self.source_thumbnail_root["widget"])
        self.source_thumbnail_status = QLabel()
        self.source_thumbnail_status.setWordWrap(True)
        self.source_thumbnail_status.setStyleSheet("color: #e7b65c;")
        file_form.addRow("サムネイル確認", self.source_thumbnail_status)
        file_form.addRow("サムネイルサイズ", self.thumbnail_size)
        tabs.addTab(file_tab, "ファイル")

        generation_tab = QWidget()
        generation_form = QFormLayout(generation_tab)
        api_url_row = QWidget()
        api_url_layout = QHBoxLayout(api_url_row)
        api_url_layout.setContentsMargins(0, 0, 0, 0)
        api_url_layout.setSpacing(8)
        api_url_layout.addWidget(self.api_base_url, 1)
        api_url_layout.addWidget(self.api_test_button, 0)
        generation_form.addRow("APIベースURL", api_url_row)
        generation_form.addRow("APIタイムアウト", self.api_timeout_sec)
        generation_form.addRow("生成プロンプト", self.generation_prompt_prefix)
        generation_form.addRow("ネガティブプロンプト", self.generation_negative_prompt)
        generation_form.addRow("幅", self.generation_width)
        generation_form.addRow("高さ", self.generation_height)
        generation_form.addRow("ステップ数", self.generation_steps)
        generation_form.addRow("CFG", self.generation_cfg_scale)
        generation_form.addRow("サンプラー", self.generation_sampler_name)
        checkpoint_row = QWidget()
        checkpoint_layout = QHBoxLayout(checkpoint_row)
        checkpoint_layout.setContentsMargins(0, 0, 0, 0)
        checkpoint_layout.setSpacing(8)
        checkpoint_layout.addWidget(self.generation_checkpoint_name, 1)
        checkpoint_layout.addWidget(self.refresh_checkpoints_button, 0)
        generation_form.addRow("checkpoint", checkpoint_row)
        generation_form.addRow("", self.checkpoint_status)
        generation_form.addRow("生成 payload JSON", self.generation_extra_payload_json)
        tabs.addTab(generation_tab, "生成")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.source_thumbnail_root["line_edit"].textChanged.connect(self._refresh_source_thumbnail_status)
        self.source_wildcard_root["line_edit"].textChanged.connect(self._refresh_source_thumbnail_status)
        self._refresh_source_thumbnail_status()

    def _path_row(self, value: str) -> dict[str, QWidget]:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        line_edit = QLineEdit(value)
        button = QPushButton("参照")
        button.clicked.connect(lambda: self._pick_directory(line_edit))
        layout.addWidget(line_edit)
        layout.addWidget(button)
        return {"widget": row, "line_edit": line_edit}

    def _pick_directory(self, line_edit: QLineEdit) -> None:
        selected = QFileDialog.getExistingDirectory(self, "フォルダ選択", line_edit.text())
        if selected:
            line_edit.setText(selected)

    def _center_dialog(self, widget: QWidget) -> None:
        geometry = widget.frameGeometry()
        geometry.moveCenter(self.frameGeometry().center())
        widget.move(geometry.topLeft())

    def _show_error(self, title: str, text: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle(title)
        box.setText(text)
        box.setWindowModality(Qt.WindowModal)
        box.setMinimumWidth(520)
        QTimer.singleShot(0, lambda: self._center_dialog(box))
        box.exec()

    def _show_info(self, title: str, text: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(title)
        box.setText(text)
        box.setWindowModality(Qt.WindowModal)
        box.setMinimumWidth(520)
        QTimer.singleShot(0, lambda: self._center_dialog(box))
        box.exec()

    def _ask_ok_cancel(self, title: str, text: str) -> QMessageBox.StandardButton:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        box.setWindowModality(Qt.WindowModal)
        box.setMinimumWidth(520)
        QTimer.singleShot(0, lambda: self._center_dialog(box))
        return QMessageBox.StandardButton(box.exec())

    def _test_api_connection(self) -> None:
        try:
            self.api.test_connection(self._candidate_settings())
            self._show_info("API", "接続できました")
        except Exception as exc:
            self._show_error("API", str(exc))

    def _candidate_settings(self) -> AppSettings:
        return AppSettings(
            library_root=self.library_root["line_edit"].text().strip(),
            thumbnail_root=self.thumbnail_root["line_edit"].text().strip(),
            source_wildcard_root=self.source_wildcard_root["line_edit"].text().strip(),
            source_thumbnail_root=self.source_thumbnail_root["line_edit"].text().strip(),
            api_base_url=self.api_base_url.text().strip(),
            api_timeout_sec=int(self.api_timeout_sec.text().strip() or 120),
            generation_prompt_prefix=self.generation_prompt_prefix.toPlainText().strip(),
            generation_negative_prompt=self.generation_negative_prompt.toPlainText(),
            generation_width=int(self.generation_width.text().strip() or 512),
            generation_height=int(self.generation_height.text().strip() or 768),
            generation_steps=int(self.generation_steps.text().strip() or 20),
            generation_cfg_scale=float(self.generation_cfg_scale.text().strip() or 7.0),
            generation_sampler_name=self.generation_sampler_name.text().strip(),
            generation_checkpoint_name=self.generation_checkpoint_name.currentText().strip(),
            generation_extra_payload_json=self.generation_extra_payload_json.toPlainText().strip() or "{}",
            include_lora_on_copy=True,
            thumbnail_size=int(self.thumbnail_size.text().strip() or 260),
        )

    def _refresh_checkpoint_list(self) -> None:
        current = self.generation_checkpoint_name.currentText().strip()
        try:
            checkpoints = self.api.list_checkpoints(self._candidate_settings())
        except Exception as exc:
            self.checkpoint_status.setStyleSheet("color: #ef8d8d;")
            self.checkpoint_status.setText(f"候補一覧を取得できません: {exc}")
            return

        self.generation_checkpoint_name.blockSignals(True)
        try:
            self.generation_checkpoint_name.clear()
            self.generation_checkpoint_name.addItem("")
            for checkpoint in checkpoints:
                self.generation_checkpoint_name.addItem(checkpoint)
            if current and current not in checkpoints:
                self.generation_checkpoint_name.addItem(current)
            self.generation_checkpoint_name.setCurrentText(current)
            if checkpoints:
                self.checkpoint_status.setStyleSheet("color: #7fd48e;")
                self.checkpoint_status.setText(f"{len(checkpoints)} 件の checkpoint 候補を取得しました")
            else:
                self.checkpoint_status.setStyleSheet("color: #e7b65c;")
                self.checkpoint_status.setText("checkpoint 候補が見つかりませんでした")
        finally:
            self.generation_checkpoint_name.blockSignals(False)

    def _refresh_source_thumbnail_status(self) -> None:
        try:
            inspection = self.repo.inspect_source_thumbnail_roots(self._candidate_settings())
        except Exception as exc:
            self.source_thumbnail_status.setStyleSheet("color: #ef8d8d;")
            self.source_thumbnail_status.setText(f"エラー: {exc}")
            return

        configured = inspection["configured_root"]
        existing = inspection["existing_roots"]
        configured_exists = inspection["configured_exists"]
        if configured_exists:
            self.source_thumbnail_status.setStyleSheet("color: #7fd48e;")
            self.source_thumbnail_status.setText(f"OK: 設定パスが存在します\n{configured}")
            return
        if existing:
            self.source_thumbnail_status.setStyleSheet("color: #e7b65c;")
            self.source_thumbnail_status.setText(
                "設定パスは見つかりませんが、存在する候補が見つかりました\n"
                f"設定パス: {configured}\n"
                f"候補: {existing[0]}"
            )
            return
        self.source_thumbnail_status.setStyleSheet("color: #ef8d8d;")
        self.source_thumbnail_status.setText(
            "設定パスも候補パスも見つかりません\n"
            f"設定パス: {configured}"
        )

    def build_settings(self, base_settings: AppSettings) -> AppSettings:
        json.loads(self.generation_extra_payload_json.toPlainText() or "{}")
        return AppSettings(
            library_root=self.library_root["line_edit"].text().strip(),
            thumbnail_root=self.thumbnail_root["line_edit"].text().strip(),
            source_wildcard_root=self.source_wildcard_root["line_edit"].text().strip(),
            source_thumbnail_root=self.source_thumbnail_root["line_edit"].text().strip(),
            api_base_url=self.api_base_url.text().strip(),
            api_timeout_sec=int(self.api_timeout_sec.text().strip() or base_settings.api_timeout_sec),
            generation_prompt_prefix=self.generation_prompt_prefix.toPlainText().strip(),
            generation_negative_prompt=self.generation_negative_prompt.toPlainText(),
            generation_width=int(self.generation_width.text().strip() or base_settings.generation_width),
            generation_height=int(self.generation_height.text().strip() or base_settings.generation_height),
            generation_steps=int(self.generation_steps.text().strip() or base_settings.generation_steps),
            generation_cfg_scale=float(self.generation_cfg_scale.text().strip() or base_settings.generation_cfg_scale),
            generation_sampler_name=self.generation_sampler_name.text().strip(),
            generation_checkpoint_name=self.generation_checkpoint_name.currentText().strip(),
            generation_extra_payload_json=self.generation_extra_payload_json.toPlainText().strip() or "{}",
            include_lora_on_copy=base_settings.include_lora_on_copy,
            thumbnail_size=int(self.thumbnail_size.text().strip() or base_settings.thumbnail_size),
            window_width=base_settings.window_width,
            window_height=base_settings.window_height,
            window_maximized=base_settings.window_maximized,
            splitter_sizes=base_settings.splitter_sizes,
            detail_splitter_sizes=base_settings.detail_splitter_sizes,
            last_folder=base_settings.last_folder,
            sort_mode=base_settings.sort_mode,
        )

    def accept(self) -> None:
        try:
            inspection = self.repo.inspect_source_thumbnail_roots(self._candidate_settings())
        except Exception as exc:
            self._show_error("設定エラー", str(exc))
            return

        if not inspection["configured_exists"] and not inspection["existing_roots"]:
            result = self._ask_ok_cancel(
                "サムネイル設定の確認",
                "サムネイルの設定先が見つかりません。\n"
                "このまま続けると、サムネイル生成は 0 件になります。\n\n"
                f"設定先:\n{inspection['configured_root']}",
            )
            if result != QMessageBox.Ok:
                return
        super().accept()


class ToastPopup(QFrame):
    def __init__(self, parent: QWidget, message: str):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.SubWindow)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setObjectName("toastPopup")
        self.setStyleSheet(
            """
            QFrame#toastPopup {
                background: rgba(22, 27, 33, 235);
                border: 1px solid #3e4b57;
                border-radius: 12px;
            }
            QLabel {
                color: #f4f7fb;
                font-size: 13px;
                padding: 10px 14px;
            }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(message)
        layout.addWidget(label)
        self.adjustSize()

    def show_for(self, ms: int = 1400) -> None:
        parent = self.parentWidget()
        if parent is not None:
            x = (parent.width() - self.width()) // 2
            y = parent.height() - self.height() - 36
            self.move(max(12, x), max(12, y))
            self.raise_()
        self.show()
        QTimer.singleShot(ms, self.close)


class ImportOptionsDialog(QDialog):
    def __init__(self, title: str, message: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(520, 220)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(message))

        form = QFormLayout()
        self.policy_combo = QComboBox()
        for key, label in CONFLICT_POLICIES.items():
            self.policy_combo.addItem(label, key)
        form.addRow("Conflict policy", self.policy_combo)
        layout.addLayout(form)

        help_label = QLabel("Choose how to handle conflicts when a file already exists. This applies to both txt and thumbnail files.")
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #aeb8c2;")
        layout.addWidget(help_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def conflict_policy(self) -> str:
        return str(self.policy_combo.currentData())


class CachedEntriesLoader(QObject):
    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, db_path: Path, settings: AppSettings):
        super().__init__()
        self.db_path = db_path
        self.settings = settings

    @Slot()
    def run(self) -> None:
        try:
            repo = WildcardRepository(self.db_path)
            entries = repo.load_entries_summary(self.settings)
            self.finished.emit(entries)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, app_dir: Path):
        super().__init__()
        self.app_dir = app_dir
        self.settings_store = SettingsStore(app_dir)
        self.ui_state_store = UIStateStore(app_dir)
        self.settings = self.ui_state_store.load_into(self.settings_store.load())
        self.repo = WildcardRepository(app_dir / "wildcard_manager.db")
        self.api = ThumbnailApiClient()

        self.entries: list[WildcardEntry] = []
        self.filtered_entries: list[WildcardEntry] = []
        self.current_entry: WildcardEntry | None = None
        self.current_folder_prefix = self.settings.last_folder
        self.entry_by_path: dict[str, WildcardEntry] = {}
        self.search_cache: dict[str, str] = {}
        self.toast_popup: ToastPopup | None = None
        self.is_edit_mode = False
        self.cached_load_thread: QThread | None = None
        self.cached_load_worker: CachedEntriesLoader | None = None
        self.reload_after_cached_load = False
        self.last_splitter_sizes: list[int] = []
        self.left_panel_width = max(220, self.settings.splitter_sizes[0] if len(self.settings.splitter_sizes) == 3 else 260)
        self.right_panel_width = max(280, self.settings.splitter_sizes[2] if len(self.settings.splitter_sizes) == 3 else 520)
        self.thumbnail_generation_thread: QThread | None = None
        self.thumbnail_generation_worker: SingleThumbnailWorker | None = None
        self.thumbnail_loading_timer = QTimer(self)
        self.thumbnail_loading_frames = ["[    ]", "[=   ]", "[==  ]", "[=== ]", "[ ===]", "[  ==]", "[   =]"]
        self.thumbnail_loading_frame_index = 0
        self.thumbnail_loading_message = ""
        self._preview_cache_key: tuple[str, int, int] | None = None
        self._preview_cache_pixmap: QPixmap | None = None
        self._preview_request_token = 0
        self._content_request_token = 0
        self._content_load_cache: dict[str, str] = {}
        self._suppress_splitter_tracking = False
        self.folder_children_map: dict[str, set[str]] = {}
        self._folder_tree_populating = False
        self._startup_load_requested = False

        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(250)
        self.filter_timer.timeout.connect(self.apply_filters)
        self.thumbnail_loading_timer.setInterval(120)
        self.thumbnail_loading_timer.timeout.connect(self._advance_thumbnail_loading_animation)

        self.list_model = ThumbnailListModel(self)
        self.list_model.set_thumbnail_size(self.settings.thumbnail_size)
        self.item_delegate = ThumbnailCardDelegate(self)

        self.setWindowTitle("Wildcard Manager")
        self.setMinimumSize(1280, 760)
        self.resize(max(self.settings.window_width, 1280), max(self.settings.window_height, 760))
        self._build_ui()
        self._create_actions()

    def _build_ui(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #111315;
                color: #eef2f7;
            }
            QLineEdit, QTextEdit, QPlainTextEdit, QListWidget, QTreeWidget, QListView, QComboBox, QSpinBox {
                background: #1a1d22;
                border: 1px solid #2d3640;
                border-radius: 10px;
                color: #eef2f7;
                padding: 4px;
            }
            QListView {
                outline: 0;
                selection-background-color: transparent;
                selection-color: #eef2f7;
            }
            QPushButton {
                background: #225d8f;
                border: 0;
                border-radius: 8px;
                padding: 5px 10px;
                color: white;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #2d75b3;
            }
            QToolBar {
                background: #0d0f11;
                border-bottom: 1px solid #20262d;
                spacing: 6px;
            }
            QToolBar QToolButton {
                background: #1a1d22;
                border: 1px solid #2d3640;
                border-radius: 7px;
                padding: 4px 8px;
                margin: 1px 0;
                color: #eef2f7;
            }
            QToolBar QToolButton:hover {
                background: #252b33;
            }
            """
        )

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(14, 14, 14, 14)
        root_layout.setSpacing(12)

        self.sort_combo = QComboBox()
        for key, label in SORT_LABELS.items():
            self.sort_combo.addItem(label, key)
        self.sort_combo.setMinimumWidth(170)
        self.sort_combo.setMaximumWidth(220)
        idx = self.sort_combo.findData(self.settings.sort_mode)
        if idx >= 0:
            self.sort_combo.setCurrentIndex(idx)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("検索: ファイル名 / 本文 / カスタムタグ / lora:名前")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(360)
        self.search_edit.textChanged.connect(self.schedule_apply_filters)
        self.search_edit.setPlaceholderText("検索: ファイル名 / 本文 / lora:名前")

        self.thumbnail_size_decrease_button = QPushButton("-")
        self.thumbnail_size_decrease_button.setFixedWidth(32)
        self.thumbnail_size_decrease_button.setFixedHeight(32)
        self.thumbnail_size_decrease_button.clicked.connect(lambda: self._step_thumbnail_size(-THUMBNAIL_SIZE_STEP))
        self.thumbnail_size_display = QLabel()
        self.thumbnail_size_display.setAlignment(Qt.AlignCenter)
        self.thumbnail_size_display.setFixedWidth(92)
        self.thumbnail_size_display.setFixedHeight(32)
        self.thumbnail_size_display.setStyleSheet("background: #1a1d22; border: 1px solid #2d3640; border-radius: 10px; color: #eef2f7;")
        self._update_thumbnail_size_display(self.settings.thumbnail_size)
        self.thumbnail_size_increase_button = QPushButton("+")
        self.thumbnail_size_increase_button.setFixedWidth(32)
        self.thumbnail_size_increase_button.setFixedHeight(32)
        self.thumbnail_size_increase_button.clicked.connect(lambda: self._step_thumbnail_size(THUMBNAIL_SIZE_STEP))

        sort_group = QWidget()
        sort_layout = QHBoxLayout(sort_group)
        sort_layout.setContentsMargins(0, 0, 0, 0)
        sort_layout.setSpacing(8)
        sort_layout.setAlignment(Qt.AlignVCenter)
        sort_layout.addWidget(QLabel("並び順"))
        sort_layout.addWidget(self.sort_combo)
        sort_group.setFixedHeight(32)

        self.search_group = QWidget()
        search_layout = QHBoxLayout(self.search_group)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(8)
        search_layout.setAlignment(Qt.AlignVCenter)
        search_layout.addWidget(QLabel("検索"))
        search_layout.addWidget(self.search_edit, 0, Qt.AlignLeft)
        search_layout.addStretch(1)
        self.search_group.setFixedHeight(32)

        self.size_group = QWidget()
        size_layout = QHBoxLayout(self.size_group)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.setSpacing(8)
        size_layout.setAlignment(Qt.AlignVCenter)
        size_layout.addWidget(QLabel("サムネサイズ"))
        size_layout.addWidget(self.thumbnail_size_decrease_button)
        size_layout.addWidget(self.thumbnail_size_display)
        size_layout.addWidget(self.thumbnail_size_increase_button)
        self.size_group.setMaximumWidth(260)
        self.size_group.setFixedHeight(32)

        self.top_controls = QWidget()
        self.top_controls_layout = QGridLayout(self.top_controls)
        self.top_controls_layout.setContentsMargins(0, 0, 0, 0)
        self.top_controls_layout.setHorizontalSpacing(10)
        self.top_controls_layout.setVerticalSpacing(8)
        self.pane_group = QWidget()
        pane_layout = QHBoxLayout(self.pane_group)
        pane_layout.setContentsMargins(0, 0, 0, 0)
        pane_layout.setSpacing(8)
        pane_layout.setAlignment(Qt.AlignVCenter)
        self.left_toggle_button = QPushButton()
        self.left_toggle_button.clicked.connect(self.toggle_left_panel)
        self.right_toggle_button = QPushButton()
        self.right_toggle_button.clicked.connect(self.toggle_right_panel)
        self.left_toggle_button.setFixedHeight(32)
        self.right_toggle_button.setFixedHeight(32)
        pane_layout.addWidget(self.left_toggle_button)
        pane_layout.addWidget(self.right_toggle_button)
        self.pane_group.setFixedHeight(32)
        self.top_controls_layout.addWidget(sort_group, 0, 0, Qt.AlignLeft)
        self.top_controls_layout.addWidget(self.size_group, 0, 1, Qt.AlignRight)
        self.top_controls_layout.addWidget(self.pane_group, 0, 2, Qt.AlignRight)
        self.top_controls_layout.setColumnStretch(0, 0)
        self.top_controls_layout.setColumnStretch(1, 1)
        self.top_controls_layout.setColumnStretch(2, 0)
        root_layout.addWidget(self.top_controls)
        self._update_top_controls_layout()

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(True)
        root_layout.addWidget(self.splitter, 1)

        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderLabel("フォルダ")
        self.folder_tree.itemSelectionChanged.connect(self._on_folder_changed)
        self.folder_tree.itemExpanded.connect(self._on_folder_item_expanded)
        self.folder_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.folder_tree.customContextMenuRequested.connect(self._show_folder_context_menu)
        self.folder_tree.setMinimumWidth(0)
        self.folder_tree.setIndentation(12)
        self.folder_tree.setAnimated(False)
        self.folder_tree.setRootIsDecorated(True)
        self.folder_tree.setExpandsOnDoubleClick(True)

        self.left_panel = QWidget()
        self.left_panel.setMinimumWidth(220)
        self.left_panel.setMaximumWidth(360)
        left_layout = QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        self.folder_address = QLineEdit()
        self.folder_address.setReadOnly(True)
        self.folder_address.setPlaceholderText("選択中フォルダ")
        self.folder_address.setMinimumWidth(0)
        left_layout.addWidget(self.folder_address)
        left_layout.addWidget(self.folder_tree, 1)
        self.splitter.addWidget(self.left_panel)

        center = QWidget()
        center.setMinimumWidth(520)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(8)
        self.result_count_label = QLabel("0 件")
        self.result_count_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.result_count_label.setIndent(12)
        self.result_count_label.setStyleSheet("background: #14181e; border: 1px solid #28303a; border-radius: 10px; color: #eef2f7;")
        self.search_summary_row = QWidget()
        search_summary_layout = QHBoxLayout(self.search_summary_row)
        search_summary_layout.setContentsMargins(0, 0, 0, 0)
        search_summary_layout.setSpacing(8)
        search_summary_layout.addWidget(self.search_edit, 1)
        search_summary_layout.addWidget(self.result_count_label)
        center_layout.addWidget(self.search_summary_row)
        self._set_result_count_label(0)

        self.card_view = QListView()
        self.card_view.setModel(self.list_model)
        self.card_view.setItemDelegate(self.item_delegate)
        self.card_view.setViewMode(QListView.IconMode)
        self.card_view.setResizeMode(QListView.Adjust)
        self.card_view.setMovement(QListView.Static)
        self.card_view.setWrapping(True)
        self.card_view.setLayoutMode(QListView.Batched)
        self.card_view.setBatchSize(64)
        self.card_view.setWordWrap(True)
        self.card_view.setSpacing(14)
        self.card_view.setUniformItemSizes(True)
        self.card_view.setSelectionMode(QListView.SingleSelection)
        self.card_view.setFrameShape(QFrame.NoFrame)
        self.card_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.card_view.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.card_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.card_view.customContextMenuRequested.connect(self._show_card_context_menu)
        self.card_view.doubleClicked.connect(self._on_card_double_clicked)
        self.card_view.setMinimumWidth(0)
        self.card_view.verticalScrollBar().valueChanged.connect(self._prefetch_visible_thumbnails)
        center_layout.addWidget(self.card_view, 1)
        self.splitter.addWidget(center)

        self.right_panel = QWidget()
        self.right_panel.setMinimumWidth(360)
        self.right_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        right_layout.addWidget(right_scroll, 1)

        right_content = QWidget()
        right_scroll.setWidget(right_content)
        right_content_layout = QVBoxLayout(right_content)
        right_content_layout.setContentsMargins(0, 0, 0, 0)
        right_content_layout.setSpacing(10)

        self.path_label = QLabel("ファイル未選択")
        self.path_label.setMinimumWidth(0)
        self.path_label.setMinimumHeight(42)
        self.path_label.setWordWrap(False)
        self.path_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.path_label.setIndent(12)
        self.path_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.path_label.setStyleSheet("background: #14181e; border: 1px solid #28303a; border-radius: 10px; color: #d4dbe3;")
        self._path_label_text = "ファイル未選択"

        preview_panel = QFrame()
        preview_panel.setObjectName("detailPreviewPanel")
        preview_panel.setMinimumWidth(0)
        preview_panel.setStyleSheet(
            "QFrame#detailPreviewPanel {"
            " background: #10151b;"
            " border: 1px solid #28303a;"
            " border-radius: 14px;"
            "}"
        )
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(10, 10, 10, 10)
        preview_layout.setSpacing(8)

        self.thumbnail_label = QLabel("サムネイルなし")
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label.setMinimumHeight(260)
        self.thumbnail_label.setMaximumHeight(360)
        self.thumbnail_label.setMinimumWidth(0)
        self.thumbnail_label.setStyleSheet("border: 1px solid #2d3640; border-radius: 14px; background: #0b0d10;")
        preview_layout.addWidget(self.thumbnail_label)
        preview_layout.addWidget(self.path_label)

        preview_layout.addWidget(QLabel("LoRA"))
        self.lora_list = QListWidget()
        lora_height = self._lora_list_height(4)
        self.lora_list.setFixedHeight(lora_height)
        self.lora_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.lora_list.setMinimumWidth(0)
        self.lora_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lora_list.customContextMenuRequested.connect(self._show_lora_context_menu)
        preview_layout.addWidget(self.lora_list)
        right_content_layout.addWidget(preview_panel)

        editor_panel = QFrame()
        editor_panel.setObjectName("detailEditorPanel")
        editor_panel.setMinimumWidth(0)
        editor_panel.setStyleSheet(
            "QFrame#detailEditorPanel {"
            " background: #10151b;"
            " border: 1px solid #28303a;"
            " border-radius: 14px;"
            "}"
        )
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(10, 10, 10, 10)
        editor_layout.setSpacing(8)
        copy_buttons_row = QHBoxLayout()
        copy_buttons_row.setSpacing(8)
        self.copy_all_with_lora_button = QPushButton("全文コピー LoRAあり")
        self.copy_all_with_lora_button.clicked.connect(lambda: self.copy_all_text(True))
        self.copy_all_without_lora_button = QPushButton("全文コピー LoRAなし")
        self.copy_all_without_lora_button.clicked.connect(lambda: self.copy_all_text(False))
        self.copy_all_with_lora_button.setMinimumWidth(0)
        self.copy_all_without_lora_button.setMinimumWidth(0)
        self.copy_all_with_lora_button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.copy_all_without_lora_button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        copy_buttons_row.addWidget(self.copy_all_with_lora_button)
        copy_buttons_row.addWidget(self.copy_all_without_lora_button)
        editor_layout.addLayout(copy_buttons_row)
        editor_layout.addWidget(QLabel("検索用カスタムタグ"))
        self.custom_tags_edit = TagEditorWidget()
        self.custom_tags_edit.setMinimumWidth(0)
        editor_layout.addWidget(self.custom_tags_edit)
        self.custom_tags_edit.hide()
        editor_layout.addWidget(QLabel("プロンプトタグ"))
        self.prompt_tags_edit = TagEditorWidget()
        self.prompt_tags_edit.setMinimumWidth(0)
        self.prompt_tags_edit.tagsChanged.connect(self._sync_editor_from_prompt_tags)
        editor_layout.addWidget(self.prompt_tags_edit)
        editor_header_row = QHBoxLayout()
        editor_header_row.setContentsMargins(0, 0, 0, 0)
        editor_header_row.setSpacing(8)
        editor_header_row.addWidget(QLabel("元テキスト"))
        editor_header_row.addStretch(1)
        self.edit_button = QPushButton("編集")
        self.edit_button.clicked.connect(self.toggle_edit_mode)
        editor_header_row.addWidget(self.edit_button)
        editor_layout.addLayout(editor_header_row)
        self.editor = QTextEdit()
        self.editor.setMinimumHeight(180)
        self.editor.setMinimumWidth(0)
        self.editor.setReadOnly(True)
        self.editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        editor_layout.addWidget(self.editor, 1)
        right_content_layout.addWidget(editor_panel)
        self.splitter.addWidget(self.right_panel)

        self.splitter.setSizes(self._normalized_splitter_sizes(self.settings.splitter_sizes))
        self.splitter.setCollapsible(0, True)
        self.splitter.setCollapsible(1, False)
        self.splitter.setCollapsible(2, True)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.last_splitter_sizes = self.splitter.sizes()
        self.splitter.splitterMoved.connect(self._remember_splitter_sizes)
        self._apply_uniform_control_height()
        self._refresh_path_label()
        self._update_panel_toggle_buttons()
        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())

        self.card_view.selectionModel().currentChanged.connect(self._on_card_selection_changed)
        self.result_count_label.setText(f"{len(self.filtered_entries)} 件")
        self._update_view_sizes()
        self.statusBar().showMessage("起動準備完了", 2000)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._startup_load_requested:
            self._startup_load_requested = True
            QTimer.singleShot(150, self.load_cached_entries)

    def _create_actions(self) -> None:
        toolbar = self.addToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        actions = [
            ("再読込", self.rescan_library),
            ("設定", self.open_settings),
            ("元データをコピーして取り込む", lambda: self.run_import("copy")),
            ("元データを移動して取り込む", lambda: self.run_import("move")),
            ("サムネ生成", self.generate_thumbnail_for_current),
            ("不足分サムネ生成", self.generate_missing_thumbnails),
        ]
        for label, callback in actions:
            action = QAction(label, self)
            action.triggered.connect(callback)
            toolbar.addAction(action)

    def _sort_entries(self, entries: list[WildcardEntry]) -> list[WildcardEntry]:
        mode = self.settings.sort_mode
        if mode == "name_desc":
            return sorted(entries, key=lambda entry: natural_sort_key(entry.name), reverse=True)
        if mode == "path_asc":
            return sorted(entries, key=lambda entry: natural_sort_key(entry.rel_path))
        if mode == "folder_asc":
            return sorted(entries, key=lambda entry: (natural_sort_key(entry.folder), natural_sort_key(entry.name)))
        return sorted(entries, key=lambda entry: natural_sort_key(entry.name))

    def _on_sort_changed(self) -> None:
        self.settings.sort_mode = self.sort_combo.currentData()
        self.apply_filters()

    def _set_thumbnail_size(self, value: int) -> None:
        clamped = max(THUMBNAIL_SIZE_MIN, min(THUMBNAIL_SIZE_MAX, int(value)))
        if self.settings.thumbnail_size == clamped:
            self._update_thumbnail_size_display(clamped)
            return
        self.settings.thumbnail_size = clamped
        self._update_thumbnail_size_display(clamped)
        self.list_model.set_thumbnail_size(clamped)
        self._update_view_sizes()

    def _step_thumbnail_size(self, delta: int) -> None:
        self._set_thumbnail_size(self.settings.thumbnail_size + delta)

    def _update_thumbnail_size_display(self, value: int) -> None:
        self.thumbnail_size_display.setText(f"{value} px")

    def _apply_uniform_control_height(self, height: int = 32) -> None:
        widgets = [
            self.sort_combo,
            self.search_edit,
            self.thumbnail_size_decrease_button,
            self.thumbnail_size_display,
            self.thumbnail_size_increase_button,
            self.left_toggle_button,
            self.right_toggle_button,
            self.folder_address,
            self.copy_all_with_lora_button,
            self.copy_all_without_lora_button,
            self.edit_button,
        ]
        for widget in widgets:
            widget.setMinimumHeight(height)
            widget.setMaximumHeight(height)

        self.result_count_label.setMinimumHeight(height)
        self.result_count_label.setMaximumHeight(height)
        self.path_label.setMinimumHeight(height)
        self.path_label.setMaximumHeight(height)

    def _set_result_count_label(self, count: int) -> None:
        self.result_count_label.setText(f"{count} \u4ef6")

    def _refresh_path_label(self) -> None:
        raw_text = getattr(self, "_path_label_text", "") or "ファイル未選択"
        available_width = max(80, self.path_label.width() - 12)
        text = self.path_label.fontMetrics().elidedText(raw_text, Qt.ElideMiddle, available_width)
        self.path_label.setText(text)

    def _update_view_sizes(self) -> None:
        width = max(80, self.settings.thumbnail_size)
        height = int(width * CARD_ICON_RATIO)
        self.card_view.setIconSize(QSize(width, height))
        self.card_view.setGridSize(QSize(width + CARD_FRAME_PADDING, height + CARD_TITLE_HEIGHT + CARD_FRAME_PADDING))
        self.card_view.doItemsLayout()
        self.card_view.updateGeometries()
        self.card_view.viewport().update()

    def _lora_list_height(self, rows: int) -> int:
        metrics = QFontMetrics(self.lora_list.font())
        row_height = metrics.lineSpacing() + 8
        frame_padding = 12
        return rows * row_height + frame_padding

    def _update_preview_pixmap(self) -> None:
        if self.current_entry is None:
            self._preview_cache_key = None
            self._preview_cache_pixmap = None
            self.thumbnail_label.setPixmap(QPixmap())
            self.thumbnail_label.setText("サムネイルなし")
            return
        if self.thumbnail_loading_message:
            return
        if self.current_entry.thumbnail_path and Path(self.current_entry.thumbnail_path).exists():
            target_size = self.thumbnail_label.contentsRect().size()
            if target_size.width() > 0 and target_size.height() > 0:
                cache_key = (self.current_entry.thumbnail_path, target_size.width(), target_size.height())
                if self._preview_cache_key == cache_key and self._preview_cache_pixmap is not None:
                    self.thumbnail_label.setPixmap(self._preview_cache_pixmap)
                    self.thumbnail_label.setText("")
                    return
                self.thumbnail_label.setPixmap(QPixmap())
                self.thumbnail_label.setText("読み込み中...")
                self._preview_request_token += 1
                token = self._preview_request_token
                self._queue_preview_load(self.current_entry.abs_path, self.current_entry.thumbnail_path, target_size.width(), target_size.height(), token)
                return
        self.thumbnail_label.setPixmap(QPixmap())
        self.thumbnail_label.setText("サムネイルなし")

    def _queue_preview_load(self, abs_path: str, thumbnail_path: str, width: int, height: int, token: int) -> None:
        if not thumbnail_path or not Path(thumbnail_path).exists():
            return
        if not hasattr(self, "_preview_load_signals"):
            self._preview_load_signals = PreviewLoadSignals(self)
            self._preview_load_signals.loaded.connect(self._on_preview_loaded)
            self._preview_load_signals.failed.connect(self._on_preview_failed)
            self._preview_load_pool = QThreadPool.globalInstance()
        self._pending_preview_request = (abs_path, thumbnail_path, width, height, token)
        self._preview_load_pool.start(PreviewLoadTask(abs_path, thumbnail_path, width, height, token, self._preview_load_signals), 0)

    @Slot(str, str, int, int, int, object)
    def _on_preview_loaded(self, abs_path: str, thumbnail_path: str, width: int, height: int, token: int, image: QImage) -> None:
        if self.current_entry is None or self.current_entry.abs_path != abs_path:
            return
        if token != self._preview_request_token:
            return
        cache_key = (thumbnail_path, width, height)
        pixmap = QPixmap.fromImage(image)
        self._preview_cache_key = cache_key
        self._preview_cache_pixmap = pixmap
        self.thumbnail_label.setPixmap(pixmap)
        self.thumbnail_label.setText("")

    @Slot(str, str, int, int, int)
    def _on_preview_failed(self, abs_path: str, thumbnail_path: str, width: int, height: int, token: int) -> None:
        if self.current_entry is None or self.current_entry.abs_path != abs_path:
            return
        if token != self._preview_request_token:
            return
        self.thumbnail_label.setPixmap(QPixmap())
        self.thumbnail_label.setText("サムネイルなし")

    def _update_top_controls_layout(self) -> None:
        self.top_controls_layout.setColumnStretch(0, 0)
        self.top_controls_layout.setColumnStretch(1, 1)
        self.top_controls_layout.setColumnStretch(2, 0)

    def _normalized_splitter_sizes(self, sizes: list[int] | None) -> list[int]:
        default_sizes = [260, 980, 420]
        if not sizes or len(sizes) != 3:
            return default_sizes
        normalized = [max(0, int(size)) for size in sizes]
        if normalized[1] <= 0:
            normalized[1] = default_sizes[1]
        if normalized[0] < 180 or normalized[2] < 320 or normalized[1] < 480:
            return default_sizes
        if sum(normalized) <= 0:
            return default_sizes
        return normalized

    def _restore_splitter_sizes(self, previous_sizes: list[int]) -> None:
        target_sizes = self.last_splitter_sizes if len(self.last_splitter_sizes) == 3 else previous_sizes
        if len(target_sizes) != 3:
            return
        QTimer.singleShot(0, lambda sizes=list(target_sizes): self._apply_splitter_sizes(sizes))

    def _apply_splitter_sizes(self, sizes: list[int]) -> None:
        if len(sizes) != 3:
            return
        total = sum(self.splitter.sizes())
        if total <= 0:
            return
        left = max(180, sizes[0])
        right = max(320, sizes[2])
        center = max(0, total - left - right)
        if center < 480:
            center = 480
            remaining = max(0, total - center)
            left = max(180, min(left, remaining // 2))
            right = max(320, remaining - left)
        self._suppress_splitter_tracking = True
        try:
            self.splitter.setSizes([left, center, right])
        finally:
            self._suppress_splitter_tracking = False

    def _remember_splitter_sizes(self) -> None:
        if self._suppress_splitter_tracking:
            return
        sizes = self.splitter.sizes()
        if len(sizes) == 3:
            self.last_splitter_sizes = list(sizes)
            if sizes[0] > 0:
                self.left_panel_width = sizes[0]
            if sizes[2] > 0:
                self.right_panel_width = sizes[2]
            self._update_panel_toggle_buttons()

    def _prefetch_visible_thumbnails(self) -> None:
        if not self.filtered_entries:
            return
        top_index = self.card_view.indexAt(QPoint(12, 12))
        bottom_point = QPoint(max(12, self.card_view.viewport().width() - 12), max(12, self.card_view.viewport().height() - 12))
        bottom_index = self.card_view.indexAt(bottom_point)
        start = top_index.row() if top_index.isValid() else 0
        end = bottom_index.row() if bottom_index.isValid() else min(len(self.filtered_entries) - 1, start + 30)
        self.list_model.preload_rows(max(0, start - 1), min(len(self.filtered_entries) - 1, end + 3), priority=1)

    def _update_panel_toggle_buttons(self) -> None:
        sizes = self.splitter.sizes()
        left_visible = len(sizes) == 3 and sizes[0] > 0
        right_visible = len(sizes) == 3 and sizes[2] > 0
        self.left_toggle_button.setText("左を隠す" if left_visible else "左を表示")
        self.right_toggle_button.setText("右を隠す" if right_visible else "右を表示")

    def toggle_left_panel(self) -> None:
        sizes = self.splitter.sizes()
        if len(sizes) != 3:
            return
        if sizes[0] > 0:
            self.left_panel_width = sizes[0]
            self._apply_splitter_sizes([0, sizes[1] + sizes[0], sizes[2]])
        else:
            self._apply_splitter_sizes([self.left_panel_width, max(0, sizes[1] - self.left_panel_width), sizes[2]])
        self._update_panel_toggle_buttons()

    def toggle_right_panel(self) -> None:
        sizes = self.splitter.sizes()
        if len(sizes) != 3:
            return
        if sizes[2] > 0:
            self.right_panel_width = sizes[2]
            self._apply_splitter_sizes([sizes[0], sizes[1] + sizes[2], 0])
        else:
            self._apply_splitter_sizes([sizes[0], max(0, sizes[1] - self.right_panel_width), self.right_panel_width])
        self._update_panel_toggle_buttons()

    def _center_window(self, widget: QWidget) -> None:
        target = self.frameGeometry().center()
        geometry = widget.frameGeometry()
        geometry.moveCenter(target)
        widget.move(geometry.topLeft())

    def _exec_message_box(
        self,
        icon: QMessageBox.Icon,
        title: str,
        text: str,
        buttons: QMessageBox.StandardButtons = QMessageBox.Ok,
        default_button: QMessageBox.StandardButton | None = None,
    ) -> QMessageBox.StandardButton:
        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(buttons)
        if default_button is not None:
            box.setDefaultButton(default_button)
        box.setWindowModality(Qt.WindowModal)
        box.setMinimumWidth(520)
        QTimer.singleShot(0, lambda: self._center_window(box))
        return QMessageBox.StandardButton(box.exec())

    def _show_info(self, title: str, text: str) -> None:
        self._exec_message_box(QMessageBox.Information, title, text)

    def _show_warning(self, title: str, text: str) -> None:
        self._exec_message_box(QMessageBox.Warning, title, text)

    def _show_error(self, title: str, text: str) -> None:
        self._exec_message_box(QMessageBox.Critical, title, text)

    def _ask_yes_no(
        self,
        title: str,
        text: str,
        *,
        default_button: QMessageBox.StandardButton = QMessageBox.Yes,
    ) -> QMessageBox.StandardButton:
        return self._exec_message_box(
            QMessageBox.Question,
            title,
            text,
            QMessageBox.Yes | QMessageBox.No,
            default_button,
        )

    def _ask_yes_no_cancel(
        self,
        title: str,
        text: str,
        *,
        default_button: QMessageBox.StandardButton = QMessageBox.Yes,
    ) -> QMessageBox.StandardButton:
        return self._exec_message_box(
            QMessageBox.Question,
            title,
            text,
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            default_button,
        )

    def _create_progress_dialog(
        self,
        title: str,
        label: str,
        *,
        maximum: int = 0,
        can_cancel: bool = True,
    ) -> QProgressDialog:
        progress = QProgressDialog(label, "キャンセル" if can_cancel else "", 0, maximum, self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumWidth(560)
        progress.setMaximumWidth(560)
        if not can_cancel:
            progress.setCancelButton(None)
        QTimer.singleShot(0, lambda: self._center_window(progress))
        progress.show()
        QApplication.processEvents()
        return progress

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self.repo, self.api, self)
        QTimer.singleShot(0, lambda: self._center_window(dialog))
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self.settings = dialog.build_settings(self.settings)
            self.settings_store.save(self.settings)
            self._update_thumbnail_size_display(self.settings.thumbnail_size)
            with QSignalBlocker(self.sort_combo):
                idx = self.sort_combo.findData(self.settings.sort_mode)
                if idx >= 0:
                    self.sort_combo.setCurrentIndex(idx)
            self.list_model.set_thumbnail_size(self.settings.thumbnail_size)
            self._update_view_sizes()
            self.statusBar().showMessage("設定を保存しました", 4000)
            self.load_cached_entries()
        except Exception as exc:
            self._show_error("險ｭ螳壹お繝ｩ繝ｼ", str(exc))

    def load_cached_entries(self) -> None:
        if self.cached_load_thread is not None and self.cached_load_thread.isRunning():
            self.reload_after_cached_load = True
            return
        self.reload_after_cached_load = False
        self.statusBar().showMessage("キャッシュを読み込み中...", 0)

        self.cached_load_thread = QThread(self)
        self.cached_load_worker = CachedEntriesLoader(self.app_dir / "wildcard_manager.db", self.settings)
        self.cached_load_worker.moveToThread(self.cached_load_thread)
        self.cached_load_thread.started.connect(self.cached_load_worker.run)
        self.cached_load_worker.finished.connect(self._on_cached_entries_loaded)
        self.cached_load_worker.failed.connect(self._on_cached_entries_failed)
        self.cached_load_worker.finished.connect(self._cleanup_cached_entries_loader)
        self.cached_load_worker.failed.connect(self._cleanup_cached_entries_loader)
        self.cached_load_thread.start()

    def _on_cached_entries_loaded(self, cached_entries: list[WildcardEntry]) -> None:
        try:
            self.entries = list(cached_entries)
            self.filtered_entries = []
            self.entry_by_path = {entry.abs_path: entry for entry in self.entries}
            self.search_cache = {entry.abs_path: entry.search_text for entry in self.entries}

            folder_children: dict[str, set[str]] = {}
            for entry in self.entries:
                if not entry.folder:
                    continue
                chain = ""
                for part in entry.folder.split("/"):
                    parent_key = chain
                    chain = part if not chain else f"{chain}/{part}"
                    folder_children.setdefault(parent_key, set()).add(part)

            self.folder_children_map = folder_children
            self._rebuild_folder_tree()
            current_item = self.folder_tree.currentItem()
            self.current_folder_prefix = current_item.data(0, Qt.UserRole) if current_item else ""
            self.folder_address.setText(self.current_folder_prefix or self.settings.library_root)
            self.apply_filters()
            # The initial async load can arrive after the first layout pass, so
            # force one more geometry refresh once real items are in the model.
            QTimer.singleShot(0, self._update_view_sizes)
            self.statusBar().showMessage(f"キャッシュ {len(self.entries)} 件を読み込みました。", 6000)
        except Exception as exc:
            self._show_error("キャッシュ再構築エラー", str(exc))

    def _on_cached_entries_failed(self, message: str) -> None:
        self._show_error("キャッシュ読み込みエラー", message)

    def _cleanup_cached_entries_loader(self) -> None:
        thread = self.cached_load_thread
        worker = self.cached_load_worker
        self.cached_load_thread = None
        self.cached_load_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(2000)
            thread.deleteLater()
        if worker is not None:
            worker.deleteLater()
        if self.reload_after_cached_load:
            self.reload_after_cached_load = False
            QTimer.singleShot(0, self.load_cached_entries)

    def rescan_library(self, initial: bool = False) -> None:
        progress = self._create_progress_dialog("再読込", "ライブラリを再読込中...", can_cancel=True)
        try:
            stats = self.repo.scan_library(self.settings, progress=lambda i, total, name: self._step_progress(progress, i, total, name))
            self.statusBar().showMessage("再読込後のキャッシュを読み込み中...", 0)
            QTimer.singleShot(0, self.load_cached_entries)
            self.statusBar().showMessage(
                f"再読込完了 scanned={stats['scanned']} updated={stats['updated']} deleted={stats['deleted']}",
                6000,
            )
        except OperationCancelledError:
            self.statusBar().showMessage("再読込をキャンセルしました", 5000)
        except Exception as exc:
            self._show_error("再読込エラー", str(exc))
        finally:
            progress.close()
        if initial and not self.entries:
            self.statusBar().showMessage("ライブラリは空です。設定と取り込み元パスを確認してください。", 6000)

    def _filter_existing_entries(self, entries: list[WildcardEntry]) -> list[WildcardEntry]:
        return [entry for entry in entries if Path(entry.abs_path).exists()]

    def _step_progress(self, dialog: QProgressDialog, value: int, maximum: int, name: str) -> None:
        if dialog.wasCanceled():
            return False
        if dialog.maximum() != maximum:
            dialog.setMaximum(maximum)
        dialog.setValue(value)
        dialog.setLabelText(f"{value}/{maximum} {elide(name, 64)}")
        QApplication.processEvents()
        return not dialog.wasCanceled()

    def _rebuild_entry_indexes(self) -> None:
        self.entry_by_path = {entry.abs_path: entry for entry in self.entries}
        self.search_cache = {entry.abs_path: entry.search_text for entry in self.entries}

    def _build_search_blob(self, entry: WildcardEntry) -> str:
        return entry.search_text

    def _rebuild_folder_tree(self) -> None:
        self._folder_tree_populating = True
        try:
            self.folder_tree.clear()
            self.folder_tree.setUpdatesEnabled(False)
            self.folder_tree.blockSignals(True)
            root = QTreeWidgetItem(["(all)"])
            root.setData(0, Qt.UserRole, "")
            root.setIcon(0, self.style().standardIcon(QStyle.SP_DirHomeIcon))
            root.setData(0, Qt.UserRole + 1, False)
            self.folder_tree.addTopLevelItem(root)
            self._populate_folder_children(root)
            self.folder_tree.expandItem(root)
            self._ensure_folder_path(self.current_folder_prefix, root)
            self.folder_tree.setCurrentItem(self._find_folder_item(self.current_folder_prefix) or root)
        finally:
            self.folder_tree.blockSignals(False)
            self.folder_tree.setUpdatesEnabled(True)
            self._folder_tree_populating = False

    def _folder_item_key(self, item: QTreeWidgetItem) -> str:
        return str(item.data(0, Qt.UserRole) or "")

    def _is_folder_item_populated(self, item: QTreeWidgetItem) -> bool:
        return bool(item.data(0, Qt.UserRole + 1))

    def _mark_folder_item_populated(self, item: QTreeWidgetItem) -> None:
        item.setData(0, Qt.UserRole + 1, True)

    def _populate_folder_children(self, item: QTreeWidgetItem) -> None:
        if self._is_folder_item_populated(item):
            return
        folder_key = self._folder_item_key(item)
        children = self.folder_children_map.get(folder_key, set())
        for child_name in sorted(children, key=natural_sort_key):
            child_key = child_name if not folder_key else f"{folder_key}/{child_name}"
            child = QTreeWidgetItem([child_name])
            child.setData(0, Qt.UserRole, child_key)
            child.setData(0, Qt.UserRole + 1, False)
            child.setIcon(0, self.style().standardIcon(QStyle.SP_DirIcon))
            item.addChild(child)
        self._mark_folder_item_populated(item)

    def _find_folder_item(self, folder_key: str) -> QTreeWidgetItem | None:
        if not folder_key:
            return self.folder_tree.topLevelItem(0)
        parts = folder_key.split("/")
        item = self.folder_tree.topLevelItem(0)
        if item is None:
            return None
        for part in parts:
            self._populate_folder_children(item)
            next_item = None
            for i in range(item.childCount()):
                child = item.child(i)
                if child.text(0) == part:
                    next_item = child
                    break
            if next_item is None:
                return None
            item = next_item
        return item

    def _ensure_folder_path(self, folder_key: str, root: QTreeWidgetItem | None = None) -> None:
        if not folder_key:
            return
        item = root or self.folder_tree.topLevelItem(0)
        if item is None:
            return
        for part in folder_key.split("/"):
            self._populate_folder_children(item)
            next_item = None
            for i in range(item.childCount()):
                child = item.child(i)
                if child.text(0) == part:
                    next_item = child
                    break
            if next_item is None:
                return
            item.setExpanded(True)
            item = next_item

    def _on_folder_item_expanded(self, item: QTreeWidgetItem) -> None:
        if self._folder_tree_populating:
            return
        self._populate_folder_children(item)

    def _on_folder_changed(self) -> None:
        item = self.folder_tree.currentItem()
        self.current_folder_prefix = item.data(0, Qt.UserRole) if item else ""
        self.folder_address.setText(self.current_folder_prefix or self.settings.library_root)
        self.schedule_apply_filters()

    def _show_folder_context_menu(self, pos) -> None:
        item = self.folder_tree.itemAt(pos)
        if item is None:
            return
        self.folder_tree.setCurrentItem(item)
        menu = QMenu(self)
        open_action = menu.addAction("エクスプローラーで開く")
        folder_key = item.data(0, Qt.UserRole) or ""
        delete_action = None
        if folder_key:
            delete_action = menu.addAction("このフォルダを削除")
        chosen = menu.exec(self.folder_tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == delete_action:
            self._delete_folder_from_tree(folder_key)
            return
        if chosen != open_action:
            return
        base = Path(self.settings.library_root)
        target = base if not folder_key else base / Path(folder_key.replace("/", "\\"))
        if target.exists():
            subprocess.Popen(["explorer.exe", str(target)])
        else:
            self._show_info("フォルダ", f"フォルダが見つかりません:\n{target}")

    def _delete_folder_from_tree(self, folder_key: str) -> None:
        if not folder_key:
            self._show_info("フォルダ削除", "保存先ルートは削除できません。")
            return
        result = self._ask_yes_no(
            "フォルダ削除",
            f"以下のフォルダを削除しますか？\n\n{folder_key}\n\n配下の wildcard と thumbnail もすべて削除されます。",
            default_button=QMessageBox.No,
        )
        if result != QMessageBox.Yes:
            return
        try:
            stats = self.repo.delete_folder(self.settings, folder_key)
            self.current_folder_prefix = ""
            self.rescan_library()
            self.statusBar().showMessage(
                f"フォルダを削除しました: wildcard={stats['wildcards']} thumbnail={stats['thumbnails']}",
                6000,
            )
        except Exception as exc:
            self._show_error("フォルダ削除エラー", str(exc))

    def schedule_apply_filters(self) -> None:
        self.filter_timer.start()

    def apply_filters(self) -> None:
        query = self.search_edit.text().strip().lower()
        selected_abs = self.current_entry.abs_path if self.current_entry else None
        if not self.entries:
            self.filtered_entries = []
            self.list_model.set_entries([])
            self._set_result_count_label(0)
            self._clear_detail()
            return

        def matches(entry: WildcardEntry) -> bool:
            if self.current_folder_prefix:
                if entry.folder != self.current_folder_prefix and not entry.folder.startswith(f"{self.current_folder_prefix}/"):
                    return False
            if not query:
                return True
            if query.startswith("lora:"):
                name = query[5:].strip()
                return any(name in item.lower() for item in entry.lora_names)
            return query in self.search_cache.get(entry.abs_path, "")

        filtered = self._sort_entries([entry for entry in self.entries if matches(entry)])
        self.filtered_entries = filtered
        self.list_model.set_entries(self.filtered_entries)
        self._set_result_count_label(len(self.filtered_entries))
        self._update_view_sizes()

        if self.filtered_entries:
            if selected_abs:
                target_index = None
                for pos, entry in enumerate(self.filtered_entries):
                    if entry.abs_path == selected_abs:
                        target_index = pos
                        break
                if target_index is not None:
                    self.card_view.setCurrentIndex(self.list_model.index(target_index, 0))
        else:
            self._clear_detail()

    def _clear_detail(self) -> None:
        self.current_entry = None
        self._set_edit_mode(False)
        self._path_label_text = "ファイル未選択"
        self._refresh_path_label()
        self._update_preview_pixmap()
        self.lora_list.clear()
        self.prompt_tags_edit.set_tags([])
        self.editor.clear()

    def _on_card_selection_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        entry = self.list_model.entry_at(current)
        if entry is None:
            return
        if not self._confirm_discard_or_save_if_needed():
            if previous.isValid():
                with QSignalBlocker(self.card_view.selectionModel()):
                    self.card_view.setCurrentIndex(previous)
            return
        self.current_entry = entry
        self._show_entry(entry)

    def _on_card_double_clicked(self, index: QModelIndex) -> None:
        entry = self.list_model.entry_at(index)
        if entry is None:
            return
        self.current_entry = entry
        self._show_entry(entry)
        self.copy_all_text(True)
        self.show_toast("Copied")

    def _show_entry(self, entry: WildcardEntry) -> None:
        self._set_edit_mode(False)
        self._path_label_text = entry.rel_path
        self._refresh_path_label()
        self.lora_list.clear()
        for name in entry.lora_names:
            self.lora_list.addItem(name)
        cached_content = self._content_load_cache.get(entry.abs_path)
        if cached_content is not None:
            entry.content = cached_content
            self.prompt_tags_edit.set_tags(parse_prompt_tags(cached_content))
            self.editor.setPlainText(cached_content)
        else:
            self.prompt_tags_edit.set_tags([])
            self.editor.setPlainText("読み込み中...")
            self._queue_entry_content_load(entry.abs_path)
        self._preview_cache_key = None
        self._update_preview_pixmap()

    def _queue_entry_content_load(self, abs_path: str) -> None:
        if not hasattr(self, "_content_load_signals"):
            self._content_load_signals = EntryContentLoadSignals(self)
            self._content_load_signals.loaded.connect(self._on_entry_content_loaded)
            self._content_load_signals.failed.connect(self._on_entry_content_failed)
            self._content_load_pool = QThreadPool.globalInstance()
        self._content_request_token += 1
        token_abs_path = abs_path
        self._pending_content_abs_path = token_abs_path
        self._content_load_pool.start(EntryContentLoadTask(abs_path, self.repo, self._content_load_signals), 0)

    @Slot(str, str)
    def _on_entry_content_loaded(self, abs_path: str, content: str) -> None:
        self._content_load_cache[abs_path] = content
        if self.current_entry is None or self.current_entry.abs_path != abs_path:
            return
        self.current_entry.content = content
        if self.editor.toPlainText() != content:
            with QSignalBlocker(self.editor):
                self.editor.setPlainText(content)
        self.prompt_tags_edit.set_tags(parse_prompt_tags(content))

    @Slot(str, str)
    def _on_entry_content_failed(self, abs_path: str, message: str) -> None:
        if self.current_entry is None or self.current_entry.abs_path != abs_path:
            return
        if self.editor.toPlainText() != "":
            with QSignalBlocker(self.editor):
                self.editor.setPlainText("")
        self.editor.setPlaceholderText(f"本文の読み込みに失敗しました: {message}")

    def _set_edit_mode(self, enabled: bool) -> None:
        self.is_edit_mode = enabled and self.current_entry is not None
        self.prompt_tags_edit.set_editable(self.is_edit_mode)
        self.editor.setReadOnly(not self.is_edit_mode)
        self.edit_button.setText("保存" if self.is_edit_mode else "編集")

    def toggle_edit_mode(self) -> None:
        if not self.current_entry:
            self._show_info("編集", "先に wildcard を選択してください。")
            return
        if self.is_edit_mode:
            if self.save_current_entry():
                self._set_edit_mode(False)
            return
        self._set_edit_mode(True)
        self.editor.setFocus()

    def _confirm_discard_or_save_if_needed(self) -> bool:
        if not self.is_edit_mode:
            return True
        result = self._ask_yes_no_cancel("内容編集", "保存していない変更があります。保存しますか？")
        if result == QMessageBox.Cancel:
            return False
        if result == QMessageBox.Yes:
            return self.save_current_entry()
        self._set_edit_mode(False)
        return True

    def _show_card_context_menu(self, pos) -> None:
        index = self.card_view.indexAt(pos)
        if not index.isValid():
            return
        self.card_view.setCurrentIndex(index)
        if not self.current_entry:
            return

        menu = QMenu(self)
        copy_all_action = menu.addAction("全文コピー LoRAあり")
        copy_all_without_action = menu.addAction("全文コピー LoRAなし")
        thumb_action = menu.addAction("サムネ生成")
        menu.addSeparator()
        import_copy_action = menu.addAction("この項目をコピー取り込み")
        import_move_action = menu.addAction("この項目を移動取り込み")
        if self.current_entry.lora_names:
            search_menu = menu.addMenu("LoRA で絞り込み")
            for name in self.current_entry.lora_names:
                action = search_menu.addAction(name)
                action.triggered.connect(lambda checked=False, n=name: self.search_edit.setText(f"lora:{n}"))

        action = menu.exec(self.card_view.viewport().mapToGlobal(pos))
        if action == copy_all_action:
            self.copy_all_text(True)
        elif action == copy_all_without_action:
            self.copy_all_text(False)
        elif action == thumb_action:
            self.generate_thumbnail_for_current()
        elif action == import_copy_action:
            self.run_single_import("copy")
        elif action == import_move_action:
            self.run_single_import("move")

    def _show_lora_context_menu(self, pos) -> None:
        item = self.lora_list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        filter_action = menu.addAction("この LoRA で検索")
        action = menu.exec(self.lora_list.viewport().mapToGlobal(pos))
        if action == filter_action:
            self.search_edit.setText(f"lora:{item.text()}")

    def _sync_editor_from_prompt_tags(self) -> None:
        text = ", ".join(self.prompt_tags_edit.tags())
        if self.editor.toPlainText() == text:
            return
        with QSignalBlocker(self.editor):
            self.editor.setPlainText(text)

    def save_current_entry(self) -> bool:
        if not self.current_entry:
            self._show_info("保存", "先に wildcard を選択してください。")
            return False
        try:
            content = ", ".join(self.prompt_tags_edit.tags()).strip()
            updated = self.repo.save_entry(self.settings, self.current_entry, content, [])
            self._replace_entry(updated)
            self.statusBar().showMessage("保存しました", 4000)
            return True
        except Exception as exc:
            self._show_error("保存エラー", str(exc))
            return False

    def _replace_entry(self, updated: WildcardEntry) -> None:
        for index, entry in enumerate(self.entries):
            if entry.abs_path == updated.abs_path:
                self.entries[index] = updated
                break
        self.current_entry = updated
        self._set_edit_mode(False)
        self._rebuild_entry_indexes()
        self.list_model.clear_cache_for_path(updated.abs_path)
        self.apply_filters()
        self._show_entry(updated)

    def _prepare_copy_text(self, text: str, include_lora: bool) -> str:
        return text.strip() if include_lora else strip_lora_tags(text)

    def copy_all_text(self, include_lora: bool | None = None) -> None:
        if not self.current_entry:
            return
        include = self.settings.include_lora_on_copy if include_lora is None else include_lora
        text = self._prepare_copy_text(self.editor.toPlainText(), include)
        QApplication.clipboard().setText(text, QClipboard.Clipboard)
        self.statusBar().showMessage(f"全文をコピーしました（{'LoRAあり' if include else 'LoRAなし'}）", 3000)

    def show_toast(self, message: str) -> None:
        if self.toast_popup is not None:
            self.toast_popup.close()
        self.toast_popup = ToastPopup(self, message)
        self.toast_popup.show_for()

    def _advance_thumbnail_loading_animation(self) -> None:
        if not self.thumbnail_loading_message:
            return
        frame = self.thumbnail_loading_frames[self.thumbnail_loading_frame_index % len(self.thumbnail_loading_frames)]
        self.thumbnail_loading_frame_index += 1
        self.thumbnail_label.setPixmap(QPixmap())
        self.thumbnail_label.setText(f"{frame}\n{self.thumbnail_loading_message}")

    def _start_thumbnail_loading_feedback(self, message: str) -> None:
        self.thumbnail_loading_message = message
        self.thumbnail_loading_frame_index = 0
        self.thumbnail_loading_timer.start()
        self._advance_thumbnail_loading_animation()

    def _stop_thumbnail_loading_feedback(self) -> None:
        self.thumbnail_loading_timer.stop()
        self.thumbnail_loading_message = ""

    def _cleanup_thumbnail_generation_worker(self) -> None:
        if self.thumbnail_generation_thread is not None:
            self.thumbnail_generation_thread.quit()
            self.thumbnail_generation_thread.wait(2000)
            self.thumbnail_generation_thread.deleteLater()
        if self.thumbnail_generation_worker is not None:
            self.thumbnail_generation_worker.deleteLater()
        self.thumbnail_generation_thread = None
        self.thumbnail_generation_worker = None

    def _on_thumbnail_generation_finished(self, updated: WildcardEntry, filename: str) -> None:
        self._stop_thumbnail_loading_feedback()
        self._replace_entry(updated)
        self.statusBar().showMessage(f"サムネイルを生成しました: {filename}", 5000)
        self._cleanup_thumbnail_generation_worker()

    def _on_thumbnail_generation_failed(self, message: str) -> None:
        self._stop_thumbnail_loading_feedback()
        self._show_error("サムネイル生成エラー", message)
        if self.current_entry is not None:
            self._show_entry(self.current_entry)
        self._cleanup_thumbnail_generation_worker()

    def _choose_import_conflict_policy(self, title: str, message: str) -> str | None:
        dialog = ImportOptionsDialog(title, message, self)
        QTimer.singleShot(0, lambda: self._center_window(dialog))
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.conflict_policy

    def run_import(self, mode: str) -> None:
        title = "移動取り込み" if mode == "move" else "コピー取り込み"
        message = "選択した wildcard ファイルをライブラリへ移動します。" if mode == "move" else "選択した wildcard ファイルをライブラリへコピーします。"
        conflict_policy = self._choose_import_conflict_policy(title, "一括取り込み時の重複ファイルの扱いを選んでください。")
        if not conflict_policy:
            return
        inspection = self.repo.inspect_source_thumbnail_roots(self.settings)
        if not inspection["configured_exists"] and not inspection["existing_roots"]:
            self._show_warning(
                title,
                "設定されたサムネイル取込元ルートが見つかりません。\n"
                "wildcard 本体は取り込みできますが、サムネイルは取り込まれません。\n\n"
                f"設定値:\n{inspection['configured_root']}",
            )
        elif not inspection["configured_exists"] and inspection["existing_roots"]:
            self._show_info(
                title,
                "設定されたサムネイル取込元ルートは見つかりませんでしたが、\n"
                "代わりに使えそうな場所があります。\n\n"
                f"設定値:\n{inspection['configured_root']}\n\n"
                f"使用候補:\n{inspection['existing_roots'][0]}",
            )
        if self._ask_yes_no(title, message) != QMessageBox.Yes:
            return

        progress = self._create_progress_dialog(title, f"{title} 螳溯｡御ｸｭ...", can_cancel=True)
        try:
            stats = self.repo.import_from_sources(
                self.settings,
                mode=mode,
                conflict_policy=conflict_policy,
                progress=lambda i, total, name: self._step_progress(progress, i, total, name),
            )
            self.rescan_library()
            self.statusBar().showMessage(
                f"{title} 取り込み完了 wildcard={stats['imported']} thumbnail={stats['thumbnails']} skipped={stats['skipped']}",
                6000,
            )
            if stats["thumbnail_source_roots"] == 0:
                self._show_warning(
                    title,
                    "設定されたサムネイル取込元パスが見つかりません。\n"
                    "サムネイルは 0 件でした。取込元パスを確認してください。",
                )
        except OperationCancelledError:
            self.statusBar().showMessage(f"{title} cancelled", 5000)
        except Exception as exc:
            self._show_error("取り込みエラー", str(exc))
        finally:
            progress.close()

    def run_single_import(self, mode: str) -> None:
        if not self.current_entry:
            self._show_info("個別取り込み", "先に wildcard を選択してください。")
            return
        title = "移動取り込み" if mode == "move" else "コピー取り込み"
        conflict_policy = self._choose_import_conflict_policy(
            title,
            f"この項目だけを取り込みますか？\n\n{self.current_entry.rel_path}\n\n既存ファイルの扱いを選んでください。",
        )
        if not conflict_policy:
            return
        try:
            stats = self.repo.import_single_from_sources(
                self.settings,
                Path(self.current_entry.rel_path),
                mode=mode,
                conflict_policy=conflict_policy,
            )
            self.rescan_library()
            self.statusBar().showMessage(
                f"{title} 取り込み完了 wildcard={stats['imported']} thumbnail={stats['thumbnails']} skipped={stats['skipped']}",
                6000,
            )
        except Exception as exc:
            self._show_error("取り込みエラー", str(exc))

    def test_api_connection(self) -> None:
        try:
            self.api.test_connection(self.settings)
            self._show_info("API", "接続できました")
        except Exception as exc:
            self._show_error("API", str(exc))

    def generate_thumbnail_for_current(self) -> None:
        if not self.current_entry:
            self._show_info("サムネ生成", "先に wildcard を選択してください。")
            return
        if self.thumbnail_generation_thread is not None and self.thumbnail_generation_thread.isRunning():
            self._show_info("サムネ生成", "別のサムネ生成が実行中です。")
            return
        self._start_thumbnail_loading_feedback("Generating thumbnail...\nPreparing image...")
        self.statusBar().showMessage("Generating thumbnail...", 0)
        self.thumbnail_generation_thread = QThread(self)
        self.thumbnail_generation_worker = SingleThumbnailWorker(self.app_dir / "wildcard_manager.db", self.settings, self.current_entry)
        self.thumbnail_generation_worker.moveToThread(self.thumbnail_generation_thread)
        self.thumbnail_generation_thread.started.connect(self.thumbnail_generation_worker.run)
        self.thumbnail_generation_worker.finished.connect(self._on_thumbnail_generation_finished)
        self.thumbnail_generation_worker.failed.connect(self._on_thumbnail_generation_failed)
        self.thumbnail_generation_thread.start()

    def generate_missing_thumbnails(self) -> None:
        targets = [entry for entry in self.filtered_entries if not entry.has_thumbnail]
        if not targets:
            self._show_info("不足分サムネ生成", "不足しているサムネイルはありません。")
            return
        if self._ask_yes_no("不足分サムネ生成", f"{len(targets)} 件のサムネイルを生成しますか？") != QMessageBox.Yes:
            return

        progress = self._create_progress_dialog("不足分サムネ生成", "サムネイルを生成中...", maximum=len(targets), can_cancel=True)

        done = 0
        try:
            self.statusBar().showMessage("サムネイルを生成中...", 0)
            for index, entry in enumerate(targets, start=1):
                if progress.wasCanceled():
                    raise OperationCancelledError()
                progress.setValue(index)
                progress.setLabelText(f"[{(index % 4) * '=':<3}] 生成中 {index}/{len(targets)} {elide(entry.name, 64)}")
                QApplication.processEvents()
                self.api.generate_thumbnail(self.settings, entry)
                refreshed = self.repo.refresh_entry(self.settings, Path(entry.abs_path))
                for pos, existing in enumerate(self.entries):
                    if existing.abs_path == refreshed.abs_path:
                        self.entries[pos] = refreshed
                        break
                done += 1
            self._rebuild_entry_indexes()
            self.apply_filters()
            self.statusBar().showMessage(f"{done} 件のサムネイルを生成しました", 6000)
        except OperationCancelledError:
            self.statusBar().showMessage(f"サムネイル生成をキャンセルしました。完了 {done} 件", 5000)
        except Exception as exc:
            self._show_error("不足分サムネ生成エラー", f"{done} 件まで完了しました。\n{exc}")
        finally:
            progress.close()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_top_controls_layout()
        self._refresh_path_label()
        if self.current_entry is not None:
            self._update_preview_pixmap()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._confirm_discard_or_save_if_needed():
            event.ignore()
            return
        # Let outstanding thumbnail/content jobs finish cleanly so they do not
        # emit into deleted QObject signal sources during shutdown.
        QThreadPool.globalInstance().waitForDone(2000)
        if self.cached_load_thread is not None and self.cached_load_thread.isRunning():
            self.cached_load_thread.quit()
            self.cached_load_thread.wait(2000)
        self._stop_thumbnail_loading_feedback()
        self._cleanup_thumbnail_generation_worker()
        normal = self.normalGeometry()
        self.settings.window_maximized = self.isMaximized()
        if normal.isValid():
            self.settings.window_width = normal.width()
            self.settings.window_height = normal.height()
        else:
            self.settings.window_width = self.width()
            self.settings.window_height = self.height()
        self.settings.splitter_sizes = self.splitter.sizes()
        self.settings.detail_splitter_sizes = [420, 520]
        self.settings.last_folder = self.current_folder_prefix
        self.settings.sort_mode = self.sort_combo.currentData()
        self.ui_state_store.save(self.settings)
        super().closeEvent(event)
