from __future__ import annotations

import base64
import copy
import io
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import random
import uuid

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from PySide6.QtCore import QFile, QModelIndex, QObject, QPoint, QRect, QRectF, QSignalBlocker, QSize, Qt, QThread, QThreadPool, QTimer, Signal, Slot, QEvent, QFileSystemWatcher
from PySide6.QtGui import QAction, QActionGroup, QClipboard, QCloseEvent, QColor, QFont, QFontMetrics, QIcon, QImage, QKeySequence, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLayout,
    QGroupBox,
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
    QRadioButton,
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
from .repository import CONFLICT_POLICIES, OperationCancelledError, WildcardRepository, sidecar_metadata_path, strip_lora_tags, parse_prompt_tags
from .thumbnail_model import ThumbnailListModel, ThumbnailItemDelegate, ThumbnailCardDelegate, CARD_ICON_RATIO, CARD_TITLE_HEIGHT, CARD_FRAME_PADDING
from .thumbnail_workers import ThumbnailLoadSignals, ThumbnailLoadTask, EntryContentLoadSignals, EntryContentLoadTask, PreviewLoadSignals, PreviewLoadTask
from .tag_editor import FlowLayout, TagChip, TagEditorWidget
from .new_wildcard_dialog import NewWildcardDialog, PREVIEW_THUMB_SUBDIR
from .single_thumbnail_worker import SingleThumbnailWorker

SORT_LABELS = {
    "name": "ファイル名",
    "size": "サイズ",
    "created": "作成日順",
    "updated": "更新日順",
    "path": "パス順",
    "folder": "フォルダ順",
}


def _setup_japanese_context_menu(widget):
    """QLineEdit/QTextEdit/QPlainTextEdit のコンテキストメニューを日本語化（イベントフィルター方式）

    以前の実装は「削除」アクションに ``w.deleteLater`` を渡しており、
    右クリック→削除 で入力フィールド自体が消える重大バグがあった。
    ui_utils.setup_japanese_context_menu に処理を委譲する。
    """
    from .ui_utils import setup_japanese_context_menu as _impl
    _impl(widget)

THUMBNAIL_SIZE_MIN = 80
THUMBNAIL_SIZE_MAX = 720
THUMBNAIL_SIZE_STEP = 16
UI_FONT_FAMILIES = ["Yu Gothic UI", "Yu Gothic", "Meiryo UI", "Meiryo", "Segoe UI", "Noto Sans CJK JP", "MS Gothic"]
UI_OUTER_MARGIN = 12
UI_SECTION_SPACING = 12
UI_CARD_MARGIN_TOP = 12
UI_CARD_MARGIN_X = 12
UI_CARD_PADDING_X = 14
UI_CARD_PADDING_Y = 12
UI_RADIUS = 10
UI_CONTROL_HEIGHT = 32
UI_BUTTON_HEIGHT = 30
UI_BUTTON_PADDING_X = 8
UI_BUTTON_PADDING_Y = 2
TOOLBAR_BUTTON_SIZE = 32
TOOLBAR_HEIGHT = 36


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event) -> None:
        event.ignore()


class ApiStatusIndicator(QWidget):
    """API接続状態を示すミニマルインジケーター。「API ●」形式。"""

    _LAMP_COLORS = {
        "off":        "#E74C3C",
        "connected":  "#2ECC71",
        "generating": "#F1C40F",
    }
    _STATE_TOOLTIPS = {
        "off":        "API: 切断",
        "connected":  "API: 接続済",
        "generating": "API: 生成中",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "off"
        self.setStyleSheet("""
            QFrame#apiIndicator {
                background: #1a1d22;
                border: 1px solid #2a3440;
                border-radius: 4px;
            }
        """)
        container = QFrame()
        container.setObjectName("apiIndicator")
        container.setFixedHeight(TOOLBAR_BUTTON_SIZE)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(6)

        self._label = QLabel("API")
        self._label.setStyleSheet("color: #E0E0E0; font: 600 11px 'Segoe UI';")
        layout.addWidget(self._label)

        self._lamp = QLabel()
        self._lamp.setFixedSize(10, 10)
        self._lamp.setStyleSheet(f"background: {self._LAMP_COLORS['off']}; border-radius: 5px;")
        layout.addWidget(self._lamp)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(container)
        self.setToolTip(self._STATE_TOOLTIPS["off"])

        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(500)
        self._blink_timer.timeout.connect(self._blink)
        self._blink_on = True

    def set_state(self, state: str) -> None:
        # L7 修正: 未知の state で KeyError にならないよう .get でフォールバック。
        if self._state == state:
            return
        self._state = state
        self.setToolTip(self._STATE_TOOLTIPS.get(state, state))
        if state == "generating":
            self._blink_on = True
            self._lamp.setStyleSheet(f"background: {self._LAMP_COLORS['generating']}; border-radius: 5px;")
            self._blink_timer.start()
        else:
            self._blink_timer.stop()
            color = self._LAMP_COLORS.get(state, self._LAMP_COLORS["off"])
            self._lamp.setStyleSheet(f"background: {color}; border-radius: 5px;")

    def _blink(self) -> None:
        self._blink_on = not self._blink_on
        if self._blink_on:
            self._lamp.setStyleSheet(f"background: {self._LAMP_COLORS['generating']}; border-radius: 5px;")
        else:
            self._lamp.setStyleSheet("background: #1E1E1E; border-radius: 5px;")


class ResponsivePixmapLabel(QLabel):
    """リサイズ対応・角丸表示のサムネイルラベル。

    ユーザー作業分: super().setPixmap() を呼ばず paintEvent を完全自前実装することで
    QLabel 内部の QPainter 再入問題を回避しつつ、角丸クリッピングを実現する。
    ピクスマップなし時は通常の QLabel テキスト描画にフォールバックする。
    """
    _CORNER_RADIUS = 12

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._base_pixmap: QPixmap | None = None

    def setPixmap(self, pixmap: QPixmap | None) -> None:  # type: ignore[override]
        self._base_pixmap = pixmap
        self.update()  # paintEvent をスケジュール（super().setPixmap は呼ばない）

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update()

    def paintEvent(self, event) -> None:
        if self._base_pixmap is None or self._base_pixmap.isNull():
            # ピクスマップなし → QLabel のテキスト描画に委譲
            super().paintEvent(event)
            return
        cr = self.contentsRect()
        if cr.width() <= 0 or cr.height() <= 0:
            return
        scaled = self._base_pixmap.scaled(cr.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        # センタリング
        x = cr.x() + (cr.width() - scaled.width()) // 2
        y = cr.y() + (cr.height() - scaled.height()) // 2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, scaled.width(), scaled.height()), self._CORNER_RADIUS, self._CORNER_RADIUS)
        painter.setClipPath(path)
        painter.drawPixmap(x, y, scaled)
        painter.end()


class SlowWheelListView(QListView):
    """QListView whose mouse-wheel scroll step is reduced for finer scrolling."""

    def __init__(self, scroll_step: int = 30, parent=None):
        super().__init__(parent)
        self.scroll_step = scroll_step

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        bar = self.verticalScrollBar()
        notches = delta / 120.0
        bar.setValue(bar.value() - int(notches * self.scroll_step))
        event.accept()


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


def collect_txt_files(root: Path, include_subfolders: bool) -> list[Path]:
    if not root.exists():
        return []
    files = root.rglob("*.txt") if include_subfolders else root.glob("*.txt")
    return sorted((path for path in files if path.is_file()), key=lambda path: path.as_posix().lower())


def extract_prompt_line(text: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            return line
    return ""



class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, repo: WildcardRepository, api: ThumbnailApiClient, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.api = api
        self.setWindowTitle("設定")
        self.resize(1120, 820)
        self.setMinimumSize(1040, 760)
        self.setStyleSheet(
            f"""
            QDialog {{
                background: #0f1216;
                color: #edf2f7;
            }}
            QLabel {{
                color: #edf2f7;
            }}
            QGroupBox {{
                border: 1px solid #2a3440;
                border-radius: {UI_RADIUS}px;
                margin-top: {UI_CARD_MARGIN_TOP}px;
                color: #edf2f7;
                font-weight: 600;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: {UI_CARD_MARGIN_X}px;
                padding: 0 4px;
            }}
            QTabWidget::pane {{
                border: 1px solid #2a3440;
                border-radius: {UI_RADIUS}px;
                top: -1px;
                background: #0f1216;
            }}
            QTabBar::tab {{
                background: #151a21;
                color: #dce5ef;
                border: 1px solid #2a3440;
                border-bottom: none;
                border-top-left-radius: {UI_RADIUS}px;
                border-top-right-radius: {UI_RADIUS}px;
                padding: 7px 14px;
                margin-right: 4px;
            }}
            QTabBar::tab:selected {{
                background: #1d2630;
                color: #ffffff;
            }}
            QLineEdit, QTextEdit, QPlainTextEdit, QListWidget, QTreeWidget, QListView, QComboBox, QSpinBox {{
                background: #161b22;
                border: 1px solid #2a3440;
                border-radius: {UI_RADIUS}px;
                color: #edf2f7;
                padding: 6px 10px;
                min-height: {UI_CONTROL_HEIGHT}px;
            }}
            QPushButton {{
                background: #2e5fb8;
                border: 1px solid #2e5fb8;
                border-radius: {UI_RADIUS}px;
                color: white;
                padding: 0px {UI_BUTTON_PADDING_X}px;
                min-height: {UI_BUTTON_HEIGHT}px;
                max-height: {UI_BUTTON_HEIGHT}px;
                height: {UI_BUTTON_HEIGHT}px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: #3a71c6;
                border-color: #3a71c6;
            }}
            """
        )

        self.library_root = self._path_row(settings.library_root)
        self.thumbnail_root = self._path_row(settings.thumbnail_root)
        self.thumbnail_root_mgmt = self._path_row(settings.thumbnail_root)
        # 両行を双方向同期
        self.thumbnail_root["line_edit"].textChanged.connect(
            lambda t: self.thumbnail_root_mgmt["line_edit"].setText(t)
            if self.thumbnail_root_mgmt["line_edit"].text() != t else None
        )
        self.thumbnail_root_mgmt["line_edit"].textChanged.connect(
            lambda t: self.thumbnail_root["line_edit"].setText(t)
            if self.thumbnail_root["line_edit"].text() != t else None
        )
        self.source_wildcard_root = self._path_row(settings.source_wildcard_root)
        self.source_thumbnail_root = self._path_row(settings.source_thumbnail_root)

        self.lora_root = self._path_row(settings.lora_root)

        self.api_base_url = QLineEdit(settings.api_base_url)
        self.api_test_button = QPushButton("接続確認")
        self.api_test_button.clicked.connect(self._test_api_connection)
        self.api_test_button.setFixedHeight(UI_BUTTON_HEIGHT)
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
        self.generation_adetailer_enabled = QCheckBox("ADetailer を使う")
        self.generation_adetailer_enabled.setChecked(settings.generation_adetailer_enabled)
        self.thumbnail_size = QLineEdit(str(settings.thumbnail_size))
        self.random_output_root = self._path_row(settings.random_output_root)
        self.random_concept_root = self._path_row(settings.random_concept_root)
        self.random_character_root = self._path_row(settings.random_character_root)
        self.random_concept_scope = NoWheelComboBox()
        self.random_concept_scope.addItem("直下のみ", False)
        self.random_concept_scope.addItem("サブフォルダ含む", True)
        self.random_concept_scope.setCurrentIndex(1 if settings.random_concept_include_subfolders else 0)
        self.random_character_scope = NoWheelComboBox()
        self.random_character_scope.addItem("直下のみ", False)
        self.random_character_scope.addItem("サブフォルダ含む", True)
        self.random_character_scope.setCurrentIndex(1 if settings.random_character_include_subfolders else 0)
        self.random_prompt = QPlainTextEdit(settings.random_prompt)
        self.random_prompt.setPlaceholderText("ランダム生成の固定プロンプト")
        self.random_prompt.setMaximumHeight(100)
        self.random_negative_prompt = QPlainTextEdit(settings.random_negative_prompt)
        self.random_negative_prompt.setPlaceholderText("ネガティブプロンプト")
        self.random_negative_prompt.setMaximumHeight(100)
        self.random_width = QLineEdit(str(settings.random_width))
        self.random_height = QLineEdit(str(settings.random_height))
        self.random_steps = QLineEdit(str(settings.random_steps))
        self.random_batch_count = QLineEdit(str(settings.random_batch_count))
        self.random_batch_size = QLineEdit(str(settings.random_batch_size))
        self.random_cfg_scale = QLineEdit(str(settings.random_cfg_scale))
        self.random_sampler_name = QLineEdit(settings.random_sampler_name)
        self.random_seed_mode = NoWheelComboBox()
        self.random_seed_mode.addItem("ランダム", "random")
        self.random_seed_mode.addItem("固定", "fixed")
        self.random_seed_mode.setCurrentIndex(0 if settings.random_seed_mode != "fixed" else 1)
        self.random_seed = QLineEdit(str(settings.random_seed))
        self.random_checkpoint_name = NoWheelComboBox()
        self.random_checkpoint_name.setEditable(True)
        self.random_checkpoint_name.setInsertPolicy(QComboBox.NoInsert)
        self.random_checkpoint_name.addItem(settings.random_checkpoint_name or "")
        self.random_checkpoint_name.setCurrentText(settings.random_checkpoint_name)
        self.random_refresh_checkpoints_button = QPushButton("候補更新")
        self.random_refresh_checkpoints_button.clicked.connect(self._refresh_random_checkpoint_list)
        self.random_refresh_checkpoints_button.setFixedHeight(UI_BUTTON_HEIGHT)
        self.random_checkpoint_status = QLabel("API から候補を取得できます")
        self.random_checkpoint_status.setWordWrap(True)
        self.random_checkpoint_status.setStyleSheet("color: #e7b65c;")
        self.random_adetailer_enabled = QCheckBox("ADetailer を使う")
        self.random_adetailer_enabled.setChecked(settings.random_adetailer_enabled)

        for widget in (
            self.api_base_url,
            self.api_timeout_sec,
            self.generation_width,
            self.generation_height,
            self.generation_steps,
            self.generation_cfg_scale,
            self.generation_sampler_name,
            self.thumbnail_size,
            self.random_width,
            self.random_height,
            self.random_steps,
            self.random_batch_count,
            self.random_batch_size,
            self.random_cfg_scale,
            self.random_sampler_name,
            self.random_seed,
        ):
            self._style_line_field(widget)
        for widget in (
            self.generation_prompt_prefix,
            self.generation_negative_prompt,
            self.random_prompt,
            self.random_negative_prompt,
        ):
            self._style_text_field(widget)
        for widget in (
            self.random_seed_mode,
            self.random_checkpoint_name,
            self.random_concept_scope,
            self.random_character_scope,
        ):
            self._style_combo_field(widget)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs, 1)

        common_tab = QWidget()
        common_layout = QVBoxLayout(common_tab)
        common_layout.setContentsMargins(0, 0, 0, 0)
        common_layout.setSpacing(UI_SECTION_SPACING)
        common_scroll = QScrollArea()
        common_scroll.setWidgetResizable(True)
        common_scroll.setFrameShape(QFrame.NoFrame)
        common_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        common_content = QWidget()
        common_content_layout = QVBoxLayout(common_content)
        common_content_layout.setContentsMargins(0, 0, 0, 0)
        common_content_layout.setSpacing(UI_SECTION_SPACING)
        api_box, common_form = self._section_form("接続")
        api_url_row = QWidget()
        api_url_layout = QHBoxLayout(api_url_row)
        api_url_layout.setContentsMargins(0, 0, 0, 0)
        api_url_layout.setSpacing(UI_SECTION_SPACING)
        api_url_layout.addWidget(self.api_base_url, 1)
        api_url_layout.addWidget(self.api_test_button, 0)
        common_form.addRow("APIベースURL", api_url_row)
        common_form.addRow("APIタイムアウト", self.api_timeout_sec)
        common_content_layout.addWidget(api_box)

        display_box, display_form = self._section_form("表示")
        display_form.addRow("一覧サムネイルサイズ", self.thumbnail_size)
        common_content_layout.addWidget(display_box)

        lora_box, lora_form = self._section_form("LoRAフォルダ")
        lora_form.addRow("LoRAフォルダ", self.lora_root["widget"])
        common_content_layout.addWidget(lora_box)

        common_content_layout.addStretch(1)
        common_scroll.setWidget(common_content)
        common_layout.addWidget(common_scroll, 1)
        self.tabs.addTab(common_tab, "共通設定")

        management_tab = QWidget()
        management_layout = QVBoxLayout(management_tab)
        management_layout.setContentsMargins(0, 0, 0, 0)
        management_layout.setSpacing(UI_SECTION_SPACING)
        management_box, management_form = self._section_form("保存先と取込元")
        management_form.addRow("保存先ワイルドカード", self.library_root["widget"])
        management_form.addRow("保存先サムネイル", self.thumbnail_root_mgmt["widget"])
        management_form.addRow("取込元ワイルドカード", self.source_wildcard_root["widget"])
        management_form.addRow("取込元サムネイル", self.source_thumbnail_root["widget"])
        self.source_thumbnail_status = QLabel()
        self.source_thumbnail_status.setWordWrap(True)
        self.source_thumbnail_status.setStyleSheet("color: #e7b65c;")
        self.source_thumbnail_status.setMinimumHeight(72)
        self.source_thumbnail_status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.source_thumbnail_status.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        management_form.addRow("サムネイル確認", self.source_thumbnail_status)
        management_layout.addWidget(management_box)
        management_layout.addStretch(1)
        self.tabs.addTab(management_tab, "ワイルドカード管理設定")

        thumbnail_tab = QWidget()
        thumbnail_layout = QVBoxLayout(thumbnail_tab)
        thumbnail_layout.setContentsMargins(0, 0, 0, 0)
        thumbnail_layout.setSpacing(UI_SECTION_SPACING)
        thumbnail_scroll = QScrollArea()
        thumbnail_scroll.setWidgetResizable(True)
        thumbnail_scroll.setFrameShape(QFrame.NoFrame)
        thumbnail_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        thumbnail_content = QWidget()
        thumbnail_content_layout = QVBoxLayout(thumbnail_content)
        thumbnail_content_layout.setContentsMargins(0, 0, 0, 0)
        thumbnail_content_layout.setSpacing(UI_SECTION_SPACING)
        thumbnail_box, thumbnail_form = self._section_form("サムネイルの生成")
        thumbnail_form.addRow("サムネイル保存先", self.thumbnail_root["widget"])
        thumbnail_form.addRow("生成プロンプト", self.generation_prompt_prefix)
        thumbnail_form.addRow("ネガティブプロンプト", self.generation_negative_prompt)
        thumbnail_form.addRow("幅", self.generation_width)
        thumbnail_form.addRow("高さ", self.generation_height)
        thumbnail_form.addRow("ステップ数", self.generation_steps)
        thumbnail_form.addRow("CFG", self.generation_cfg_scale)
        thumbnail_form.addRow("サンプラー", self.generation_sampler_name)
        thumbnail_form.addRow(self.generation_adetailer_enabled)
        thumbnail_content_layout.addWidget(thumbnail_box)

        random_wc_box, random_wc_form = self._section_form("ランダムワイルドカード")
        self.thumbnail_random_wc_root = self._path_row(settings.thumbnail_random_wildcard_root)
        random_wc_form.addRow("ワイルドカードフォルダ", self.thumbnail_random_wc_root["widget"])
        thumbnail_content_layout.addWidget(random_wc_box)

        thumbnail_content_layout.addStretch(1)
        thumbnail_scroll.setWidget(thumbnail_content)
        thumbnail_layout.addWidget(thumbnail_scroll, 1)
        self.tabs.addTab(thumbnail_tab, "サムネイル設定")

        random_tab = QWidget()
        random_layout = QVBoxLayout(random_tab)
        random_layout.setContentsMargins(0, 0, 0, 0)
        random_layout.setSpacing(UI_SECTION_SPACING)
        random_scroll = QScrollArea()
        random_scroll.setWidgetResizable(True)
        random_scroll.setFrameShape(QFrame.NoFrame)
        random_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        random_content = QWidget()
        random_content_layout = QVBoxLayout(random_content)
        random_content_layout.setContentsMargins(0, 0, 0, 0)
        random_content_layout.setSpacing(UI_SECTION_SPACING)

        random_note = QLabel(
            "ここはランダム生成画面の初期値だけを保存します。実際の生成条件はランダム生成画面でその都度調整します。"
        )
        random_note.setWordWrap(True)

        random_source_box, random_source_form = self._section_form("初期フォルダ")
        random_source_form.addRow("保存先", self.random_output_root["widget"])
        random_source_form.addRow("ワイルドカードフォルダ", self.random_concept_root["widget"])
        random_source_form.addRow("サブフォルダ含む", self.random_concept_scope)

        random_content_layout.addWidget(random_note)
        random_content_layout.addWidget(random_source_box)
        random_content_layout.addStretch(1)
        random_scroll.setWidget(random_content)
        random_layout.addWidget(random_scroll, 1)
        self.tabs.addTab(random_tab, "ランダム生成の初期値")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.source_thumbnail_root["line_edit"].textChanged.connect(self._refresh_source_thumbnail_status)
        self.source_wildcard_root["line_edit"].textChanged.connect(self._refresh_source_thumbnail_status)
        self._refresh_source_thumbnail_status()

        # コンテキストメニューを日本語化
        _setup_japanese_context_menu(self.api_base_url)
        _setup_japanese_context_menu(self.api_timeout_sec)
        _setup_japanese_context_menu(self.generation_prompt_prefix)
        _setup_japanese_context_menu(self.generation_negative_prompt)
        _setup_japanese_context_menu(self.generation_width)
        _setup_japanese_context_menu(self.generation_height)
        _setup_japanese_context_menu(self.generation_steps)
        _setup_japanese_context_menu(self.generation_cfg_scale)
        _setup_japanese_context_menu(self.generation_sampler_name)
        _setup_japanese_context_menu(self.thumbnail_size)
        _setup_japanese_context_menu(self.random_width)
        _setup_japanese_context_menu(self.random_height)
        _setup_japanese_context_menu(self.random_steps)
        _setup_japanese_context_menu(self.random_batch_count)
        _setup_japanese_context_menu(self.random_batch_size)
        _setup_japanese_context_menu(self.random_cfg_scale)
        _setup_japanese_context_menu(self.random_sampler_name)
        _setup_japanese_context_menu(self.random_seed)
        _setup_japanese_context_menu(self.random_prompt)
        _setup_japanese_context_menu(self.random_negative_prompt)

    def set_current_tab(self, index: int) -> None:
        if 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)

    def _section_form(self, title: str) -> tuple[QGroupBox, QFormLayout]:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.DontWrapRows)
        form.setContentsMargins(UI_CARD_PADDING_X, UI_CARD_PADDING_Y, UI_CARD_PADDING_X, UI_CARD_PADDING_Y)
        form.setHorizontalSpacing(UI_SECTION_SPACING)
        form.setVerticalSpacing(10)
        return box, form

    def _path_row(self, value: str) -> dict[str, QWidget]:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        line_edit = QLineEdit(value)
        line_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        _setup_japanese_context_menu(line_edit)
        button = QPushButton("参照")
        button.setFixedWidth(72)
        button.setFixedHeight(UI_BUTTON_HEIGHT)
        button.clicked.connect(lambda: self._pick_directory(line_edit))
        layout.addWidget(line_edit)
        layout.addWidget(button)
        return {"widget": row, "line_edit": line_edit}

    def _style_line_field(self, widget: QLineEdit) -> None:
        widget.setMinimumHeight(UI_CONTROL_HEIGHT)
        widget.setStyleSheet(
            """
            QLineEdit {
                background: #1a1d22;
                border: 1px solid #2d3640;
                border-radius: 10px;
                color: #eef2f7;
                padding: 6px 10px;
                font-size: 13px;
            }
            """
        )

    def _style_text_field(self, widget: QPlainTextEdit) -> None:
        widget.setMinimumHeight(84)
        widget.setStyleSheet(
            """
            QPlainTextEdit {
                background: #1a1d22;
                border: 1px solid #2d3640;
                border-radius: 10px;
                color: #eef2f7;
                padding: 8px 10px;
                font-size: 13px;
            }
            """
        )

    def _style_combo_field(self, widget: QComboBox) -> None:
        widget.setMinimumHeight(UI_CONTROL_HEIGHT)
        widget.setStyleSheet(
            """
            QComboBox {
                background: #1a1d22;
                border: 1px solid #2d3640;
                border-radius: 10px;
                color: #eef2f7;
                padding: 4px 10px;
                font-size: 13px;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            QComboBox QAbstractItemView {
                background: #1a1d22;
                color: #eef2f7;
                selection-background-color: #2b6cb0;
            }
            """
        )

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
        """フォーム入力から仮設定を構築する。

        M8 修正: 以前は ``int(...)`` / ``float(...)`` が無防備で、入力が
        "abc" 等のときに ``ValueError`` で落ちていた。``_parse_int`` /
        ``_parse_float`` ヘルパでフォールバックする。
        """
        def _parse_int(text: str, default: int) -> int:
            s = text.strip()
            if not s:
                return default
            try:
                return int(s)
            except ValueError:
                return default

        def _parse_float(text: str, default: float) -> float:
            s = text.strip()
            if not s:
                return default
            try:
                return float(s)
            except ValueError:
                return default

        return AppSettings(
            library_root=self.library_root["line_edit"].text().strip(),
            thumbnail_root=self.thumbnail_root["line_edit"].text().strip(),
            source_wildcard_root=self.source_wildcard_root["line_edit"].text().strip(),
            source_thumbnail_root=self.source_thumbnail_root["line_edit"].text().strip(),
            lora_root=self.lora_root["line_edit"].text().strip(),
            api_base_url=self.api_base_url.text().strip(),
            api_timeout_sec=_parse_int(self.api_timeout_sec.text(), 120),
            generation_prompt_prefix=self.generation_prompt_prefix.toPlainText().strip(),
            generation_negative_prompt=self.generation_negative_prompt.toPlainText(),
            generation_width=_parse_int(self.generation_width.text(), 512),
            generation_height=_parse_int(self.generation_height.text(), 768),
            generation_steps=_parse_int(self.generation_steps.text(), 20),
            generation_cfg_scale=_parse_float(self.generation_cfg_scale.text(), 7.0),
            generation_sampler_name=self.generation_sampler_name.text().strip(),
            generation_adetailer_enabled=False,
            generation_extra_payload_json="{}",
            include_lora_on_copy=True,
            thumbnail_size=_parse_int(self.thumbnail_size.text(), 260),
            random_output_root=self.random_output_root["line_edit"].text().strip(),
            random_concept_root=self.random_concept_root["line_edit"].text().strip(),
            random_concept_include_subfolders=bool(self.random_concept_scope.currentData()),
            random_character_root=self.random_character_root["line_edit"].text().strip(),
            random_character_include_subfolders=bool(self.random_character_scope.currentData()),
            random_prompt=self.random_prompt.toPlainText().strip(),
            random_negative_prompt=self.random_negative_prompt.toPlainText(),
            random_width=_parse_int(self.random_width.text(), 512),
            random_height=_parse_int(self.random_height.text(), 768),
            random_steps=_parse_int(self.random_steps.text(), 20),
            random_batch_count=_parse_int(self.random_batch_count.text(), 1),
            random_batch_size=_parse_int(self.random_batch_size.text(), 1),
            random_cfg_scale=_parse_float(self.random_cfg_scale.text(), 7.0),
            random_sampler_name=self.random_sampler_name.text().strip(),
            random_seed_mode=str(self.random_seed_mode.currentData() or "random"),
            random_seed=_parse_int(self.random_seed.text(), -1),
            random_checkpoint_name=self.random_checkpoint_name.currentText().strip(),
            random_adetailer_enabled=self.random_adetailer_enabled.isChecked(),
        )

    def _refresh_random_checkpoint_list(self) -> None:
        current = self.random_checkpoint_name.currentText().strip()
        try:
            checkpoints = self.api.list_checkpoints(self._candidate_settings())
        except Exception as exc:
            self.random_checkpoint_status.setStyleSheet("color: #ef8d8d;")
            self.random_checkpoint_status.setText(f"候補一覧を取得できません: {exc}")
            return

        self.random_checkpoint_name.blockSignals(True)
        try:
            self.random_checkpoint_name.clear()
            self.random_checkpoint_name.addItem("")
            for checkpoint in checkpoints:
                self.random_checkpoint_name.addItem(checkpoint)
            if current and current not in checkpoints:
                self.random_checkpoint_name.addItem(current)
            self.random_checkpoint_name.setCurrentText(current)
            if checkpoints:
                self.random_checkpoint_status.setStyleSheet("color: #7fd48e;")
                self.random_checkpoint_status.setText(f"{len(checkpoints)} 件の checkpoint 候補を取得しました")
            else:
                self.random_checkpoint_status.setStyleSheet("color: #e7b65c;")
                self.random_checkpoint_status.setText("checkpoint 候補が見つかりませんでした")
        finally:
            self.random_checkpoint_name.blockSignals(False)

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
        """確定設定を構築する。M8 修正で int/float 入力を安全にパースする。"""
        def _pi(text: str, default: int) -> int:
            s = text.strip()
            if not s:
                return default
            try:
                return int(s)
            except ValueError:
                return default

        def _pf(text: str, default: float) -> float:
            s = text.strip()
            if not s:
                return default
            try:
                return float(s)
            except ValueError:
                return default

        return AppSettings(
            library_root=self.library_root["line_edit"].text().strip(),
            thumbnail_root=self.thumbnail_root["line_edit"].text().strip(),
            source_wildcard_root=self.source_wildcard_root["line_edit"].text().strip(),
            source_thumbnail_root=self.source_thumbnail_root["line_edit"].text().strip(),
            lora_root=self.lora_root["line_edit"].text().strip(),
            api_base_url=self.api_base_url.text().strip(),
            api_timeout_sec=_pi(self.api_timeout_sec.text(), base_settings.api_timeout_sec),
            generation_prompt_prefix=self.generation_prompt_prefix.toPlainText().strip(),
            generation_negative_prompt=self.generation_negative_prompt.toPlainText(),
            generation_width=_pi(self.generation_width.text(), base_settings.generation_width),
            generation_height=_pi(self.generation_height.text(), base_settings.generation_height),
            generation_steps=_pi(self.generation_steps.text(), base_settings.generation_steps),
            generation_cfg_scale=_pf(self.generation_cfg_scale.text(), base_settings.generation_cfg_scale),
            generation_sampler_name=self.generation_sampler_name.text().strip(),
            generation_adetailer_enabled=self.generation_adetailer_enabled.isChecked(),
            generation_extra_payload_json=base_settings.generation_extra_payload_json,
            thumbnail_random_wildcard=base_settings.thumbnail_random_wildcard,
            thumbnail_random_wildcard_root=self.thumbnail_random_wc_root["line_edit"].text().strip() or base_settings.thumbnail_random_wildcard_root,
            include_lora_on_copy=base_settings.include_lora_on_copy,
            thumbnail_size=_pi(self.thumbnail_size.text(), base_settings.thumbnail_size),
            random_output_root=self.random_output_root["line_edit"].text().strip() or base_settings.random_output_root,
            random_save_mode=base_settings.random_save_mode,
            random_concept_root=self.random_concept_root["line_edit"].text().strip() or base_settings.random_concept_root,
            random_concept_include_subfolders=bool(self.random_concept_scope.currentData()),
            random_wildcard_per_generation=base_settings.random_wildcard_per_generation,
            random_character_root=self.random_character_root["line_edit"].text().strip() or base_settings.random_character_root,
            random_character_include_subfolders=bool(self.random_character_scope.currentData()),
            random_prompt=self.random_prompt.toPlainText().strip(),
            random_negative_prompt=self.random_negative_prompt.toPlainText(),
            random_width=_pi(self.random_width.text(), base_settings.random_width),
            random_height=_pi(self.random_height.text(), base_settings.random_height),
            random_steps=_pi(self.random_steps.text(), base_settings.random_steps),
            random_batch_count=_pi(self.random_batch_count.text(), base_settings.random_batch_count),
            random_batch_size=_pi(self.random_batch_size.text(), base_settings.random_batch_size),
            random_cfg_scale=_pf(self.random_cfg_scale.text(), base_settings.random_cfg_scale),
            random_sampler_name=self.random_sampler_name.text().strip() or base_settings.random_sampler_name,
            random_seed_mode=str(self.random_seed_mode.currentData() or base_settings.random_seed_mode),
            random_seed=_pi(self.random_seed.text(), base_settings.random_seed),
            random_checkpoint_name=self.random_checkpoint_name.currentText().strip(),
            random_adetailer_enabled=self.random_adetailer_enabled.isChecked(),
            window_width=base_settings.window_width,
            window_height=base_settings.window_height,
            window_maximized=base_settings.window_maximized,
            splitter_sizes=base_settings.splitter_sizes,
            detail_splitter_sizes=base_settings.detail_splitter_sizes,
            last_folder=base_settings.last_folder,
            sort_mode=base_settings.sort_mode,
            sort_key=base_settings.sort_key,
            sort_order=base_settings.sort_order,
            scroll_step=base_settings.scroll_step,
            new_wildcard_custom_tabs=base_settings.new_wildcard_custom_tabs,
        )

    def accept(self) -> None:
        try:
            inspection = self.repo.inspect_source_thumbnail_roots(self._candidate_settings())
        except Exception as exc:
            self._show_error("設定エラー", str(exc))
            return

        # L14 修正: 以前は configured_exists が False でも existing_roots が
        # 1つでもあれば警告なしで進んでいた。設定パス不一致をユーザーに通知する。
        if not inspection["configured_exists"] and not inspection["existing_roots"]:
            result = self._ask_ok_cancel(
                "サムネイル設定の確認",
                "サムネイルの設定先が見つかりません。\n"
                "このまま続けると、サムネイル生成は 0 件になります。\n\n"
                f"設定先:\n{inspection['configured_root']}",
            )
            if result != QMessageBox.Ok:
                return
        elif not inspection["configured_exists"] and inspection["existing_roots"]:
            result = self._ask_ok_cancel(
                "サムネイル設定の確認",
                "設定されたサムネイル取込元パスが見つかりません。\n"
                "代わりの候補が存在しますが、設定パスを先に修正することを推奨します。\n\n"
                f"設定先:\n{inspection['configured_root']}\n\n"
                f"候補:\n{inspection['existing_roots'][0]}",
            )
            if result != QMessageBox.Ok:
                return
        super().accept()


@dataclass(slots=True)
class RandomGenerationRequest:
    prompt: str
    negative_prompt: str
    output_root: str
    save_mode: str
    wildcard_root: str
    wildcard_include_subfolders: bool
    wildcard_per_generation: bool
    character_text: str
    width: int
    height: int
    steps: int
    batch_count: int
    batch_size: int
    cfg_scale: float
    sampler_name: str
    seed_mode: str
    seed: int
    checkpoint_name: str
    adetailer_enabled: bool


class RandomGenerationWorker(QObject):
    finished = Signal(list, str)
    failed = Signal(str)

    def __init__(self, settings: AppSettings, api: ThumbnailApiClient, repo: WildcardRepository, request: RandomGenerationRequest):
        super().__init__()
        self.settings = settings
        self.api = api
        self.repo = repo
        self.request = request

    def _pick_random_prompt(self, root_text: str, include_subfolders: bool) -> tuple[str, str]:
        root = Path(root_text)
        candidates = collect_txt_files(root, include_subfolders)
        if not candidates:
            raise FileNotFoundError(f"ワイルドカード候補が見つかりません: {root}")
        path = random.choice(candidates)
        content = self.repo.load_entry_content(path)
        prompt = extract_prompt_line(content)
        if not prompt:
            raise ValueError(f"プロンプトとして使える行がありません: {path}")
        return prompt, str(path)

    def _build_prompt(self, wildcard_text: str) -> str:
        prompt_parts = [self.request.prompt.strip(), self.request.character_text.strip(), wildcard_text]
        prompt_parts = [part for part in prompt_parts if part]
        prompt = ThumbnailApiClient._compose_prompt("", ", ".join(prompt_parts))
        if not prompt:
            raise ValueError("生成用プロンプトが空です。")
        return prompt

    def _build_payload(self, prompt: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "prompt": prompt,
            "negative_prompt": self.request.negative_prompt.strip(),
            "steps": self.request.steps,
            "cfg_scale": self.request.cfg_scale,
            "width": self.request.width,
            "height": self.request.height,
            "sampler_name": self.request.sampler_name,
            "batch_size": self.request.batch_size,
            "n_iter": self.request.batch_count,
            "seed": self.request.seed if self.request.seed_mode == "fixed" else -1,
        }

        if self.request.checkpoint_name.strip():
            payload["override_settings"] = {
                "sd_model_checkpoint": self.request.checkpoint_name.strip(),
            }

        if self.request.adetailer_enabled:
            payload["alwayson_scripts"] = {
                "ADetailer": {
                    "args": [
                        {
                            "ad_model": "face_yolov8n.pt",
                        }
                    ]
                }
            }

        return payload

    @Slot()
    def run(self) -> None:
        try:
            output_root = Path(self.request.output_root or self.settings.library_root).resolve()
            if self.request.save_mode == "date_folder":
                output_root = output_root / datetime.now().strftime("%Y-%m-%d")
            output_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            saved_paths: list[str] = []
            if self.request.wildcard_per_generation:
                total = max(1, self.request.batch_count * self.request.batch_size)
                for generation_index in range(total):
                    wildcard_text, wildcard_source = self._pick_random_prompt(
                        self.request.wildcard_root,
                        self.request.wildcard_include_subfolders,
                    )
                    prompt = self._build_prompt(wildcard_text)
                    payload = self._build_payload(prompt)
                    payload["batch_size"] = 1
                    payload["n_iter"] = 1
                    data = self.api.request_txt2img(self.settings, payload)
                    images = data.get("images") or []
                    if not images:
                        raise ValueError("API から画像が返りませんでした。")
                    parameters_text, extra_metadata = self._build_png_metadata(
                        prompt=prompt,
                        negative_prompt=self.request.negative_prompt.strip(),
                        wildcard_text=wildcard_text,
                        wildcard_source=wildcard_source,
                        payload=payload,
                        api_data=data,
                    )
                    for image_index, image_data in enumerate(images, start=1):
                        image_bytes = base64.b64decode(str(image_data).split(",", 1)[-1])
                        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                        filename = f"random_{stamp}_{uuid.uuid4().hex[:8]}_{generation_index + 1:03d}_{image_index:02d}.png"
                        destination = output_root / filename
                        pnginfo = PngInfo()
                        pnginfo.add_text("parameters", parameters_text)
                        for key, value in extra_metadata.items():
                            pnginfo.add_text(key, value)
                        image.save(destination, "PNG", pnginfo=pnginfo)
                        saved_paths.append(str(destination))
            else:
                wildcard_text, wildcard_source = self._pick_random_prompt(
                    self.request.wildcard_root,
                    self.request.wildcard_include_subfolders,
                )
                prompt = self._build_prompt(wildcard_text)
                payload = self._build_payload(prompt)
                data = self.api.request_txt2img(self.settings, payload)
                images = data.get("images") or []
                if not images:
                    raise ValueError("API から画像が返りませんでした。")
                parameters_text, extra_metadata = self._build_png_metadata(
                    prompt=prompt,
                    negative_prompt=self.request.negative_prompt.strip(),
                    wildcard_text=wildcard_text,
                    wildcard_source=wildcard_source,
                    payload=payload,
                    api_data=data,
                )
                for index, image_data in enumerate(images, start=1):
                    image_bytes = base64.b64decode(str(image_data).split(",", 1)[-1])
                    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                    filename = f"random_{stamp}_{uuid.uuid4().hex[:8]}_{index:02d}.png"
                    destination = output_root / filename
                    pnginfo = PngInfo()
                    pnginfo.add_text("parameters", parameters_text)
                    for key, value in extra_metadata.items():
                        pnginfo.add_text(key, value)
                    image.save(destination, "PNG", pnginfo=pnginfo)
                    saved_paths.append(str(destination))

            self.finished.emit(saved_paths, prompt)
        except Exception as exc:
            self.failed.emit(str(exc))

    @staticmethod
    def _build_png_metadata(
        *,
        prompt: str,
        negative_prompt: str,
        wildcard_text: str,
        wildcard_source: str,
        payload: dict[str, object],
        api_data: dict[str, object],
    ) -> tuple[str, dict[str, str]]:
        if isinstance(api_data.get("parameters"), str) and api_data["parameters"].strip():
            parameters_text = str(api_data["parameters"]).strip()
        else:
            parameters_text = "\n".join(
                [
                    prompt,
                    f"Negative prompt: {negative_prompt}",
                    (
                        "Steps: {steps}, Sampler: {sampler}, CFG scale: {cfg}, Seed: {seed}, "
                        "Size: {width}x{height}, Batch size: {batch_size}, Batch count: {batch_count}"
                    ).format(
                        steps=payload.get("steps", ""),
                        sampler=payload.get("sampler_name", ""),
                        cfg=payload.get("cfg_scale", ""),
                        seed=payload.get("seed", ""),
                        width=payload.get("width", ""),
                        height=payload.get("height", ""),
                        batch_size=payload.get("batch_size", ""),
                        batch_count=payload.get("n_iter", ""),
                    ),
                ]
            )

        extra_metadata = {
            "random_wildcard": wildcard_text,
            "random_wildcard_source": wildcard_source,
        }
        if isinstance(api_data.get("info"), str) and api_data["info"].strip():
            extra_metadata["random_generation_info"] = str(api_data["info"]).strip()
        return parameters_text, extra_metadata


class RandomGenerationDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        repo: WildcardRepository,
        api: ThumbnailApiClient,
        parent=None,
        *,
        preset_prompt_text: str = "",
    ):
        super().__init__(parent)
        self.settings = settings
        self.repo = repo
        self.api = api
        self.preset_prompt_text = preset_prompt_text.strip()
        self.setWindowTitle("ランダム生成")
        self.setStyleSheet(
            f"""
            QDialog {{
                background: #0f1216;
                color: #edf2f7;
            }}
            QLabel {{
                color: #edf2f7;
            }}
            QGroupBox {{
                border: 1px solid #2a3440;
                border-radius: {UI_RADIUS}px;
                margin-top: {UI_CARD_MARGIN_TOP}px;
                padding: 6px 0px 10px 0px;
                color: #edf2f7;
                font-weight: 600;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: {UI_CARD_MARGIN_X}px;
                padding: 0 4px;
            }}
            QLineEdit, QTextEdit, QPlainTextEdit, QListWidget, QTreeWidget, QListView, QComboBox, QSpinBox {{
                background: #161b22;
                border: 1px solid #2a3440;
                border-radius: {UI_RADIUS}px;
                color: #edf2f7;
                padding: 6px 10px;
                min-height: {UI_CONTROL_HEIGHT}px;
            }}
            QPushButton {{
                background: #2e5fb8;
                border: 1px solid #2e5fb8;
                border-radius: {UI_RADIUS}px;
                color: white;
                padding: 0px {UI_BUTTON_PADDING_X}px;
                min-height: {UI_BUTTON_HEIGHT}px;
                max-height: {UI_BUTTON_HEIGHT}px;
                height: {UI_BUTTON_HEIGHT}px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: #3a71c6;
                border-color: #3a71c6;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(UI_OUTER_MARGIN, UI_OUTER_MARGIN, UI_OUTER_MARGIN, UI_OUTER_MARGIN)
        layout.setSpacing(UI_SECTION_SPACING)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        layout.addWidget(scroll, 3)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 12, 0)
        content_layout.setSpacing(10)
        scroll.setWidget(content)

        overview = QLabel(
            "毎回 1 件の wildcard を抽選して、固定プロンプトと固定キャラクターに合成して生成します。"
        )
        overview.setWordWrap(True)
        overview.setStyleSheet("color: #c8d2dd; padding: 2px 4px;")
        content_layout.addWidget(overview)

        source_box = QGroupBox("抽選元")
        source_layout = QGridLayout(source_box)
        source_layout.setContentsMargins(UI_CARD_PADDING_X, UI_CARD_PADDING_Y, UI_CARD_PADDING_X, UI_CARD_PADDING_Y + 8)
        source_layout.setHorizontalSpacing(UI_SECTION_SPACING)
        source_layout.setVerticalSpacing(8)
        source_layout.setColumnStretch(1, 1)

        fixed_box = QGroupBox("固定入力")
        fixed_layout = QGridLayout(fixed_box)
        fixed_layout.setContentsMargins(UI_CARD_PADDING_X, UI_CARD_PADDING_Y, UI_CARD_PADDING_X, UI_CARD_PADDING_Y + 8)
        fixed_layout.setHorizontalSpacing(UI_SECTION_SPACING)
        fixed_layout.setVerticalSpacing(8)
        fixed_layout.setColumnStretch(1, 1)

        param_box = QGroupBox("生成パラメータ")
        param_layout = QGridLayout(param_box)
        param_layout.setContentsMargins(UI_CARD_PADDING_X, UI_CARD_PADDING_Y, UI_CARD_PADDING_X, UI_CARD_PADDING_Y + 8)
        param_layout.setHorizontalSpacing(16)
        param_layout.setVerticalSpacing(8)
        param_layout.setColumnStretch(1, 1)
        param_layout.setColumnStretch(3, 1)

        advanced_box = QGroupBox("高度な設定")
        advanced_layout = QGridLayout(advanced_box)
        advanced_layout.setContentsMargins(UI_CARD_PADDING_X, UI_CARD_PADDING_Y, UI_CARD_PADDING_X, UI_CARD_PADDING_Y + 8)
        advanced_layout.setHorizontalSpacing(16)
        advanced_layout.setVerticalSpacing(8)
        advanced_layout.setColumnStretch(1, 1)

        self.output_root = self._path_row(settings.random_output_root)
        self.save_mode = NoWheelComboBox()
        self.save_mode.addItem("直接保存", "direct")
        self.save_mode.addItem("日付フォルダを作成", "date_folder")
        # M16 修正: 以前は settings.random_save_mode の値に関わらず常に
        # "direct" (index 0) がデフォルトになり、初回起動時に
        # models.py のデフォルト "date_folder" が "direct" で上書きされていた。
        # 設定値を尊重する。
        self.save_mode.setCurrentIndex(1 if settings.random_save_mode == "date_folder" else 0)
        self.wildcard_root = self._path_row(settings.random_concept_root)
        self.wildcard_include_subfolders = NoWheelComboBox()
        self.wildcard_include_subfolders.addItem("直下のみ", False)
        self.wildcard_include_subfolders.addItem("サブフォルダ含む", True)
        self.wildcard_include_subfolders.setCurrentIndex(1 if settings.random_concept_include_subfolders else 0)
        self.wildcard_per_generation = QCheckBox("生成ごとに wildcard を変える")
        self.wildcard_per_generation.setChecked(settings.random_wildcard_per_generation)

        self.prompt_edit = QPlainTextEdit(self.preset_prompt_text or settings.random_prompt)
        self.prompt_edit.setPlaceholderText("固定プロンプト")
        self.prompt_edit.setMinimumHeight(92)
        self.prompt_edit.setMaximumHeight(120)
        self.character_text = QPlainTextEdit("")
        self.character_text.setPlaceholderText("固定キャラクター")
        self.character_text.setMinimumHeight(92)
        self.character_text.setMaximumHeight(120)
        self.negative_prompt_edit = QPlainTextEdit(settings.random_negative_prompt)
        self.negative_prompt_edit.setPlaceholderText("ネガティブプロンプト")
        self.negative_prompt_edit.setMinimumHeight(76)
        self.negative_prompt_edit.setMaximumHeight(100)

        self.random_steps = QLineEdit(str(settings.random_steps))
        self.random_width = QLineEdit(str(settings.random_width))
        self.random_height = QLineEdit(str(settings.random_height))
        self.random_batch_count = QLineEdit(str(settings.random_batch_count))
        self.random_batch_size = QLineEdit(str(settings.random_batch_size))
        self.random_cfg_scale = QLineEdit(str(settings.random_cfg_scale))
        self.random_sampler_name = QLineEdit(settings.random_sampler_name)
        self.random_seed_mode = NoWheelComboBox()
        self.random_seed_mode.addItem("ランダム", "random")
        self.random_seed_mode.addItem("固定", "fixed")
        self.random_seed_mode.setCurrentIndex(0 if settings.random_seed_mode != "fixed" else 1)
        self.random_seed = QLineEdit(str(settings.random_seed))
        self.random_checkpoint_name = NoWheelComboBox()
        self.random_checkpoint_name.setEditable(True)
        self.random_checkpoint_name.setInsertPolicy(QComboBox.NoInsert)
        self.random_checkpoint_name.addItem(settings.random_checkpoint_name or "")
        self.random_checkpoint_name.setCurrentText(settings.random_checkpoint_name)
        self.random_refresh_checkpoints_button = QPushButton("候補更新")
        self.random_refresh_checkpoints_button.clicked.connect(self._refresh_random_checkpoint_list)
        self.random_refresh_checkpoints_button.setFixedHeight(UI_BUTTON_HEIGHT)
        self.random_checkpoint_status = QLabel("checkpoint 候補を取得できます")
        self.random_checkpoint_status.setWordWrap(True)
        self.random_checkpoint_status.setMinimumHeight(20)
        self.random_checkpoint_status.setStyleSheet("color: #e7b65c;")
        self.random_adetailer_enabled = QCheckBox("ADetailer を使う")
        self.random_adetailer_enabled.setChecked(settings.random_adetailer_enabled)

        for widget in (
            self.prompt_edit,
            self.character_text,
            self.negative_prompt_edit,
        ):
            self._style_input(widget)
        for widget in (
            self.random_steps,
            self.random_width,
            self.random_height,
            self.random_batch_count,
            self.random_batch_size,
            self.random_cfg_scale,
            self.random_sampler_name,
            self.random_seed,
        ):
            self._style_compact_line_field(widget)
        for widget in (
            self.wildcard_include_subfolders,
            self.random_seed_mode,
            self.random_checkpoint_name,
            self.save_mode,
        ):
            self._style_compact_combo_field(widget)
        self._style_compact_line_field(self.output_root["line_edit"])
        self._style_compact_line_field(self.wildcard_root["line_edit"])
        self.random_refresh_checkpoints_button.setFixedHeight(30)

        source_layout.addWidget(self._field_label("保存先"), 0, 0)
        source_layout.addWidget(self.output_root["widget"], 0, 1, 1, 3)
        source_layout.addWidget(self._field_label("保存方法"), 1, 0)
        source_layout.addWidget(self.save_mode, 1, 1, 1, 3)
        source_layout.addWidget(self._field_label("ワイルドカードフォルダ"), 2, 0)
        source_layout.addWidget(self.wildcard_root["widget"], 2, 1, 1, 3)
        source_layout.addWidget(self._field_label("範囲"), 3, 0)
        source_layout.addWidget(self.wildcard_include_subfolders, 3, 1)
        source_layout.addWidget(self.wildcard_per_generation, 4, 0, 1, 4)

        fixed_layout.addWidget(self._field_label("固定プロンプト"), 0, 0)
        fixed_layout.addWidget(self.prompt_edit, 0, 1, 1, 3)
        fixed_layout.addWidget(self._field_label("固定キャラクター"), 1, 0)
        fixed_layout.addWidget(self.character_text, 1, 1, 1, 3)
        fixed_layout.addWidget(self._field_label("ネガティブ"), 2, 0)
        fixed_layout.addWidget(self.negative_prompt_edit, 2, 1, 1, 3)

        param_layout.addWidget(self._field_label("Steps"), 0, 0)
        param_layout.addWidget(self.random_steps, 0, 1)
        param_layout.addWidget(self._field_label("CFG"), 0, 2)
        param_layout.addWidget(self.random_cfg_scale, 0, 3)
        param_layout.addWidget(self._field_label("Width"), 1, 0)
        param_layout.addWidget(self.random_width, 1, 1)
        param_layout.addWidget(self._field_label("Height"), 1, 2)
        param_layout.addWidget(self.random_height, 1, 3)
        param_layout.addWidget(self._field_label("Batch Size"), 2, 0)
        param_layout.addWidget(self.random_batch_size, 2, 1)
        param_layout.addWidget(self._field_label("Batch Count"), 2, 2)
        param_layout.addWidget(self.random_batch_count, 2, 3)
        param_layout.addWidget(self._field_label("サンプラー"), 3, 0)
        param_layout.addWidget(self.random_sampler_name, 3, 1, 1, 3)

        seed_row = QWidget()
        seed_layout = QHBoxLayout(seed_row)
        seed_layout.setContentsMargins(0, 0, 0, 0)
        seed_layout.setSpacing(UI_SECTION_SPACING)
        seed_layout.addWidget(self.random_seed_mode, 0)
        seed_layout.addWidget(self.random_seed, 1)
        param_layout.addWidget(self._field_label("Seed"), 4, 0)
        param_layout.addWidget(seed_row, 4, 1, 1, 3)

        checkpoint_row = QWidget()
        checkpoint_layout = QHBoxLayout(checkpoint_row)
        checkpoint_layout.setContentsMargins(0, 0, 0, 0)
        checkpoint_layout.setSpacing(UI_SECTION_SPACING)
        checkpoint_layout.addWidget(self.random_checkpoint_name, 1)
        checkpoint_layout.addWidget(self.random_refresh_checkpoints_button, 0)
        advanced_layout.addWidget(self._field_label("Checkpoint"), 0, 0)
        advanced_layout.addWidget(checkpoint_row, 0, 1, 1, 3)
        advanced_layout.addWidget(self.random_checkpoint_status, 1, 1, 1, 3)
        advanced_layout.addWidget(self.random_adetailer_enabled, 2, 0, 1, 4)
        advanced_layout.setRowMinimumHeight(1, 24)

        content_layout.addWidget(source_box)
        content_layout.addWidget(fixed_box)
        content_layout.addWidget(param_box)
        content_layout.addWidget(advanced_box)
        self._stabilize_random_dialog_layout(content, scroll, [source_box, fixed_box, param_box, advanced_box])

        self.result_box = QPlainTextEdit()
        self.result_box.setReadOnly(True)
        self.result_box.setPlaceholderText("生成結果がここに表示されます")
        self.result_box.setMinimumHeight(80)
        self.result_box.setMaximumHeight(96)
        layout.addWidget(self.result_box, 1)

        self.status_label = QLabel("待機中")
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        self.generate_button = QPushButton("生成開始")
        self.generate_button.clicked.connect(self.start_generation)
        self.generate_button.setFixedHeight(UI_BUTTON_HEIGHT)
        self.close_button = QPushButton("閉じる")
        self.close_button.clicked.connect(self.reject)
        self.close_button.setFixedHeight(UI_BUTTON_HEIGHT)
        button_row.addWidget(self.generate_button)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

        self._apply_random_dialog_geometry(scroll, content, [source_box, fixed_box, param_box, advanced_box])


        self._thread: QThread | None = None
        self._worker: RandomGenerationWorker | None = None

        # コンテキストメニューを日本語化
        _setup_japanese_context_menu(self.prompt_edit)
        _setup_japanese_context_menu(self.character_text)
        _setup_japanese_context_menu(self.negative_prompt_edit)
        _setup_japanese_context_menu(self.result_box)

    def start_generation(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return

        def _pi(text: str, default: int) -> int:
            s = text.strip()
            if not s:
                return default
            try:
                return int(s)
            except ValueError:
                return default

        def _pf(text: str, default: float) -> float:
            s = text.strip()
            if not s:
                return default
            try:
                return float(s)
            except ValueError:
                return default

        request = RandomGenerationRequest(
            prompt=self.prompt_edit.toPlainText().strip(),
            negative_prompt=self.negative_prompt_edit.toPlainText(),
            output_root=self.output_root["line_edit"].text().strip(),
            save_mode=str(self.save_mode.currentData() or "direct"),
            wildcard_root=self.wildcard_root["line_edit"].text().strip(),
            wildcard_include_subfolders=bool(self.wildcard_include_subfolders.currentData()),
            wildcard_per_generation=self.wildcard_per_generation.isChecked(),
            character_text=self.character_text.toPlainText(),
            width=_pi(self.random_width.text(), self.settings.random_width),
            height=_pi(self.random_height.text(), self.settings.random_height),
            steps=_pi(self.random_steps.text(), self.settings.random_steps),
            batch_count=_pi(self.random_batch_count.text(), self.settings.random_batch_count),
            batch_size=_pi(self.random_batch_size.text(), self.settings.random_batch_size),
            cfg_scale=_pf(self.random_cfg_scale.text(), self.settings.random_cfg_scale),
            sampler_name=self.random_sampler_name.text().strip() or self.settings.random_sampler_name,
            seed_mode=str(self.random_seed_mode.currentData() or self.settings.random_seed_mode),
            seed=_pi(self.random_seed.text(), self.settings.random_seed),
            checkpoint_name=self.random_checkpoint_name.currentText().strip(),
            adetailer_enabled=self.random_adetailer_enabled.isChecked(),
        )
        self.generate_button.setEnabled(False)
        self.status_label.setText("生成中...")
        self.result_box.clear()

        self._thread = QThread(self)
        self._worker = RandomGenerationWorker(self.settings, self.api, self.repo, request)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_generation_finished)
        self._worker.failed.connect(self._on_generation_failed)
        self._worker.finished.connect(self._cleanup_worker)
        self._worker.failed.connect(self._cleanup_worker)
        self._thread.start()

    def build_settings(self, base_settings: AppSettings) -> AppSettings:
        """RandomGenerationDialog の確定設定を構築する。M8 修正で安全パース。"""
        def _pi(text: str, default: int) -> int:
            s = text.strip()
            if not s:
                return default
            try:
                return int(s)
            except ValueError:
                return default

        def _pf(text: str, default: float) -> float:
            s = text.strip()
            if not s:
                return default
            try:
                return float(s)
            except ValueError:
                return default

        return AppSettings(
            library_root=base_settings.library_root,
            thumbnail_root=base_settings.thumbnail_root,
            source_wildcard_root=base_settings.source_wildcard_root,
            source_thumbnail_root=base_settings.source_thumbnail_root,
            lora_root=base_settings.lora_root,
            api_base_url=base_settings.api_base_url,
            api_timeout_sec=base_settings.api_timeout_sec,
            generation_prompt_prefix=base_settings.generation_prompt_prefix,
            generation_negative_prompt=base_settings.generation_negative_prompt,
            generation_width=base_settings.generation_width,
            generation_height=base_settings.generation_height,
            generation_steps=base_settings.generation_steps,
            generation_cfg_scale=base_settings.generation_cfg_scale,
            generation_sampler_name=base_settings.generation_sampler_name,
            generation_extra_payload_json=base_settings.generation_extra_payload_json,
            include_lora_on_copy=base_settings.include_lora_on_copy,
            thumbnail_size=base_settings.thumbnail_size,
            random_output_root=self.output_root["line_edit"].text().strip() or base_settings.random_output_root,
            random_save_mode=str(self.save_mode.currentData() or "direct"),
            random_concept_root=self.wildcard_root["line_edit"].text().strip() or base_settings.random_concept_root,
            random_concept_include_subfolders=bool(self.wildcard_include_subfolders.currentData()),
            random_wildcard_per_generation=self.wildcard_per_generation.isChecked(),
            random_character_root=base_settings.random_character_root,
            random_character_include_subfolders=base_settings.random_character_include_subfolders,
            random_prompt=self.prompt_edit.toPlainText().strip(),
            random_negative_prompt=self.negative_prompt_edit.toPlainText(),
            random_width=_pi(self.random_width.text(), base_settings.random_width),
            random_height=_pi(self.random_height.text(), base_settings.random_height),
            random_steps=_pi(self.random_steps.text(), base_settings.random_steps),
            random_batch_count=_pi(self.random_batch_count.text(), base_settings.random_batch_count),
            random_batch_size=_pi(self.random_batch_size.text(), base_settings.random_batch_size),
            random_cfg_scale=_pf(self.random_cfg_scale.text(), base_settings.random_cfg_scale),
            random_sampler_name=self.random_sampler_name.text().strip() or base_settings.random_sampler_name,
            random_seed_mode=str(self.random_seed_mode.currentData() or base_settings.random_seed_mode),
            random_seed=_pi(self.random_seed.text(), base_settings.random_seed),
            random_checkpoint_name=self.random_checkpoint_name.currentText().strip(),
            random_adetailer_enabled=self.random_adetailer_enabled.isChecked(),
            window_width=base_settings.window_width,
            window_height=base_settings.window_height,
            window_maximized=base_settings.window_maximized,
            splitter_sizes=base_settings.splitter_sizes,
            detail_splitter_sizes=base_settings.detail_splitter_sizes,
            last_folder=base_settings.last_folder,
            sort_mode=base_settings.sort_mode,
            sort_key=base_settings.sort_key,
            sort_order=base_settings.sort_order,
            scroll_step=base_settings.scroll_step,
            new_wildcard_custom_tabs=base_settings.new_wildcard_custom_tabs,
        )

    def _on_generation_finished(self, saved_paths: list[str], prompt: str) -> None:
        self.status_label.setText(f"{len(saved_paths)} 件を保存しました")
        self.result_box.setPlainText("\n".join(saved_paths) + (f"\n\nPrompt:\n{prompt}" if prompt else ""))

    def _on_generation_failed(self, message: str) -> None:
        self.status_label.setText("失敗")
        self.result_box.setPlainText(message)

    def _cleanup_worker(self, *args) -> None:
        self.generate_button.setEnabled(True)
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
            self._thread.deleteLater()
        if self._worker is not None:
            self._worker.deleteLater()
        self._thread = None
        self._worker = None

    def _refresh_random_checkpoint_list(self) -> None:
        current = self.random_checkpoint_name.currentText().strip()
        try:
            checkpoints = self.api.list_checkpoints(self.settings)
        except Exception as exc:
            self.random_checkpoint_status.setStyleSheet("color: #ef8d8d;")
            self.random_checkpoint_status.setText(f"候補一覧を取得できません: {exc}")
            return

        self.random_checkpoint_name.blockSignals(True)
        try:
            self.random_checkpoint_name.clear()
            self.random_checkpoint_name.addItem("")
            for checkpoint in checkpoints:
                self.random_checkpoint_name.addItem(checkpoint)
            if current and current not in checkpoints:
                self.random_checkpoint_name.addItem(current)
            self.random_checkpoint_name.setCurrentText(current)
            if checkpoints:
                self.random_checkpoint_status.setStyleSheet("color: #7fd48e;")
                self.random_checkpoint_status.setText(f"{len(checkpoints)} 件の checkpoint 候補を取得しました")
            else:
                self.random_checkpoint_status.setStyleSheet("color: #e7b65c;")
                self.random_checkpoint_status.setText("checkpoint 候補が見つかりませんでした")
        finally:
            self.random_checkpoint_name.blockSignals(False)

    def _path_row(self, value: str) -> dict[str, QWidget]:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        line_edit = QLineEdit(value)
        line_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        _setup_japanese_context_menu(line_edit)
        button = QPushButton("参照")
        button.setFixedWidth(72)
        button.setFixedHeight(UI_BUTTON_HEIGHT)
        button.clicked.connect(lambda: self._pick_directory(line_edit))
        layout.addWidget(line_edit)
        layout.addWidget(button)
        return {"widget": row, "line_edit": line_edit}

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        label.setMinimumWidth(92)
        label.setStyleSheet("color: #dce5ef; font-weight: 600;")
        return label

    def _stabilize_random_dialog_layout(self, content: QWidget, scroll: QScrollArea, boxes: list[QGroupBox]) -> None:
        content.adjustSize()
        content_width = max(box.sizeHint().width() for box in boxes)
        scrollbar_width = scroll.verticalScrollBar().sizeHint().width() + 8
        target_width = content_width + scrollbar_width + (UI_OUTER_MARGIN * 2)
        content.setMinimumWidth(content_width)
        scroll.setMinimumWidth(target_width)
        self.setMinimumWidth(max(self.minimumWidth(), target_width + (UI_OUTER_MARGIN * 2)))
        if self.width() < self.minimumWidth():
            self.resize(self.minimumWidth(), self.height())

    def _apply_random_dialog_geometry(self, scroll: QScrollArea, content: QWidget, boxes: list[QGroupBox]) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        screen_w = available.width() if available else 1280
        screen_h = available.height() if available else 900

        w = min(max(860, self.minimumWidth()), screen_w - 80)
        h = min(screen_h - 80, screen_h - 80)
        self.resize(w, h)

    def _pick_directory(self, line_edit: QLineEdit) -> None:
        selected = QFileDialog.getExistingDirectory(self, "フォルダ選択", line_edit.text())
        if selected:
            line_edit.setText(selected)

    def _style_line_field(self, widget: QLineEdit) -> None:
        widget.setMinimumHeight(UI_CONTROL_HEIGHT)
        widget.setStyleSheet(
            """
            QLineEdit {
                background: #1a1d22;
                border: 1px solid #2d3640;
                border-radius: 10px;
                color: #eef2f7;
                padding: 6px 10px;
                font-size: 13px;
            }
            """
        )

    def _style_compact_line_field(self, widget: QLineEdit) -> None:
        widget.setMinimumHeight(30)
        widget.setMaximumHeight(30)
        widget.setStyleSheet(
            """
            QLineEdit {
                background: #1a1d22;
                border: 1px solid #2d3640;
                border-radius: 8px;
                color: #eef2f7;
                padding: 3px 8px;
                font-size: 12px;
                min-height: 30px;
                max-height: 30px;
            }
            """
        )

    def _style_text_field(self, widget: QPlainTextEdit) -> None:
        widget.setMinimumHeight(84)
        widget.setStyleSheet(
            """
            QPlainTextEdit {
                background: #1a1d22;
                border: 1px solid #2d3640;
                border-radius: 10px;
                color: #eef2f7;
                padding: 8px 10px;
                font-size: 13px;
            }
            """
        )

    def _style_compact_combo_field(self, widget: QComboBox) -> None:
        widget.setMinimumHeight(30)
        widget.setMaximumHeight(30)
        widget.setStyleSheet(
            """
            QComboBox {
                background: #1a1d22;
                border: 1px solid #2d3640;
                border-radius: 8px;
                color: #eef2f7;
                padding: 2px 8px;
                font-size: 12px;
                min-height: 30px;
                max-height: 30px;
            }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox QAbstractItemView {
                background: #1a1d22;
                color: #eef2f7;
                selection-background-color: #2b6cb0;
            }
            """
        )

    def _style_combo_field(self, widget: QComboBox) -> None:
        widget.setMinimumHeight(UI_CONTROL_HEIGHT)
        widget.setStyleSheet(
            """
            QComboBox {
                background: #1a1d22;
                border: 1px solid #2d3640;
                border-radius: 10px;
                color: #eef2f7;
                padding: 4px 10px;
                font-size: 13px;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            QComboBox QAbstractItemView {
                background: #1a1d22;
                color: #eef2f7;
                selection-background-color: #2b6cb0;
            }
            """
        )

    @staticmethod
    def _style_input(widget: QWidget) -> None:
        widget.setStyleSheet(
            """
            QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {
                background: #1a1d22;
                border: 1px solid #2d3640;
                border-radius: 10px;
                color: #eef2f7;
                padding: 8px 10px;
                font-size: 13px;
            }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox QAbstractItemView {
                background: #1a1d22;
                color: #eef2f7;
                selection-background-color: #2b6cb0;
            }
            """
        )


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
        self.policy_combo = NoWheelComboBox()
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


class RescanWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)
    progress = Signal(int, int, str)  # current, total, name

    def __init__(self, db_path: Path, settings):
        super().__init__()
        self.db_path = db_path
        self.settings = settings
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        try:
            repo = WildcardRepository(self.db_path)

            db_count = repo.db_entry_count()
            if db_count > 0:
                disk_count = repo.count_txt_files_on_disk(self.settings)
                if disk_count == db_count:
                    self.finished.emit({"scanned": db_count, "updated": 0, "deleted": 0, "quick": True})
                    return

            _PROGRESS_INTERVAL = 50
            _last_emitted: list[int] = [-1]  # mutable cell for nested closure

            def _progress(i: int, total: int, name: str) -> None:
                if self._cancelled:
                    raise OperationCancelledError()
                # 最初・最後・_PROGRESS_INTERVAL ごとにだけ emit
                if (i == 1 or i == total or (i - _last_emitted[0]) >= _PROGRESS_INTERVAL):
                    _last_emitted[0] = i
                    self.progress.emit(i, total, name)

            stats = repo.scan_library(
                self.settings,
                progress=_progress,
            )
            self.finished.emit(stats)
        except OperationCancelledError:
            self.finished.emit({"scanned": 0, "updated": 0, "deleted": 0, "cancelled": True})
        except Exception as exc:
            self.failed.emit(str(exc))


class UnmadeWildcardScanWorker(QObject):
    """未作成LoRA一覧をバックグラウンドでスキャンするワーカー。

    create_wildcard() / _import_cm_info_json() が以前は MainWindow._scan_unmade_wildcards()
    をUIスレッド上で同期実行しており、lora_root配下のrglobスキャン＋cm-info.json読み込み＋
    既存ワイルドカードとの突き合わせがダイアログオープン時のプチフリーズの原因になっていた。
    ここではその処理一式をQThread上で実行する。MainWindow（self.entries等）には触れず、
    呼び出し側が事前に構築した検索用haystack文字列とlora_rootパスのみを受け取る。
    """
    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, lora_root: str, haystack: str):
        super().__init__()
        self.lora_root = lora_root
        self.haystack = haystack

    @staticmethod
    def _find_lora_preview(directory: Path, stem: str) -> Path | None:
        for ext in [".preview.png", ".preview.jpg", ".preview.jpeg", ".preview.webp", ".png", ".jpg", ".jpeg", ".webp"]:
            candidate = directory / f"{stem}{ext}"
            if candidate.exists():
                return candidate
        return None

    @Slot()
    def run(self) -> None:
        try:
            lora_root = Path(self.lora_root)
            if not lora_root.exists():
                self.finished.emit([])
                return

            results = []
            for safetensors in lora_root.rglob("*.safetensors"):
                stem = safetensors.stem
                cm_info = safetensors.parent / f"{stem}.cm-info.json"
                if not cm_info.exists():
                    continue

                if stem.lower() in self.haystack:
                    continue

                preview = self._find_lora_preview(safetensors.parent, stem)

                try:
                    with open(cm_info, "r", encoding="utf-8") as f:
                        cm_data = json.load(f)
                except Exception:
                    continue

                results.append({
                    "path": str(safetensors),
                    "name": stem,
                    "preview": str(preview) if preview else None,
                    "trained_words": cm_data.get("TrainedWords", []),
                    "model_name": cm_data.get("ModelName", ""),
                })

            self.finished.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))


class ApiMonitorWorker(QThread):
    """定期的にAPIの接続状態と生成状況を監視するワーカー。

    以前は3秒周期だったが、アイドル時の通信負荷を下げるため5秒周期に延長（H8 修正）。
    また二重の _running チェックをシンプルな単一ループに整理した。
    """
    state_changed = Signal(str)
    POLL_INTERVAL_MS = 5000

    def __init__(self, api: ThumbnailApiClient, get_settings, parent=None):
        super().__init__(parent)
        self.api = api
        self._get_settings = get_settings
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        while self._running:
            settings = self._get_settings()
            try:
                self.api.test_connection(settings)
                try:
                    generating = self.api.is_generating(settings)
                except Exception:
                    generating = False
                self.state_changed.emit("generating" if generating else "connected")
            except Exception:
                self.state_changed.emit("off")
            # 100ms 単位で細切れに sleep することで stop() への応答性を担保する
            slept = 0
            while slept < self.POLL_INTERVAL_MS and self._running:
                self.msleep(100)
                slept += 100


class MissingThumbnailBatchWorker(QObject):
    """不足分サムネイルをバックグラウンドで逐次生成するワーカー（H1 修正）。

    以前は MainWindow.generate_missing_thumbnails() が UI スレッド上で
    QApplication.processEvents() を挟みつつループしており、フリーズや
    リエントラントバグの温床だった。本ワーカーは QThread 上で動作し、
    progress / item_finished / failed / finished シグナルで UI 側に通知する。
    """
    progress = Signal(int, int, str)  # current, total, name
    item_finished = Signal(object)    # updated WildcardEntry
    item_failed = Signal(str, str)    # abs_path, message
    finished = Signal(int, int)       # done, total
    cancelled = Signal()

    def __init__(self, db_path: Path, settings: AppSettings,
                 api: "ThumbnailApiClient",
                 entries: list[WildcardEntry],
                 use_random_wildcard: bool):
        super().__init__()
        self.db_path = db_path
        self.settings = settings
        self.api = api
        self.entries = list(entries)
        self.use_random_wildcard = use_random_wildcard
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        import copy as _copy
        total = len(self.entries)
        done = 0
        worker_settings = _copy.copy(self.settings)
        worker_settings.thumbnail_random_wildcard = self.use_random_wildcard
        for index, entry in enumerate(self.entries, start=1):
            if self._cancelled:
                break
            self.progress.emit(index, total, entry.name)
            try:
                self.api.generate_thumbnail(worker_settings, entry)
                repo = WildcardRepository(self.db_path)
                refreshed = repo.refresh_entry(self.settings, Path(entry.abs_path))
                done += 1
                self.item_finished.emit(refreshed)
            except Exception as exc:
                self.item_failed.emit(entry.abs_path, str(exc))
        if self._cancelled:
            self.cancelled.emit()
        else:
            self.finished.emit(done, total)


class MainWindow(QMainWindow):
    _api_lamp_signal = Signal(str)

    def __init__(self, app_dir: Path):
        super().__init__()
        self.app_dir = app_dir
        self.db_path = app_dir / "wildcard_manager.db"
        self.settings_store = SettingsStore(app_dir)
        self.ui_state_store = UIStateStore(app_dir)
        self.settings = self.ui_state_store.load_into(self.settings_store.load())
        self.repo = WildcardRepository(self.db_path)
        self.api = ThumbnailApiClient()
        self.setAcceptDrops(True)

        self.entries: list[WildcardEntry] = []
        self.filtered_entries: list[WildcardEntry] = []
        self.current_entry: WildcardEntry | None = None
        self.current_folder_prefix = self.settings.last_folder
        self.entry_by_path: dict[str, WildcardEntry] = {}
        self.search_cache: dict[str, str] = {}
        self.toast_popup: ToastPopup | None = None
        self.is_edit_mode = False
        self.cached_load_thread: QThread | None = None
        self.rescan_thread: QThread | None = None
        self.rescan_worker: RescanWorker | None = None
        self.cached_load_worker: CachedEntriesLoader | None = None
        self.unmade_scan_thread: QThread | None = None
        self.unmade_scan_worker: UnmadeWildcardScanWorker | None = None
        self._unmade_scan_progress: QProgressDialog | None = None
        self._pending_dialog_json_path: str | None = None
        self.reload_after_cached_load = False
        self.last_splitter_sizes: list[int] = []
        self.left_panel_width = max(110, self.settings.splitter_sizes[0] if len(self.settings.splitter_sizes) == 3 else 260)
        self.right_panel_width = max(140, self.settings.splitter_sizes[2] if len(self.settings.splitter_sizes) == 3 else 520)
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
        # LRUキャッシュ。本文テキストを保持する。大規模ライブラリでの
        # メモリ膨張を防ぐため上限を設ける（H10 修正）。
        self._content_load_cache: OrderedDict[str, str] = OrderedDict()
        self._content_load_cache_max = 500
        self._suppress_splitter_tracking = False
        self.folder_children_map: dict[str, set[str]] = {}
        self._folder_tree_populating = False
        self._startup_load_requested = False
        # rescan_library の完了コールバックで参照されるため、
        # 初期化前に _on_rescan_finished が発火しても AttributeError に
        # ならないよう __init__ で初期化しておく（B4 修正）。
        self._rescan_initial = False
        # generate_missing_thumbnails 用のバッチワーカー（H1 修正）
        self._missing_thumb_thread: QThread | None = None
        self._missing_thumb_worker: "MissingThumbnailBatchWorker | None" = None

        self._fs_watcher = QFileSystemWatcher(self)
        self._fs_watcher.directoryChanged.connect(self._on_fs_directory_changed)
        self._fs_refresh_timer = QTimer(self)
        self._fs_refresh_timer.setSingleShot(True)
        self._fs_refresh_timer.setInterval(800)
        self._fs_refresh_timer.timeout.connect(self._do_fs_refresh)
        self._update_fs_watcher()

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
            f"""
            QMainWindow, QWidget {{
                background: #0f1216;
                color: #edf2f7;
            }}
            QLineEdit, QTextEdit, QPlainTextEdit, QListWidget, QTreeWidget, QListView, QComboBox, QSpinBox {{
                background: #161b22;
                border: 1px solid #2a3440;
                border-radius: {UI_RADIUS}px;
                color: #edf2f7;
                padding: 6px 10px;
            }}
            QWidget#topControls QLineEdit,
            QWidget#topControls QComboBox {{
                min-height: {TOOLBAR_BUTTON_SIZE}px;
                max-height: {TOOLBAR_BUTTON_SIZE}px;
                height: {TOOLBAR_BUTTON_SIZE}px;
                padding: 0px 6px;
                border-radius: 4px;
                font-size: 11px;
            }}
            QWidget#topControls QPushButton {{
                min-height: {TOOLBAR_BUTTON_SIZE}px;
                max-height: {TOOLBAR_BUTTON_SIZE}px;
                height: {TOOLBAR_BUTTON_SIZE}px;
                padding: 0px;
                border-radius: 4px;
                font-size: 14px;
            }}
            QListView {{
                outline: 0;
                selection-background-color: transparent;
                selection-color: #edf2f7;
            }}
            QListView::item:selected,
            QListView::item:selected:active,
            QListWidget::item:selected,
            QListWidget::item:selected:active,
            QTreeWidget::item:selected,
            QTreeWidget::item:selected:active {{
                background: transparent;
                color: #edf2f7;
            }}
            QPushButton {{
                background: #2e5fb8;
                border: 1px solid #2e5fb8;
                border-radius: {UI_RADIUS}px;
                padding: 0px {UI_BUTTON_PADDING_X}px;
                min-height: {UI_BUTTON_HEIGHT}px;
                max-height: {UI_BUTTON_HEIGHT}px;
                height: {UI_BUTTON_HEIGHT}px;
                color: white;
                font-weight: 600;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: #3a71c6;
                border-color: #3a71c6;
            }}
            QMenuBar {{
                background: #11151b;
                color: #dbe4ee;
                spacing: 2px;
                padding: 4px 8px;
            }}
            QMenuBar::item {{
                background: transparent;
                padding: 4px 10px;
                margin: 0px 1px;
                border-radius: 6px;
            }}
            QMenuBar::item:selected {{
                background: #1d2630;
            }}
            QMenu {{
                background: #151a21;
                color: #edf2f7;
                border: 1px solid #2a3440;
                padding: 6px;
            }}
            QMenu::item {{
                padding: 6px 28px 6px 20px;
                border-radius: 6px;
            }}
            QMenu::item:selected {{
                background: #2e5fb8;
            }}
            QStatusBar {{
                background: #101419;
                color: #aeb8c2;
                border-top: 1px solid #202833;
            }}
            QToolBar {{
                background: #101419;
                border-bottom: 1px solid #202833;
                spacing: {UI_SECTION_SPACING}px;
            }}
            QToolBar QToolButton {{
                background: #161b22;
                border: 1px solid #2a3440;
                border-radius: {UI_RADIUS}px;
                padding: 0px {UI_BUTTON_PADDING_X}px;
                min-height: {UI_BUTTON_HEIGHT}px;
                max-height: {UI_BUTTON_HEIGHT}px;
                height: {UI_BUTTON_HEIGHT}px;
                color: #edf2f7;
                font-size: 11px;
            }}
            QToolBar QToolButton:hover {{
                background: #1f2731;
            }}
            """
        )

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(UI_OUTER_MARGIN, UI_OUTER_MARGIN, UI_OUTER_MARGIN, UI_OUTER_MARGIN)
        root_layout.setSpacing(UI_SECTION_SPACING)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("検索: ファイル名 / 本文 / カスタムタグ / lora:名前")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(200)
        self.search_edit.setFixedHeight(TOOLBAR_BUTTON_SIZE)
        self.search_edit.textChanged.connect(self.schedule_apply_filters)

        self.result_count_label = QLabel("0 件")
        self.result_count_label.setAlignment(Qt.AlignCenter)
        self.result_count_label.setFixedSize(60, TOOLBAR_BUTTON_SIZE)
        self.result_count_label.setStyleSheet("background: #161b22; border: 1px solid #2a3440; border-radius: 4px; color: #edf2f7; font-size: 11px;")

        self.thumbnail_size_decrease_button = QPushButton("-")
        self.thumbnail_size_decrease_button.setFixedSize(TOOLBAR_BUTTON_SIZE, TOOLBAR_BUTTON_SIZE)
        self.thumbnail_size_decrease_button.clicked.connect(lambda: self._step_thumbnail_size(-THUMBNAIL_SIZE_STEP))
        self.thumbnail_size_display = QLabel()
        self.thumbnail_size_display.setAlignment(Qt.AlignCenter)
        self.thumbnail_size_display.setFixedSize(56, TOOLBAR_BUTTON_SIZE)
        self.thumbnail_size_display.setStyleSheet("background: #161b22; border: 1px solid #2a3440; border-radius: 4px; color: #edf2f7; font-size: 11px;")
        self._update_thumbnail_size_display(self.settings.thumbnail_size)
        self.thumbnail_size_increase_button = QPushButton("+")
        self.thumbnail_size_increase_button.setFixedSize(TOOLBAR_BUTTON_SIZE, TOOLBAR_BUTTON_SIZE)
        self.thumbnail_size_increase_button.clicked.connect(lambda: self._step_thumbnail_size(THUMBNAIL_SIZE_STEP))

        self.size_group = QWidget()
        self.size_group.setFixedHeight(TOOLBAR_BUTTON_SIZE)
        size_layout = QHBoxLayout(self.size_group)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.setSpacing(2)
        size_layout.addWidget(self.thumbnail_size_decrease_button)
        size_layout.addWidget(self.thumbnail_size_display)
        size_layout.addWidget(self.thumbnail_size_increase_button)

        dark_button_style = """
            QPushButton {
                background: #1a1d22;
                border: 1px solid #2a3440;
                border-radius: 4px;
                color: #edf2f7;
                font-size: 14px;
                padding: 0px;
                min-height: %dpx;
                max-height: %dpx;
                height: %dpx;
            }
            QPushButton:hover {
                background: #2a2d32;
                border-color: #3a4450;
            }
        """ % (TOOLBAR_BUTTON_SIZE, TOOLBAR_BUTTON_SIZE, TOOLBAR_BUTTON_SIZE)

        self.folder_tree_button = QPushButton("📁")
        self.folder_tree_button.setFixedSize(TOOLBAR_BUTTON_SIZE, TOOLBAR_BUTTON_SIZE)
        self.folder_tree_button.setToolTip("フォルダツリー")
        self.folder_tree_button.clicked.connect(self._switch_to_folder_tab)
        self.folder_tree_button.setStyleSheet(dark_button_style)

        self.lora_button = QPushButton("🔗")
        self.lora_button.setFixedSize(TOOLBAR_BUTTON_SIZE, TOOLBAR_BUTTON_SIZE)
        self.lora_button.setToolTip("LoRA一覧")
        self.lora_button.clicked.connect(self._switch_to_lora_tab)
        self.lora_button.setStyleSheet(dark_button_style)

        self.left_toggle_button = QPushButton("◀")
        self.left_toggle_button.setFixedSize(TOOLBAR_BUTTON_SIZE, TOOLBAR_BUTTON_SIZE)
        self.left_toggle_button.setToolTip("左パネルを表示/非表示")
        self.left_toggle_button.clicked.connect(self.toggle_left_panel)
        self.left_toggle_button.setStyleSheet(dark_button_style)

        self.right_toggle_button = QPushButton("▶")
        self.right_toggle_button.setFixedSize(TOOLBAR_BUTTON_SIZE, TOOLBAR_BUTTON_SIZE)
        self.right_toggle_button.setToolTip("右パネルを表示/非表示")
        self.right_toggle_button.clicked.connect(self.toggle_right_panel)
        self.right_toggle_button.setStyleSheet(dark_button_style)

        self.new_wildcard_button = QPushButton("📝 新規作成")
        self.new_wildcard_button.setFixedHeight(TOOLBAR_BUTTON_SIZE)
        self.new_wildcard_button.setMinimumWidth(80)
        self.new_wildcard_button.setToolTip("ワイルドカード新規作成 (Ctrl+N)")
        self.new_wildcard_button.clicked.connect(lambda checked=False: self.create_wildcard())
        self.new_wildcard_button.setStyleSheet(dark_button_style)

        self.random_generation_button = QPushButton("🎲")
        self.random_generation_button.setFixedSize(TOOLBAR_BUTTON_SIZE, TOOLBAR_BUTTON_SIZE)
        self.random_generation_button.setToolTip("ランダム生成")
        self.random_generation_button.clicked.connect(lambda checked=False: self.open_random_generation())
        self.random_generation_button.setStyleSheet(dark_button_style)

        self.api_lamp = ApiStatusIndicator()
        self.api_lamp.setToolTip("API: 切断")

        self._api_lamp_signal.connect(self.api_lamp.set_state)

        self._api_monitor = ApiMonitorWorker(self.api, lambda: self.settings, self)
        self._api_monitor.state_changed.connect(self._api_lamp_signal.emit)

        self.thumbnail_size_decrease_button.setStyleSheet(dark_button_style)
        self.thumbnail_size_increase_button.setStyleSheet(dark_button_style)

        self.top_controls = QWidget()
        self.top_controls.setObjectName("topControls")
        self.top_controls.setFixedHeight(TOOLBAR_HEIGHT)
        self.top_controls.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        toolbar_layout = QHBoxLayout(self.top_controls)
        toolbar_layout.setContentsMargins(4, 2, 4, 2)
        toolbar_layout.setSpacing(4)
        toolbar_layout.addWidget(self.folder_tree_button)
        toolbar_layout.addWidget(self.lora_button)
        toolbar_layout.addSpacing(8)
        toolbar_layout.addWidget(self.search_edit, 1)
        toolbar_layout.addSpacing(8)
        toolbar_layout.addWidget(self.result_count_label)
        toolbar_layout.addWidget(self.size_group)
        toolbar_layout.addWidget(self.new_wildcard_button)
        toolbar_layout.addWidget(self.random_generation_button)
        toolbar_layout.addSpacing(8)
        toolbar_layout.addWidget(self.api_lamp)
        toolbar_layout.addSpacing(8)
        toolbar_layout.addWidget(self.left_toggle_button)
        toolbar_layout.addWidget(self.right_toggle_button)
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
        self.left_panel.setMinimumWidth(110)
        self.left_panel.setMaximumWidth(360)
        left_layout = QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        self.folder_tree_container = QWidget()
        folder_container_layout = QVBoxLayout(self.folder_tree_container)
        folder_container_layout.setContentsMargins(0, 0, 0, 0)
        folder_container_layout.setSpacing(0)
        folder_container_layout.addWidget(self.folder_tree, 1)

        self.lora_container = QWidget()
        lora_container_layout = QVBoxLayout(self.lora_container)
        lora_container_layout.setContentsMargins(4, 2, 4, 4)
        lora_container_layout.setSpacing(2)
        lora_header_layout = QHBoxLayout()
        lora_header_layout.setContentsMargins(0, 0, 0, 0)
        lora_header_layout.addWidget(QLabel("LoRAフィルタ"))
        lora_header_layout.addStretch(1)
        lora_container_layout.addLayout(lora_header_layout)
        self.lora_deselect_all = QCheckBox("すべて外す")
        self.lora_deselect_all.setChecked(True)
        self.lora_deselect_all.setStyleSheet("color:#edf2f7;font-size:11px")
        self.lora_deselect_all.toggled.connect(self._on_lora_deselect_all)
        lora_container_layout.addWidget(self.lora_deselect_all)
        self.lora_cloud = QListWidget()
        self.lora_cloud.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lora_cloud.customContextMenuRequested.connect(self._show_lora_cloud_context_menu)
        self.lora_cloud.itemChanged.connect(self._on_lora_item_changed)
        lora_container_layout.addWidget(self.lora_cloud, 1)

        left_layout.addWidget(self.folder_tree_container)
        left_layout.addWidget(self.lora_container)
        self.splitter.addWidget(self.left_panel)

        center = QWidget()
        center.setMinimumWidth(520)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(UI_SECTION_SPACING)

        self.card_view = SlowWheelListView(scroll_step=self.settings.scroll_step)
        self.card_view.setModel(self.list_model)
        self.card_view.setItemDelegate(self.item_delegate)
        self.card_view.setViewMode(QListView.IconMode)
        self.card_view.setResizeMode(QListView.Adjust)
        self.card_view.setMovement(QListView.Static)
        self.card_view.setWrapping(True)
        self.card_view.setLayoutMode(QListView.Batched)
        self.card_view.setBatchSize(64)
        self.card_view.setWordWrap(True)
        self.card_view.setSpacing(UI_SECTION_SPACING)
        self.card_view.setUniformItemSizes(False)
        self.card_view.setSelectionMode(QListView.ExtendedSelection)
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
        self.right_panel.setMinimumWidth(180)
        self.right_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(UI_SECTION_SPACING)

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
        right_content_layout.setSpacing(UI_SECTION_SPACING)

        self.path_label = QLabel("ファイル未選択")
        self.path_label.setMinimumWidth(0)
        self.path_label.setMinimumHeight(42)
        self.path_label.setWordWrap(False)
        self.path_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.path_label.setIndent(12)
        self.path_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.path_label.setStyleSheet("background: #151a21; border: 1px solid #2a3440; border-radius: 10px; color: #d4dbe3;")
        self._path_label_text = "ファイル未選択"

        preview_panel = QFrame()
        preview_panel.setObjectName("detailPreviewPanel")
        preview_panel.setMinimumWidth(0)
        preview_panel.setStyleSheet(
            "QFrame#detailPreviewPanel {"
            " background: #131820;"
            " border: 1px solid #2a3440;"
            " border-radius: 14px;"
            "}"
        )
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(UI_CARD_PADDING_X, UI_CARD_PADDING_Y, UI_CARD_PADDING_X, UI_CARD_PADDING_Y)
        preview_layout.setSpacing(UI_SECTION_SPACING)

        self.thumbnail_label = ResponsivePixmapLabel("サムネイルなし")
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label.setMinimumHeight(260)
        self.thumbnail_label.setMaximumHeight(360)
        self.thumbnail_label.setMinimumWidth(0)
        self.thumbnail_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.thumbnail_label.setStyleSheet("border: 1px solid #2a3440; border-radius: 14px; background: #0d1014;")
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
            " background: #131820;"
            " border: 1px solid #2a3440;"
            " border-radius: 14px;"
            "}"
        )
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(UI_CARD_PADDING_X, UI_CARD_PADDING_Y, UI_CARD_PADDING_X, UI_CARD_PADDING_Y)
        editor_layout.setSpacing(UI_SECTION_SPACING)
        copy_buttons_row = QHBoxLayout()
        copy_buttons_row.setSpacing(UI_SECTION_SPACING)
        self.copy_all_with_lora_button = QPushButton("プロンプトコピー（LORAあり）")
        self.copy_all_with_lora_button.clicked.connect(lambda: self.copy_all_text(True))
        self.copy_all_without_lora_button = QPushButton("プロンプトコピー（LORAなし）")
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
        editor_header_row.setSpacing(UI_SECTION_SPACING)
        editor_header_row.addWidget(QLabel("元テキスト"))
        editor_header_row.addStretch(1)
        self.delete_tags_button = QPushButton("削除")
        self.delete_tags_button.setFixedHeight(UI_BUTTON_HEIGHT)
        self.delete_tags_button.setStyleSheet(
            "QPushButton { background: #6a2a2a; border: 1px solid #8a3a3a; border-radius: 6px;"
            " color: #ffcccc; font-size: 11px; padding: 0 10px; }"
            "QPushButton:hover { background: #8a3a3a; }"
            "QPushButton:disabled { background: #3a2a2a; color: #6a5a5a; }"
        )
        self.delete_tags_button.setEnabled(False)
        self.delete_tags_button.clicked.connect(self._delete_selected_tags)
        self.delete_tags_button.hide()
        editor_header_row.addWidget(self.delete_tags_button)
        self.edit_button = QPushButton("編集")
        self.edit_button.clicked.connect(self.toggle_edit_mode)
        editor_header_row.addWidget(self.edit_button)
        editor_layout.addLayout(editor_header_row)
        self.editor = QTextEdit()
        self.editor.setMinimumHeight(80)
        self.editor.setMinimumWidth(0)
        self.editor.setReadOnly(True)
        self.editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.editor.textChanged.connect(self._sync_tags_from_editor)
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
        self._apply_top_controls_compact_height()
        self._refresh_path_label()
        self._update_panel_toggle_buttons()

        # コンテキストメニューを日本語化
        _setup_japanese_context_menu(self.search_edit)
        _setup_japanese_context_menu(self.editor)

        self.setCentralWidget(root)
        status_bar = QStatusBar()
        status_bar.setStyleSheet("""
            QStatusBar {
                background: #101419;
                color: #aeb8c2;
                border-top: 1px solid #202833;
                font-size: 11px;
            }
        """)
        self.setStatusBar(status_bar)

        self.card_view.selectionModel().currentChanged.connect(self._on_card_selection_changed)
        self._update_view_sizes()
        self.statusBar().showMessage("起動準備完了", 2000)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._startup_load_requested:
            self._startup_load_requested = True
            QTimer.singleShot(150, self.load_cached_entries)
            QTimer.singleShot(500, lambda: self._api_monitor.start())

    def _create_actions(self) -> None:
        menubar = self.menuBar()
        menubar.setNativeMenuBar(False)

        def add_action(label: str, callback, shortcut: str | None = None) -> QAction:
            action = QAction(label, self)
            if callback is not None:
                action.triggered.connect(callback)
            if shortcut:
                action.setShortcut(shortcut)
            return action

        # File menu
        self.action_new_wildcard = add_action("ワイルドカード新規作成", self.create_wildcard, "Ctrl+N")
        self.action_rescan = add_action("ライブラリを再読込", self.rescan_library, "F5")
        self.action_settings = add_action("設定", self.open_settings, "Ctrl+,")
        self.action_exit = add_action("終了", self.close, "Ctrl+Q")

        # Edit menu
        self.action_edit = add_action("編集モード", self.toggle_edit_mode, "Ctrl+E")
        self.action_copy_with_lora = add_action("プロンプトコピー（LORAあり）", lambda: self.copy_all_text(True), "Ctrl+C")
        self.action_copy_without_lora = add_action("プロンプトコピー（LORAなし）", lambda: self.copy_all_text(False), "Ctrl+Shift+C")
        self.action_delete_card = add_action("カードを削除", self._delete_selected_cards, "Delete")

        # View menu
        self.action_zoom_in = add_action("サムネイルを大きく", lambda: self._step_thumbnail_size(THUMBNAIL_SIZE_STEP), "Ctrl+=")
        self.action_zoom_out = add_action("サムネイルを小さく", lambda: self._step_thumbnail_size(-THUMBNAIL_SIZE_STEP), "Ctrl+-")
        self.action_toggle_left_panel = add_action("左パネルを表示/非表示", self.toggle_left_panel, "Ctrl+1")
        self.action_toggle_right_panel = add_action("右パネルを表示/非表示", self.toggle_right_panel, "Ctrl+2")
        self.action_toggle_folder_tree = add_action("フォルダツリーを表示/非表示", self._toggle_folder_tree_visibility, "Ctrl+3")

        # Sort actions (used in Sort menu with QActionGroup)
        self.action_sort_key_name = add_action("ファイル名", lambda: self._set_sort_key("name"))
        self.action_sort_key_size = add_action("サイズ", lambda: self._set_sort_key("size"))
        self.action_sort_key_created = add_action("作成日順", lambda: self._set_sort_key("created"))
        self.action_sort_key_updated = add_action("更新日順", lambda: self._set_sort_key("updated"))
        self.action_sort_key_path = add_action("パス順", lambda: self._set_sort_key("path"))
        self.action_sort_key_folder = add_action("フォルダ順", lambda: self._set_sort_key("folder"))
        self.action_sort_order_asc = add_action("昇順", lambda: self._set_sort_order("asc"))
        self.action_sort_order_desc = add_action("降順", lambda: self._set_sort_order("desc"))

        for act in (
            self.action_sort_key_name, self.action_sort_key_size,
            self.action_sort_key_created, self.action_sort_key_updated,
            self.action_sort_key_path, self.action_sort_key_folder,
            self.action_sort_order_asc, self.action_sort_order_desc,
        ):
            act.setCheckable(True)

        self._sort_key_group = QActionGroup(self)
        self._sort_key_group.setExclusive(True)
        self._sort_key_group.addAction(self.action_sort_key_name)
        self._sort_key_group.addAction(self.action_sort_key_size)
        self._sort_key_group.addAction(self.action_sort_key_created)
        self._sort_key_group.addAction(self.action_sort_key_updated)
        self._sort_key_group.addAction(self.action_sort_key_path)
        self._sort_key_group.addAction(self.action_sort_key_folder)

        self._sort_order_group = QActionGroup(self)
        self._sort_order_group.setExclusive(True)
        self._sort_order_group.addAction(self.action_sort_order_asc)
        self._sort_order_group.addAction(self.action_sort_order_desc)

        self._update_sort_checkmarks()

        # Tools menu
        self.action_thumbnail_generate = add_action("サムネイルを生成", self.generate_thumbnail_for_current, "Ctrl+G")
        self.action_thumbnail_missing = add_action("不足サムネイルを一括生成", self.generate_missing_thumbnails)
        self.action_random_generation = add_action("ランダム生成", lambda: self.open_random_generation(), "Ctrl+R")
        self.action_import = add_action("ワイルドカードを取り込み", lambda: self.run_import("copy"), "Ctrl+I")

        # Help menu
        self.action_about = add_action("このアプリについて", self._show_about)
        self.action_help_shortcuts = add_action("キーボードショートカット", self._show_shortcuts_help, "F1")

        # Build menus
        file_menu = menubar.addMenu("ファイル(F)")
        file_menu.addAction(self.action_rescan)
        file_menu.addSeparator()
        file_menu.addAction(self.action_import)
        file_menu.addSeparator()
        file_menu.addAction(self.action_exit)

        edit_menu = menubar.addMenu("編集(E)")
        edit_menu.addAction(self.action_edit)
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_copy_with_lora)
        edit_menu.addAction(self.action_copy_without_lora)
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_delete_card)

        view_menu = menubar.addMenu("表示(V)")
        view_menu.addAction(self.action_zoom_in)
        view_menu.addAction(self.action_zoom_out)
        view_menu.addSeparator()
        view_menu.addAction(self.action_toggle_left_panel)
        view_menu.addAction(self.action_toggle_right_panel)
        view_menu.addAction(self.action_toggle_folder_tree)

        sort_menu = menubar.addMenu("並び替え(S)")
        sort_menu.addAction(self.action_sort_key_name)
        sort_menu.addAction(self.action_sort_key_size)
        sort_menu.addAction(self.action_sort_key_created)
        sort_menu.addAction(self.action_sort_key_updated)
        sort_menu.addAction(self.action_sort_key_path)
        sort_menu.addAction(self.action_sort_key_folder)
        sort_menu.addSeparator()
        sort_menu.addAction(self.action_sort_order_asc)
        sort_menu.addAction(self.action_sort_order_desc)

        tools_menu = menubar.addMenu("ツール(T)")
        tools_menu.addAction(self.action_new_wildcard)
        tools_menu.addSeparator()
        tools_menu.addAction(self.action_thumbnail_generate)
        tools_menu.addAction(self.action_thumbnail_missing)
        tools_menu.addSeparator()
        tools_menu.addAction(self.action_random_generation)

        settings_menu = menubar.addMenu("設定")
        settings_menu.addAction(self.action_settings)
        settings_menu.addSeparator()
        self.action_settings_common = add_action("共通設定", lambda: self.open_settings(0))
        self.action_settings_management = add_action("ワイルドカード管理設定", lambda: self.open_settings(1))
        self.action_settings_thumbnail = add_action("サムネイル設定", lambda: self.open_settings(2))
        self.action_settings_random = add_action("ランダム生成の初期値", lambda: self.open_settings(3))
        settings_menu.addAction(self.action_settings_common)
        settings_menu.addAction(self.action_settings_management)
        settings_menu.addAction(self.action_settings_thumbnail)
        settings_menu.addAction(self.action_settings_random)

        help_menu = menubar.addMenu("ヘルプ(H)")
        help_menu.addAction(self.action_help_shortcuts)
        help_menu.addAction(self.action_about)

    def _show_about(self) -> None:
        self._show_info(
            "Wildcard Manager",
            "Wildcard Manager\n\nランダム生成と wildcard 管理用の GUI です。",
        )

    def _show_shortcuts_help(self) -> None:
        shortcuts = """
        <b>キーボードショートカット</b><br><br>
        <b>ファイル</b><br>
        Ctrl+N - ワイルドカード新規作成<br>
        F5 - ライブラリを再読込<br>
        Ctrl+I - ワイルドカードを取り込み<br>
        Ctrl+, - 設定<br>
        Ctrl+Q - 終了<br><br>
        <b>編集</b><br>
        Ctrl+E - 編集モード<br>
        Ctrl+C - プロンプトコピー（LORAあり）<br>
        Ctrl+Shift+C - プロンプトコピー（LORAなし）<br>
        Del - カードを削除<br><br>
        <b>表示</b><br>
        Ctrl+= - サムネイルを大きく<br>
        Ctrl+- - サムネイルを小さく<br>
        Ctrl+1 - 左パネルを表示/非表示<br>
        Ctrl+2 - 右パネルを表示/非表示<br>
        Ctrl+3 - フォルダツリーを表示/非表示<br><br>
        <b>ツール</b><br>
        Ctrl+G - サムネイルを生成<br>
        Ctrl+R - ランダム生成<br><br>
        <b>メニュー</b><br>
        ファイル - 新規作成・取り込み<br>
        編集 - 編集モード・コピー<br>
        表示 - サムネイル・パネル表示<br>
        並び替え - ソート条件・順序<br>
        設定 - アプリ設定
        """
        self._show_info("キーボードショートカット", shortcuts)

    def _toggle_folder_tree_visibility(self) -> None:
        self.folder_tree.setVisible(not self.folder_tree.isVisible())

    def _switch_to_folder_tab(self) -> None:
        self.folder_tree_container.setVisible(not self.folder_tree_container.isVisible())

    def _switch_to_lora_tab(self) -> None:
        self.lora_container.setVisible(not self.lora_container.isVisible())

    def _sort_entries(self, entries: list[WildcardEntry]) -> list[WildcardEntry]:
        sort_key = getattr(self.settings, "sort_key", "name")
        sort_order = getattr(self.settings, "sort_order", "asc")
        reverse = sort_order == "desc"

        if sort_key == "size":
            def sort_func(entry: WildcardEntry) -> tuple:
                try:
                    size = Path(entry.abs_path).stat().st_size
                except OSError:
                    size = 0
                return (size,)
        elif sort_key == "created":
            def sort_func(entry: WildcardEntry) -> tuple:
                try:
                    ctime = Path(entry.abs_path).stat().st_ctime
                except OSError:
                    ctime = 0
                return (ctime,)
        elif sort_key == "updated":
            def sort_func(entry: WildcardEntry) -> tuple:
                try:
                    mtime = Path(entry.abs_path).stat().st_mtime
                except OSError:
                    mtime = 0
                return (mtime,)
        elif sort_key == "path":
            def sort_func(entry: WildcardEntry) -> tuple:
                return (natural_sort_key(entry.rel_path),)
        elif sort_key == "folder":
            def sort_func(entry: WildcardEntry) -> tuple:
                return (natural_sort_key(entry.folder), natural_sort_key(entry.name))
        else:
            def sort_func(entry: WildcardEntry) -> tuple:
                return (natural_sort_key(entry.name),)

        return sorted(entries, key=sort_func, reverse=reverse)

    def _set_sort_key(self, key: str) -> None:
        self.settings.sort_key = key
        self._update_sort_checkmarks()
        self.apply_filters()

    def _set_sort_order(self, order: str) -> None:
        self.settings.sort_order = order
        self._update_sort_checkmarks()
        self.apply_filters()

    def _update_sort_checkmarks(self) -> None:
        sort_key = getattr(self.settings, "sort_key", "name")
        sort_order = getattr(self.settings, "sort_order", "asc")

        self.action_sort_key_name.setChecked(sort_key == "name")
        self.action_sort_key_size.setChecked(sort_key == "size")
        self.action_sort_key_created.setChecked(sort_key == "created")
        self.action_sort_key_updated.setChecked(sort_key == "updated")
        self.action_sort_key_path.setChecked(sort_key == "path")
        self.action_sort_key_folder.setChecked(sort_key == "folder")

        self.action_sort_order_asc.setChecked(sort_order == "asc")
        self.action_sort_order_desc.setChecked(sort_order == "desc")

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

    def _apply_uniform_control_height(self, height: int = UI_CONTROL_HEIGHT) -> None:
        widgets = [
            self.search_edit,
            self.thumbnail_size_display,
        ]
        for widget in widgets:
            widget.setFixedHeight(height)

        button_widgets = [
            self.thumbnail_size_decrease_button,
            self.thumbnail_size_increase_button,
            self.copy_all_with_lora_button,
            self.copy_all_without_lora_button,
            self.edit_button,
        ]
        for widget in button_widgets:
            widget.setFixedHeight(UI_BUTTON_HEIGHT)

        self.path_label.setMinimumHeight(height)
        self.path_label.setMaximumHeight(height)

    def _apply_top_controls_compact_height(self) -> None:
        for widget in (
            self.search_edit,
            self.thumbnail_size_display,
            self.random_generation_button,
            self.thumbnail_size_decrease_button,
            self.thumbnail_size_increase_button,
            self.folder_tree_button,
            self.lora_button,
            self.left_toggle_button,
            self.right_toggle_button,
            self.result_count_label,
        ):
            widget.setFixedHeight(TOOLBAR_BUTTON_SIZE)
        self.top_controls.setFixedHeight(TOOLBAR_HEIGHT)

    def _set_result_count_label(self, count: int) -> None:
        self.result_count_label.setText(f"{count} 件")
        folder_info = self.current_folder_prefix or "すべて"
        thumb_count = sum(1 for e in self.filtered_entries if e.has_thumbnail)
        self.statusBar().showMessage(
            f"{count}件表示中 | フォルダ: {folder_info} | サムネ: {thumb_count}/{count}",
            0
        )

    def _refresh_path_label(self) -> None:
        # L9 修正: _path_label_text は _build_ui で確実に初期化されるため、
        # 防衛的な getattr は不要。直接属性アクセスに変更。
        raw_text = self._path_label_text or "ファイル未選択"
        available_width = max(80, self.path_label.width() - 12)
        text = self.path_label.fontMetrics().elidedText(raw_text, Qt.ElideMiddle, available_width)
        self.path_label.setText(text)

    def _update_view_sizes(self) -> None:
        width = max(80, self.settings.thumbnail_size)
        height = int(width * CARD_ICON_RATIO)
        # setGridSize/setIconSizeは値が変化しないと内部レイアウトの再計算を
        # スキップすることがあるため、一度リセットしてから設定し直す。
        self.card_view.setGridSize(QSize())
        self.card_view.setIconSize(QSize(width, height))
        self.card_view.setGridSize(QSize(width + CARD_FRAME_PADDING, height + CARD_TITLE_HEIGHT + CARD_FRAME_PADDING))
        self.card_view.updateGeometries()
        self.card_view.scheduleDelayedItemsLayout()
        self.card_view.viewport().update()
        QTimer.singleShot(0, self.card_view.doItemsLayout)

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
        # M9 修正: _pending_preview_request は書き込まれるだけで読み出されない
        # デッド状態変数だったため削除。トークンベースの破棄で十分機能する。
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
        pass

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
        left = sizes[0]
        right = sizes[2]
        center = max(0, total - left - right)
        if left > 0:
            left = max(180, left)
        if right > 0:
            right = max(320, right)
        if center < 480:
            center = 480
            remaining = max(0, total - center)
            if left > 0:
                left = max(180, min(left, remaining // 2))
            if right > 0:
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
        self.left_toggle_button.setText("◀" if left_visible else "▶")
        self.left_toggle_button.setToolTip("左パネルを隠す" if left_visible else "左パネルを表示")
        self.right_toggle_button.setText("▶" if right_visible else "◀")
        self.right_toggle_button.setToolTip("右パネルを隠す" if right_visible else "右パネルを表示")

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

    def open_settings(self, tab_index: int = -1) -> None:
        dialog = SettingsDialog(self.settings, self.repo, self.api, self)
        if tab_index >= 0:
            dialog.set_current_tab(tab_index)
        QTimer.singleShot(0, lambda: self._center_window(dialog))
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self.settings = dialog.build_settings(self.settings)
            self.settings_store.save(self.settings)
            self._update_thumbnail_size_display(self.settings.thumbnail_size)
            self.list_model.set_thumbnail_size(self.settings.thumbnail_size)
            self._update_view_sizes()
            self._update_fs_watcher()
            self.statusBar().showMessage("設定を保存しました", 4000)
            self.load_cached_entries()
        except Exception as exc:
            self._show_error("設定保存エラー", str(exc))

    def open_random_generation(self, wildcard_entry: WildcardEntry | None = None) -> None:
        preset_prompt_text = ""
        if wildcard_entry is not None:
            preset_prompt_text = wildcard_entry.first_line.strip()
            if not preset_prompt_text and wildcard_entry.content:
                preset_prompt_text = extract_prompt_line(wildcard_entry.content)
            if not preset_prompt_text:
                try:
                    preset_prompt_text = extract_prompt_line(self.repo.load_entry_content(Path(wildcard_entry.abs_path)))
                except Exception:
                    preset_prompt_text = ""

        dialog = RandomGenerationDialog(self.settings, self.repo, self.api, self, preset_prompt_text=preset_prompt_text)
        QTimer.singleShot(0, lambda: self._center_window(dialog))
        dialog.exec()
        try:
            self.settings = dialog.build_settings(self.settings)
            self.settings_store.save(self.settings)
        except Exception as exc:
            self._show_error("ランダム生成設定の保存エラー", str(exc))

    def create_wildcard(self) -> None:
        self._start_unmade_wildcard_scan(json_path=None)

    def _import_cm_info_json(self, json_path: str) -> None:
        self.statusBar().showMessage(f"JSON読み込み中: {Path(json_path).name}", 2000)
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                cm_data = json.load(f)
        except Exception as exc:
            self._show_error("JSON読み込みエラー", str(exc))
            return

        self._start_unmade_wildcard_scan(json_path=json_path)

    def _start_unmade_wildcard_scan(self, *, json_path: str | None) -> None:
        """未作成LoRA一覧のスキャンをバックグラウンドスレッドで開始し、完了後にダイアログを開く。

        以前は self._scan_unmade_wildcards() をUIスレッド上で同期実行してから
        NewWildcardDialog を生成していたため、lora_root配下のファイル数や
        ワイルドカード総数が多い環境ではダイアログを開く操作自体がプチフリーズしていた。
        """
        if not self.settings.lora_root:
            self._launch_new_wildcard_dialog(None, json_path)
            return
        if self.unmade_scan_thread is not None and self.unmade_scan_thread.isRunning():
            return

        self._pending_dialog_json_path = json_path
        haystack = self._build_lora_reference_haystack()

        self._unmade_scan_progress = self._create_progress_dialog(
            "未作成LoRA検索", "未作成のLoRAを検索中...", can_cancel=False
        )

        self.unmade_scan_thread = QThread(self)
        self.unmade_scan_worker = UnmadeWildcardScanWorker(self.settings.lora_root, haystack)
        self.unmade_scan_worker.moveToThread(self.unmade_scan_thread)
        self.unmade_scan_thread.started.connect(self.unmade_scan_worker.run)
        self.unmade_scan_worker.finished.connect(self._on_unmade_scan_finished)
        self.unmade_scan_worker.failed.connect(self._on_unmade_scan_failed)
        self.unmade_scan_worker.finished.connect(self._cleanup_unmade_scan_worker)
        self.unmade_scan_worker.failed.connect(self._cleanup_unmade_scan_worker)
        self.unmade_scan_thread.start()

    def _on_unmade_scan_finished(self, items: list[dict]) -> None:
        json_path = self._pending_dialog_json_path
        self._launch_new_wildcard_dialog(items, json_path)

    def _on_unmade_scan_failed(self, message: str) -> None:
        self._show_error("未作成LoRA検索エラー", message)
        json_path = self._pending_dialog_json_path
        self._launch_new_wildcard_dialog(None, json_path)

    def _cleanup_unmade_scan_worker(self) -> None:
        dlg = self._unmade_scan_progress
        if dlg is not None:
            dlg.close()
            self._unmade_scan_progress = None
        if self.unmade_scan_thread is not None:
            self.unmade_scan_thread.quit()
            self.unmade_scan_thread.wait(2000)
            self.unmade_scan_thread.deleteLater()
        if self.unmade_scan_worker is not None:
            self.unmade_scan_worker.deleteLater()
        self.unmade_scan_thread = None
        self.unmade_scan_worker = None
        self._pending_dialog_json_path = None

    def _launch_new_wildcard_dialog(self, unmade: list[dict] | None, json_path: str | None) -> None:
        dialog = NewWildcardDialog(
            self.settings, self.repo, self.api, self.folder_children_map,
            unmade_items=unmade, custom_tabs=self.settings.new_wildcard_custom_tabs,
            parent=self
        )
        QTimer.singleShot(0, lambda: self._center_window(dialog))
        if json_path:
            dialog.import_json_path(json_path)
        dialog.exec()
        # WA_DeleteOnClose下では、exec()がここへ戻ってきた時点で既に
        # ダイアログ（とself.tabsなどのC++実体）が破棄されている場合がある。
        # そのため exec() の後から dialog.get_custom_tabs() を呼ぶと
        # 「RuntimeError: Internal C++ object already deleted」になり得る。
        # 代わりに、ダイアログ自身が accept()/reject() の時点（まだ生きている間）
        # にスナップショットしておいた custom_tabs_result（プレーンなlist属性）を読む。
        self.settings.new_wildcard_custom_tabs = dialog.custom_tabs_result
        self.settings_store.save(self.settings)
        if not dialog.created_entries:
            return
        for entry in dialog.created_entries:
            refreshed = self.repo.refresh_entry(self.settings, Path(entry.abs_path))
            self.entries.append(refreshed)
        self._rebuild_entry_indexes()
        self.apply_filters()
        self.statusBar().showMessage(f"{len(dialog.created_entries)} 件のワイルドカードを作成しました", 4000)

    def _build_lora_reference_haystack(self) -> str:
        """全エントリのsearch_textを結合した小文字検索ブロブを構築する（LoRA重複チェック用）。

        以前は _has_wildcard_for_lora() が LoRA 1件ごとに self.entries 全件を
        ループして entry.search_text.lower() を再計算しており、
        計算量が O(LoRA数 × エントリ数) になっていた（ダイアログを開く際のプチフリーズの主因）。
        ここで1回だけ結合・小文字化しておき、以降は単純な部分文字列検索で済ませる。
        """
        # search_cache は abs_path -> search_text の辞書（_rebuild_entry_indexes 等で同期済み）
        return "\n".join(text.lower() for text in self.search_cache.values())

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                filepath = url.toLocalFile()
                if filepath.endswith(".cm-info.json"):
                    event.acceptProposedAction()
                    event.accept()
                    return
        event.ignore()

    def dropEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                filepath = url.toLocalFile()
                if filepath.endswith(".cm-info.json"):
                    try:
                        self._import_cm_info_json(filepath)
                    except Exception as exc:
                        self._show_error("インポートエラー", str(exc))
            event.accept()
        else:
            event.ignore()

    def load_cached_entries(self) -> None:
        if self.cached_load_thread is not None and self.cached_load_thread.isRunning():
            self.reload_after_cached_load = True
            return
        self.reload_after_cached_load = False
        self.statusBar().showMessage("キャッシュを読み込み中...", 0)

        self.cached_load_thread = QThread(self)
        self.cached_load_worker = CachedEntriesLoader(self.db_path, self.settings)
        self.cached_load_worker.moveToThread(self.cached_load_thread)
        self.cached_load_thread.started.connect(self.cached_load_worker.run)
        self.cached_load_worker.finished.connect(self._on_cached_entries_loaded)
        self.cached_load_worker.failed.connect(self._on_cached_entries_failed)
        self.cached_load_worker.finished.connect(self._cleanup_cached_entries_loader)
        self.cached_load_worker.failed.connect(self._cleanup_cached_entries_loader)
        self.cached_load_thread.start()

    def _on_cached_entries_loaded(self, cached_entries: list[WildcardEntry]) -> None:
        try:
            seen_paths: set[str] = set()
            deduped: list[WildcardEntry] = []
            for entry in cached_entries:
                if entry.rel_path in seen_paths:
                    continue
                seen_paths.add(entry.rel_path)
                deduped.append(entry)
            self.entries = deduped
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
            self.apply_filters()
            self._populate_lora_cloud()
            # The initial async load can arrive after the first layout pass, so
            # force one more geometry refresh once real items are in the model.
            QTimer.singleShot(0, self._update_view_sizes)
            count = len(self.entries)
            if count == 0:
                # DBが空 = 一度もスキャンされていない。自動でスキャンを開始する。
                self.statusBar().showMessage("ライブラリが空です。初回スキャンを開始します...", 0)
                QTimer.singleShot(200, lambda: self.rescan_library(initial=True))
            else:
                self.statusBar().showMessage(f"キャッシュ {count} 件を読み込みました。", 6000)
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
        if self.rescan_thread is not None and self.rescan_thread.isRunning():
            return
        self._rescan_initial = initial
        self.action_rescan.setEnabled(False)

        self._rescan_progress = self._create_progress_dialog("再読込", "ライブラリを再読込中...", can_cancel=True)
        self._rescan_progress.canceled.connect(self._cancel_rescan)
        self._rescan_progress.show()

        self.rescan_thread = QThread(self)
        self.rescan_worker = RescanWorker(self.db_path, self.settings)
        self.rescan_worker.moveToThread(self.rescan_thread)
        self.rescan_thread.started.connect(self.rescan_worker.run)
        self.rescan_worker.progress.connect(self._on_rescan_progress)
        self.rescan_worker.finished.connect(self._on_rescan_finished)
        self.rescan_worker.failed.connect(self._on_rescan_failed)
        self.rescan_worker.finished.connect(self._cleanup_rescan_worker)
        self.rescan_worker.failed.connect(self._cleanup_rescan_worker)
        self.rescan_thread.start()

    def _on_rescan_progress(self, current: int, total: int, name: str) -> None:
        dlg = getattr(self, "_rescan_progress", None)
        if dlg is None:
            return
        if total > 0:
            dlg.setMaximum(total)
            dlg.setValue(current)
            pct = int(current / total * 100)
            dlg.setLabelText(f"スキャン中 {current} / {total} ({pct}%)\n{elide(name, 60)}")
        else:
            dlg.setLabelText(f"スキャン中 {current}\n{elide(name, 60)}")

    def _cancel_rescan(self) -> None:
        if self.rescan_worker is not None:
            self.rescan_worker.cancel()

    def _on_rescan_finished(self, stats: dict) -> None:
        if stats.get("cancelled"):
            self.statusBar().showMessage("再読込をキャンセルしました", 4000)
            return
        self.statusBar().showMessage(
            f"再読込完了 scanned={stats['scanned']} updated={stats['updated']} deleted={stats['deleted']}",
            6000,
        )
        if stats.get('updated', 0) == 0 and stats.get('deleted', 0) == 0:
            return
        QTimer.singleShot(0, self.load_cached_entries)
        if self._rescan_initial and stats.get("scanned", 0) == 0:
            self.statusBar().showMessage(
                "ライブラリは空です。設定と取り込み元パスを確認してください。", 8000
            )

    def _on_rescan_failed(self, message: str) -> None:
        self._show_error("再読込エラー", message)

    def _cleanup_rescan_worker(self) -> None:
        dlg = getattr(self, "_rescan_progress", None)
        if dlg is not None:
            dlg.close()
            self._rescan_progress = None
        self.action_rescan.setEnabled(True)
        if self.rescan_thread is not None:
            self.rescan_thread.quit()
            self.rescan_thread.wait(2000)
            self.rescan_thread.deleteLater()
        if self.rescan_worker is not None:
            self.rescan_worker.deleteLater()
        self.rescan_thread = None
        self.rescan_worker = None

    def _on_fs_directory_changed(self, path: str) -> None:
        self._fs_refresh_timer.start()

    def _do_fs_refresh(self) -> None:
        if self.rescan_thread is not None and self.rescan_thread.isRunning():
            return
        if self.cached_load_thread is not None and self.cached_load_thread.isRunning():
            return
        self.rescan_library(initial=False)

    def _update_fs_watcher(self) -> None:
        watched = self._fs_watcher.directories()
        if watched:
            self._fs_watcher.removePaths(watched)
        if self.settings.library_root and Path(self.settings.library_root).exists():
            self._fs_watcher.addPath(self.settings.library_root)

    def _step_progress(self, dialog: QProgressDialog, value: int, maximum: int, name: str) -> bool:
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
            if self.folder_children_map.get(child_key):
                child.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
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
        self.schedule_apply_filters()

    def _populate_lora_cloud(self) -> None:
        self.lora_cloud.clear()
        all_lora: dict[str, int] = {}
        for entry in self.entries:
            for name in entry.lora_names:
                all_lora[name] = all_lora.get(name, 0) + 1
        for name, count in sorted(all_lora.items(), key=lambda x: -x[1]):
            item = QListWidgetItem(f"{name} ({count})")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, name)
            self.lora_cloud.addItem(item)

    def _on_lora_deselect_all(self, checked: bool) -> None:
        state = Qt.Checked if not checked else Qt.Unchecked
        self.lora_cloud.blockSignals(True)
        for i in range(self.lora_cloud.count()):
            self.lora_cloud.item(i).setCheckState(state)
        self.lora_cloud.blockSignals(False)
        self.schedule_apply_filters()

    def _on_lora_item_changed(self, item: QListWidgetItem) -> None:
        any_checked = any(
            self.lora_cloud.item(i).checkState() == Qt.Checked
            for i in range(self.lora_cloud.count())
        )
        self.lora_deselect_all.blockSignals(True)
        self.lora_deselect_all.setChecked(not any_checked)
        self.lora_deselect_all.blockSignals(False)
        self.schedule_apply_filters()

    def _show_lora_cloud_context_menu(self, pos) -> None:
        item = self.lora_cloud.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        toggle_action = menu.addAction("チェック切替")
        action = menu.exec(self.lora_cloud.viewport().mapToGlobal(pos))
        if action == toggle_action:
            current = item.checkState()
            item.setCheckState(Qt.Unchecked if current == Qt.Checked else Qt.Checked)

    def _check_lora_in_cloud(self, name: str) -> None:
        for i in range(self.lora_cloud.count()):
            item = self.lora_cloud.item(i)
            if item.data(Qt.UserRole) == name:
                item.setCheckState(Qt.Checked)
                self.lora_cloud.scrollToItem(item)
                return

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
        # folder_key は POSIX 区切り（"/"）なので OS 非依存に合成する。
        # 以前は folder_key.replace("/", "\\") を使って Windows 専用になっていた。
        target = base if not folder_key else base / Path(*folder_key.split("/"))
        from .ui_utils import open_in_file_manager
        if target.exists():
            open_in_file_manager(target)
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

    def _delete_card(self, entry: WildcardEntry) -> None:
        result = self._ask_yes_no(
            "カード削除",
            f"以下のカードをゴミ箱に移動しますか？\n\n{entry.rel_path}\n\n関連するサムネイルと sidecar もゴミ箱に移動されます。",
            default_button=QMessageBox.No,
        )
        if result != QMessageBox.Yes:
            return
        txt_path = Path(entry.abs_path)
        sidecar_path = sidecar_metadata_path(txt_path)
        thumb_path = Path(entry.thumbnail_path) if entry.thumbnail_path else None
        errors: list[str] = []
        for path in [txt_path, sidecar_path, thumb_path] if thumb_path is not None else [txt_path, sidecar_path]:
            if path is None or not path.exists():
                continue
            if not self._move_to_trash(path):
                errors.append(str(path))
        if errors:
            self._show_error("削除エラー", f"次のファイルをゴミ箱に移動できませんでした:\n\n{chr(10).join(errors)}")
            return
        try:
            self.repo.delete_entry(entry.abs_path)
            self.load_cached_entries()
            self.statusBar().showMessage("カードをゴミ箱に移動しました。", 6000)
        except Exception as exc:
            self._show_error("削除エラー", str(exc))

    def _move_to_trash(self, path: Path) -> bool:
        try:
            if not path.exists():
                return False
            if hasattr(QFile, "moveToTrash"):
                return QFile.moveToTrash(str(path))
            return False
        except Exception:
            return False

    def _delete_selected_cards(self) -> None:
        indexes = self.card_view.selectionModel().selectedIndexes()
        if not indexes:
            if self.current_entry:
                idx = self.filtered_entries.index(self.current_entry)
                indexes = [self.list_model.index(idx, 0)]
            else:
                return

        entries = [self.list_model.entry_at(idx) for idx in indexes]
        entries = [e for e in entries if e is not None]
        if not entries:
            return

        count = len(entries)
        names = "\n".join(f"  {e.rel_path}" for e in entries[:10])
        if count > 10:
            names += f"\n  ... 他 {count - 10} 件"
        result = self._ask_yes_no(
            "カード削除",
            f"以下の {count} 件をゴミ箱に移動しますか？\n\n{names}\n\n関連するサムネイルと sidecar もゴミ箱に移動されます。",
            default_button=QMessageBox.Yes,
        )
        if result != QMessageBox.Yes:
            return

        errors: list[str] = []
        for entry in entries:
            txt_path = Path(entry.abs_path)
            sidecar_path = sidecar_metadata_path(txt_path)
            thumb_path = Path(entry.thumbnail_path) if entry.thumbnail_path else None
            for path in [txt_path, sidecar_path, thumb_path] if thumb_path else [txt_path, sidecar_path]:
                if path is None or not path.exists():
                    continue
                if not self._move_to_trash(path):
                    errors.append(str(path))
            try:
                self.repo.delete_entry(entry.abs_path)
            except Exception as exc:
                errors.append(str(exc))

        if errors:
            self._show_error("削除エラー", f"次のファイルをゴミ箱に移動できませんでした:\n\n{chr(10).join(errors)}")
        else:
            self.statusBar().showMessage(f"{count} 件のカードをゴミ箱に移動しました。", 6000)

        self.load_cached_entries()

    def _move_entries_to_folder(self, entries: list[WildcardEntry], dest_folder: str) -> None:
        if not entries:
            return

        if entries[0].folder == dest_folder:
            return

        count = len(entries)
        folder_display = dest_folder or "(all)"
        result = self._ask_yes_no(
            "カード移動",
            f"以下の {count} 件を「{folder_display}」に移動しますか？\n\n"
            + "\n".join(f"  {e.rel_path}" for e in entries[:10])
            + (f"\n  ... 他 {count - 10} 件" if count > 10 else ""),
        )
        if result != QMessageBox.Yes:
            return

        errors: list[str] = []
        moved = 0
        for entry in entries:
            try:
                self.repo.move_entry(self.settings, entry, dest_folder)
                moved += 1
            except Exception as exc:
                errors.append(f"{entry.name}: {exc}")

        if errors:
            self._show_error("移動エラー", f"次のファイルの移動に失敗しました:\n\n{chr(10).join(errors)}")
        self.statusBar().showMessage(f"{moved} 件のカードを「{folder_display}」に移動しました。", 6000)
        self.load_cached_entries()

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

        selected_loras: set[str] = set()
        for i in range(self.lora_cloud.count()):
            item = self.lora_cloud.item(i)
            if item.checkState() == Qt.Checked:
                name = item.data(Qt.UserRole)
                if name:
                    selected_loras.add(name)

        def matches(entry: WildcardEntry) -> bool:
            if self.current_folder_prefix:
                if entry.folder != self.current_folder_prefix and not entry.folder.startswith(f"{self.current_folder_prefix}/"):
                    return False
            if selected_loras:
                if not selected_loras.issubset(set(entry.lora_names)):
                    return False
            if not query:
                return True
            return query in self.search_cache.get(entry.abs_path, "")

        filtered = self._sort_entries([entry for entry in self.entries if matches(entry)])
        self.filtered_entries = filtered
        self.list_model.set_entries(self.filtered_entries)
        self._set_result_count_label(len(self.filtered_entries))

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
            # H10 修正: 参照されたエントリを最新としてマーク
            self._content_load_cache.move_to_end(entry.abs_path)
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
        # M9 修正: _pending_content_abs_path は書き込まれるだけで読み出されない
        # デッド状態変数だったため削除。token だけで十分。
        self._content_load_pool.start(EntryContentLoadTask(abs_path, self.repo, self._content_load_signals), 0)

    @Slot(str, str)
    def _on_entry_content_loaded(self, abs_path: str, content: str) -> None:
        # H10 修正: LRU キャッシュ。上限超過時は最も古いエントリを破棄。
        self._content_load_cache[abs_path] = content
        self._content_load_cache.move_to_end(abs_path)
        while len(self._content_load_cache) > self._content_load_cache_max:
            self._content_load_cache.popitem(last=False)
        # 既存エントリを更新する際も LRU 順序を更新する
        cached = self._content_load_cache.get(abs_path)
        if cached is not None:
            self._content_load_cache.move_to_end(abs_path)
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
        if self.is_edit_mode:
            self.delete_tags_button.show()
        else:
            self.delete_tags_button.hide()
            self.delete_tags_button.setEnabled(False)

    def _delete_selected_tags(self) -> None:
        if self.prompt_tags_edit.has_selected_tags():
            self.prompt_tags_edit.delete_selected_tags()
            self.delete_tags_button.setEnabled(False)

    def _update_delete_button_state(self) -> None:
        if self.is_edit_mode:
            self.delete_tags_button.setEnabled(self.prompt_tags_edit.has_selected_tags())

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
        if not self.card_view.selectionModel().isSelected(index):
            self.card_view.setCurrentIndex(index)
        entry = self.list_model.entry_at(index)
        if not entry:
            return

        menu = QMenu(self)
        thumb_action = menu.addAction("サムネ生成")
        random_action = menu.addAction("固定プロンプトに入れる")
        menu.addSeparator()
        copy_all_action = menu.addAction("プロンプトコピー（LORAあり）")
        copy_all_without_action = menu.addAction("プロンプトコピー（LORAなし）")
        if entry.lora_names:
            search_menu = menu.addMenu("LORAで絞り込み")
            for name in entry.lora_names:
                action = search_menu.addAction(name)
                action.triggered.connect(lambda checked=False, n=name: self._check_lora_in_cloud(n))
        menu.addSeparator()
        folder_action = menu.addAction("フォルダを開く")
        explorer_action = menu.addAction("エクスプローラーで開く")
        thumb_folder_action = menu.addAction("サムネフォルダを開く")
        menu.addSeparator()
        move_action = menu.addAction("移動...")
        menu.addSeparator()
        delete_action = menu.addAction("削除")
        action = menu.exec(self.card_view.viewport().mapToGlobal(pos))
        if action == thumb_action:
            self.generate_thumbnail_for_current()
        elif action == random_action:
            self.open_random_generation(entry)
        elif action == copy_all_action:
            self.copy_all_text(True)
        elif action == copy_all_without_action:
            self.copy_all_text(False)
        elif action == folder_action:
            self._navigate_to_entry_folder(entry)
        elif action == explorer_action:
            self._open_in_explorer(entry)
        elif action == thumb_folder_action:
            self._open_thumbnail_folder(entry)
        elif action == move_action:
            self._show_move_folder_dialog()
        elif action == delete_action:
            self._delete_card(entry)

    def _show_move_folder_dialog(self) -> None:
        indexes = self.card_view.selectionModel().selectedIndexes()
        if not indexes:
            if self.current_entry:
                idx = self.filtered_entries.index(self.current_entry)
                indexes = [self.list_model.index(idx, 0)]
            else:
                return

        entries = [self.list_model.entry_at(idx) for idx in indexes]
        entries = [e for e in entries if e is not None]
        if not entries:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("移動先のフォルダを選択")
        dialog.setMinimumSize(350, 450)

        layout = QVBoxLayout(dialog)

        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setRootIsDecorated(True)

        root = QTreeWidgetItem(["(all)"])
        root.setData(0, Qt.UserRole, "")
        tree.addTopLevelItem(root)

        self._populate_folder_tree_items(root, "")
        tree.expandAll()

        layout.addWidget(tree)

        button_layout = QHBoxLayout()
        cancel_btn = QPushButton("キャンセル")
        move_btn = QPushButton("移動")
        move_btn.setDefault(True)
        button_layout.addStretch()
        button_layout.addWidget(cancel_btn)
        button_layout.addWidget(move_btn)
        layout.addLayout(button_layout)

        cancel_btn.clicked.connect(dialog.reject)
        move_btn.clicked.connect(dialog.accept)

        tree.setCurrentItem(root)

        if dialog.exec() != QDialog.Accepted:
            return

        selected = tree.currentItem()
        if not selected:
            return
        dest_folder = selected.data(0, Qt.UserRole) or ""

        self._move_entries_to_folder(entries, dest_folder)

    def _populate_folder_tree_items(self, item: QTreeWidgetItem, parent_key: str) -> None:
        children = self.folder_children_map.get(parent_key, set())
        for child_name in sorted(children, key=natural_sort_key):
            child_key = child_name if not parent_key else f"{parent_key}/{child_name}"
            child_item = QTreeWidgetItem([child_name])
            child_item.setData(0, Qt.UserRole, child_key)
            item.addChild(child_item)
            if self.folder_children_map.get(child_key):
                self._populate_folder_tree_items(child_item, child_key)

    def _show_lora_context_menu(self, pos) -> None:
        item = self.lora_list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        filter_action = menu.addAction("この LoRA で検索")
        action = menu.exec(self.lora_list.viewport().mapToGlobal(pos))
        if action == filter_action:
            self._check_lora_in_cloud(item.text())

    def _navigate_to_entry_folder(self, entry: WildcardEntry) -> None:
        folder = entry.folder
        if not folder:
            self.folder_tree.setCurrentItem(self.folder_tree.topLevelItem(0))
            return
        self._ensure_folder_path(folder)
        item = self._find_folder_item(folder)
        if item:
            self.folder_tree.setCurrentItem(item)
            self.folder_tree.scrollToItem(item)

    def _open_in_explorer(self, entry: WildcardEntry) -> None:
        from .ui_utils import open_in_file_manager
        path = Path(entry.abs_path)
        if path.exists():
            open_in_file_manager(path, select=True)
        else:
            self._show_info("エクスプローラー", f"ファイルが見つかりません:\n{path}")

    def _open_thumbnail_folder(self, entry: WildcardEntry) -> None:
        from .ui_utils import open_in_file_manager
        if not entry.thumbnail_path:
            self._show_info("サムネフォルダ", "サムネイルがありません。")
            return
        thumb_path = Path(entry.thumbnail_path)
        if thumb_path.exists():
            open_in_file_manager(thumb_path, select=True)
        else:
            parent = thumb_path.parent
            if parent.exists():
                open_in_file_manager(parent)
            else:
                self._show_info("サムネフォルダ", f"サムネイルフォルダが見つかりません:\n{parent}")

    def _sync_editor_from_prompt_tags(self) -> None:
        text = ", ".join(self.prompt_tags_edit.tags())
        if self.editor.toPlainText() == text:
            return
        with QSignalBlocker(self.editor):
            self.editor.setPlainText(text)

    def _sync_tags_from_editor(self) -> None:
        if not self.is_edit_mode:
            return
        text = self.editor.toPlainText().strip()
        tags = [t.strip() for t in text.split(",") if t.strip()]
        current = self.prompt_tags_edit.tags()
        if tags != current:
            with QSignalBlocker(self.prompt_tags_edit):
                self.prompt_tags_edit.set_tags(tags)

    def save_current_entry(self) -> bool:
        if not self.current_entry:
            self._show_info("保存", "先に wildcard を選択してください。")
            return False
        try:
            content = self.editor.toPlainText().strip()
            updated = self.repo.save_entry(self.settings, self.current_entry, content, [])
            self._content_load_cache.pop(updated.abs_path, None)
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
        row = self.list_model.row_by_path.get(updated.abs_path)
        if row is not None and row < len(self.list_model.entries):
            self.list_model.entries[row] = updated
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

        progress = self._create_progress_dialog(title, f"{title} 取り込み中...", can_cancel=True)
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

    def _ask_use_random_wildcard(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("ランダムワイルドカード")
        box.setText("ランダムワイルドカードを使用しますか？")
        btn_yes = box.addButton("使用する", QMessageBox.AcceptRole)
        btn_no = box.addButton("使用しない", QMessageBox.RejectRole)
        box.setDefaultButton(btn_no)
        box.exec()
        return box.clickedButton() is btn_yes

    def generate_thumbnail_for_current(self) -> None:
        if not self.current_entry:
            self._show_info("サムネ生成", "先に wildcard を選択してください。")
            return
        if self.thumbnail_generation_thread is not None and self.thumbnail_generation_thread.isRunning():
            self._show_info("サムネ生成", "別のサムネ生成が実行中です。")
            return
        use_random = self._ask_use_random_wildcard()
        worker_settings = copy.copy(self.settings)
        worker_settings.thumbnail_random_wildcard = use_random
        self._start_thumbnail_loading_feedback("Generating thumbnail...\nPreparing image...")
        self.statusBar().showMessage("Generating thumbnail...", 0)
        self.thumbnail_generation_thread = QThread(self)
        self.thumbnail_generation_worker = SingleThumbnailWorker(self.db_path, worker_settings, self.current_entry)
        self.thumbnail_generation_worker.moveToThread(self.thumbnail_generation_thread)
        self.thumbnail_generation_thread.started.connect(self.thumbnail_generation_worker.run)
        self.thumbnail_generation_worker.finished.connect(self._on_thumbnail_generation_finished)
        self.thumbnail_generation_worker.failed.connect(self._on_thumbnail_generation_failed)
        self.thumbnail_generation_thread.start()

    def generate_missing_thumbnails(self) -> None:
        """不足分サムネイルをバックグラウンドワーカーで生成する（H1 修正）。

        以前は UI スレッド上で QApplication.processEvents() を呼びながら
        同期ループを回していたが、QThread + MissingThumbnailBatchWorker に
        置き換えてフリーズとリエントラントバグを解消した。
        """
        if self._missing_thumb_thread is not None and self._missing_thumb_thread.isRunning():
            self._show_info("不足分サムネ生成", "別のサムネ生成が実行中です。")
            return
        targets = [entry for entry in self.filtered_entries if not entry.has_thumbnail]
        if not targets:
            self._show_info("不足分サムネ生成", "不足しているサムネイルはありません。")
            return
        if self._ask_yes_no("不足分サムネ生成", f"{len(targets)} 件のサムネイルを生成しますか？") != QMessageBox.Yes:
            return
        use_random = self._ask_use_random_wildcard()

        self._missing_thumb_progress = self._create_progress_dialog(
            "不足分サムネ生成", "サムネイルを生成中...", maximum=len(targets), can_cancel=True
        )
        self._missing_thumb_progress.canceled.connect(self._cancel_missing_thumbnail_batch)
        self.statusBar().showMessage("サムネイルを生成中...", 0)
        self._missing_thumb_done = 0
        self._missing_thumb_total = len(targets)

        self._missing_thumb_thread = QThread(self)
        self._missing_thumb_worker = MissingThumbnailBatchWorker(
            self.db_path, self.settings, self.api, targets, use_random
        )
        self._missing_thumb_worker.moveToThread(self._missing_thumb_thread)
        self._missing_thumb_thread.started.connect(self._missing_thumb_worker.run)
        self._missing_thumb_worker.progress.connect(self._on_missing_thumb_progress)
        self._missing_thumb_worker.item_finished.connect(self._on_missing_thumb_item_finished)
        self._missing_thumb_worker.item_failed.connect(self._on_missing_thumb_item_failed)
        self._missing_thumb_worker.finished.connect(self._on_missing_thumb_finished)
        self._missing_thumb_worker.cancelled.connect(self._on_missing_thumb_cancelled)
        # ワーカー終了時に必ずスレッド後始末を行う
        for sig in (self._missing_thumb_worker.finished, self._missing_thumb_worker.cancelled):
            sig.connect(self._cleanup_missing_thumbnail_worker)
        self._missing_thumb_thread.start()

    def _cancel_missing_thumbnail_batch(self) -> None:
        if self._missing_thumb_worker is not None:
            self._missing_thumb_worker.cancel()

    def _on_missing_thumb_progress(self, current: int, total: int, name: str) -> None:
        dlg = getattr(self, "_missing_thumb_progress", None)
        if dlg is None:
            return
        dlg.setMaximum(total)
        dlg.setValue(current)
        dlg.setLabelText(f"[{(current % 4) * '=':<3}] 生成中 {current}/{total} {elide(name, 64)}")

    def _on_missing_thumb_item_finished(self, updated: WildcardEntry) -> None:
        for pos, existing in enumerate(self.entries):
            if existing.abs_path == updated.abs_path:
                self.entries[pos] = updated
                break
        self._missing_thumb_done += 1

    def _on_missing_thumb_item_failed(self, abs_path: str, message: str) -> None:
        # 個別失敗はステータスバーに記録するだけ（バッチは継続）
        self.statusBar().showMessage(f"失敗: {Path(abs_path).name} - {message}", 3000)

    def _on_missing_thumb_finished(self, done: int, total: int) -> None:
        self._rebuild_entry_indexes()
        self.apply_filters()
        self.statusBar().showMessage(f"{done} 件のサムネイルを生成しました", 6000)

    def _on_missing_thumb_cancelled(self) -> None:
        self._rebuild_entry_indexes()
        self.apply_filters()
        self.statusBar().showMessage(
            f"サムネイル生成をキャンセルしました。完了 {self._missing_thumb_done} 件", 5000
        )

    def _cleanup_missing_thumbnail_worker(self, *args) -> None:
        dlg = getattr(self, "_missing_thumb_progress", None)
        if dlg is not None:
            dlg.close()
            self._missing_thumb_progress = None
        if self._missing_thumb_thread is not None:
            self._missing_thumb_thread.quit()
            self._missing_thumb_thread.wait(2000)
            self._missing_thumb_thread.deleteLater()
        if self._missing_thumb_worker is not None:
            self._missing_thumb_worker.deleteLater()
        self._missing_thumb_thread = None
        self._missing_thumb_worker = None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_top_controls_layout()
        self._refresh_path_label()
        if self.current_entry is not None:
            self._update_preview_pixmap()

    def _cleanup_wildcard_preview_thumbnails(self) -> None:
        """新規ワイルドカードダイアログの「サムネイル生成」で作られた一時プレビュー画像を削除する。

        これらはダイアログで衣装ごとに生成された時点ではまだ実体のワイルドカードファイルが
        存在しないプレビュー扱いのファイルで（thumbnail_root/_preview_tmp/ 配下）、
        ワイルドカード作成時に本来の保存先へコピーされた後は不要になる。
        作成せずダイアログを閉じた分も含めて、アプリ終了時にまとめて削除する。
        """
        if not self.settings.thumbnail_root:
            return
        try:
            preview_dir = Path(self.settings.thumbnail_root) / PREVIEW_THUMB_SUBDIR
            if preview_dir.exists():
                import shutil
                shutil.rmtree(preview_dir, ignore_errors=True)
        except Exception:
            pass

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._confirm_discard_or_save_if_needed():
            event.ignore()
            return
        # Let outstanding thumbnail/content jobs finish cleanly so they do not
        # emit into deleted QObject signal sources during shutdown.
        self._api_monitor.stop()
        self._api_monitor.wait(2000)
        QThreadPool.globalInstance().waitForDone(2000)
        if self.cached_load_thread is not None and self.cached_load_thread.isRunning():
            self.cached_load_thread.quit()
            self.cached_load_thread.wait(2000)
        self._stop_thumbnail_loading_feedback()
        self._cleanup_thumbnail_generation_worker()
        if self.rescan_thread is not None and self.rescan_thread.isRunning():
            self.rescan_thread.quit()
            self.rescan_thread.wait(2000)
        if self.unmade_scan_thread is not None and self.unmade_scan_thread.isRunning():
            self.unmade_scan_thread.quit()
            self.unmade_scan_thread.wait(2000)
        self._cleanup_wildcard_preview_thumbnails()
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
        self.ui_state_store.save(self.settings)
        super().closeEvent(event)

