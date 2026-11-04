from __future__ import annotations

import base64
import copy
import io
import json
import logging
import os
import re
import struct
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import random
import uuid

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from PySide6.QtCore import QFile, QItemSelection, QItemSelectionModel, QModelIndex, QObject, QPoint, QRect, QRectF, QSignalBlocker, QSize, Qt, QThread, QThreadPool, QTimer, Signal, Slot, QEvent, QFileSystemWatcher
from PySide6.QtGui import QAction, QActionGroup, QClipboard, QCloseEvent, QColor, QFont, QFontMetrics, QIcon, QImage, QImageReader, QKeySequence, QPainter, QPainterPath, QPixmap
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
    QHeaderView,
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
    QSlider,
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
from .icons import icon_folder, icon_home, icon_lora, icon_new_card, icon_panel, icon_random, icon_settings, icon_star, icon_text_size
from . import theme
from .models import AppSettings, WildcardEntry
from .repository import CONFLICT_POLICIES, OperationCancelledError, WildcardRepository, ensure_thumbnail_destination, next_gallery_slot, sidecar_metadata_path, strip_lora_tags, parse_prompt_tags, extract_lora_names, _safe_resolve_path
from .thumbnail_model import ThumbnailListModel, ThumbnailItemDelegate, ThumbnailCardDelegate, CARD_TITLE_HEIGHT, CARD_FRAME_PADDING, lora_name_key, thumbnail_paths_equal
from .thumbnail_workers import ThumbnailLoadSignals, ThumbnailLoadTask, EntryContentLoadSignals, EntryContentLoadTask, PreviewLoadSignals, PreviewLoadTask, cover_crop
from .tag_editor import FlowLayout, TagChip, TagEditorWidget
from .new_wildcard_dialog import NewWildcardDialog, PREVIEW_THUMB_SUBDIR
from .single_thumbnail_worker import SingleThumbnailWorker
from .workers import (
    RandomGenerationRequest,
    RandomGenerationWorker,
    CachedEntriesLoader,
    RescanWorker,
    UnmadeWildcardScanWorker,
    ApiMonitorWorker,
    MissingThumbnailBatchWorker,
    collect_txt_files,
    extract_prompt_line,
)
from .widgets import (
    NoWheelComboBox,
    ApiStatusIndicator,
    ResponsivePixmapLabel,
    SlowWheelListView,
    ToastPopup,
)
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

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
# 寸法は theme 由来に一本化（仕様: 8pxグリッド / ボタン34〜38 / アイコンボタン32〜36四方 / 入力34〜38）
# スタイルシートの min/max-height は content rect 指定で border(1px*2) が加算される。
# よって setFixedSize 系の外側寸法から2を引いた値が content 用になる。
UI_OUTER_MARGIN = theme.SPACE + theme.SPACE_SM          # 12
UI_SECTION_SPACING = theme.SPACE + theme.SPACE_SM       # 12
UI_CARD_MARGIN_TOP = theme.SPACE + theme.SPACE_SM       # 12
UI_CARD_MARGIN_X = theme.SPACE + theme.SPACE_SM         # 12
UI_CARD_PADDING_X = theme.SPACE + theme.SPACE_SM        # 12
UI_CARD_PADDING_Y = theme.SPACE + theme.SPACE_SM        # 12
UI_RADIUS = theme.RADIUS_CARD                           # 8
UI_CONTROL_HEIGHT = theme.INPUT_HEIGHT - 2              # 34 → 外側36
UI_BUTTON_HEIGHT = theme.BUTTON_HEIGHT - 2              # 34 → 外側36
UI_BUTTON_PADDING_X = theme.BUTTON_PADDING_X            # 12
UI_BUTTON_PADDING_Y = 2
TOOLBAR_BUTTON_SIZE = theme.ICON_BUTTON_SIZE            # 36（= BUTTON_HEIGHT）
TOOLBAR_ICON_SIZE = QSize(theme.ICON_SIZE, theme.ICON_SIZE)
# セグメンテッドピルの内側ボタン: グループ枠(1*2) + layout余白(2*2) を引いた高さ
PILL_SEGMENT_HEIGHT = TOOLBAR_BUTTON_SIZE - 6           # 30
TOOLBAR_HEIGHT = theme.TOOLBAR_HEIGHT                   # 44
# BaseModelフィルタの「LoRAなし」哨兵値。実在モデル名と衝突しないよう __ で囲む
NOLORA_FILTER = "__nolora__"
# 「サムネイルに画像を指定」で取り込む画像の長辺上限。生成APIの出力（〜1216px）
# より大きくても表示は最大720pxなので、それより上に作ってもファイルだけ膨らむ。
THUMBNAIL_ASSIGN_MAX = 1024

# safetensorsヘッダーからBaseModel判定に必要なキーだけを抜き出すパターン。
# ヘッダー全体を json.loads すると（kohyaが ss_tag_frequency 等を膨大に書き込むため）
# 3051ファイルで実測9.4s、該当値の正規表現抽出なら実測0.7s。
LORA_HEADER_PATTERNS: dict[str, re.Pattern[bytes]] = {
    "new_hash": re.compile(rb'"ss_new_sd_model_hash"\s*:\s*"([^"]*)"'),
    "old_hash": re.compile(rb'"ss_sd_model_hash"\s*:\s*"([^"]*)"'),
    "sd_model_name": re.compile(rb'"ss_sd_model_name"\s*:\s*"([^"]*)"'),
    "architecture": re.compile(rb'"modelspec\.architecture"\s*:\s*"([^"]*)"'),
    "base_version": re.compile(rb'"ss_base_model_version"\s*:\s*"([^"]*)"'),
}
LORA_HEADER_MAX_BYTES = 10 * 1024 * 1024
# 学習時ベースのハッシュ→BaseModel投票がこの割合以上で一つに固まる場合だけ採用。
# 実データ例: Illustrious 1668 / Anima 1 → 採用、Illustrious 4 / NoobAI 3 → 却下。
LORA_HASH_REGISTRY_MIN_RATIO = 0.9


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


class FolderTreeWidget(QTreeWidget):
    """フォルダツリー。ドラッグ&ドロップによるフォルダ移動要求を送出する。"""

    folder_move_requested = Signal(str, str)  # (src_key, dest_parent_key)

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt API)
        source = self.currentItem()
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        target = self.itemAt(pos)
        if source is None or target is None or source is target:
            event.ignore()
            return
        src_key = str(source.data(0, Qt.UserRole) or "")
        dest_key = str(target.data(0, Qt.UserRole) or "")
        if not src_key or src_key == "__favorites__" or src_key.startswith("__favorites__/"):
            event.ignore()
            return
        if dest_key == "__favorites__" or dest_key.startswith("__favorites__/"):
            event.ignore()
            return
        if dest_key == src_key or dest_key.startswith(src_key + "/"):
            event.ignore()
            return
        self.folder_move_requested.emit(src_key, dest_key)
        event.acceptProposedAction()


class TreeCountDelegate(QStyledItemDelegate):
    """ツリー2列目の件数。右寄せ・一段小さく淡い色で、行の主役を名前に置く（仕様②）。

    QSS の ::item color は ForegroundRole より優先されるため、色は描画側で確定させる。
    背景（選択/ホバー）はスタイルに描かせて他列と揃える。
    """

    def paint(self, painter, option, index):  # noqa: D102
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        bare = QStyleOptionViewItem(opt)
        bare.text = ""
        opt.widget.style().drawControl(QStyle.CE_ItemViewItem, bare, painter, opt.widget)
        painter.save()
        font = QFont(opt.font)
        font.setPointSizeF((theme.SMALL_SIZE + 1) * 0.75)
        painter.setFont(font)
        selected = bool(opt.state & QStyle.State_Selected)
        painter.setPen(QColor(theme.TEXT if selected else theme.TEXT_MUTED))
        rect = opt.rect.adjusted(0, 0, -theme.SPACE_SM, 0)
        painter.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, text)
        painter.restore()


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
        # 外観は app.setStyleSheet(theme.app_style()) の基底に任せる。
        # ここで QDialog/QPushButton の一括指定を残すと、基底の部品スタイル
        # （Secondaryボタン・入力欄・グループボックス）が全部上書きされて消える。
        self.setStyleSheet(f"""
            QGroupBox {{
                padding: 6px 0px 10px 0px;
            }}
            QGroupBox::title {{
                left: {UI_CARD_MARGIN_X}px;
                padding: 0 {theme.SPACE_SM}px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(UI_OUTER_MARGIN, UI_OUTER_MARGIN, UI_OUTER_MARGIN, UI_OUTER_MARGIN)
        layout.setSpacing(UI_SECTION_SPACING)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(theme.scroll_area())
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
        overview.setStyleSheet(f"QLabel {{ {theme.note()} background: transparent; padding: 2px 4px; }}")
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
        self.random_refresh_checkpoints_button.setFixedHeight(theme.BUTTON_HEIGHT)
        self.random_checkpoint_status = QLabel("checkpoint 候補を取得できます")
        self.random_checkpoint_status.setWordWrap(True)
        self.random_checkpoint_status.setMinimumHeight(20)
        self.random_checkpoint_status.setStyleSheet(f"color: {theme.WARNING};")
        self.random_adetailer_enabled = QCheckBox("ADetailer を使う")
        self.random_adetailer_enabled.setChecked(settings.random_adetailer_enabled)

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
        self.generate_button.setFixedHeight(theme.BUTTON_HEIGHT)
        self.close_button = QPushButton("閉じる")
        self.close_button.clicked.connect(self.reject)
        self.close_button.setFixedHeight(theme.BUTTON_HEIGHT)
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
            left_split_sizes=base_settings.left_split_sizes,
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
            self.random_checkpoint_status.setStyleSheet(f"color: {theme.DANGER_TEXT};")
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
                self.random_checkpoint_status.setStyleSheet(f"color: {theme.OK};")
                self.random_checkpoint_status.setText(f"{len(checkpoints)} 件の checkpoint 候補を取得しました")
            else:
                self.random_checkpoint_status.setStyleSheet(f"color: {theme.WARNING};")
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
        button.setFixedHeight(theme.BUTTON_HEIGHT)
        button.clicked.connect(lambda: self._pick_directory(line_edit))
        layout.addWidget(line_edit)
        layout.addWidget(button)
        return {"widget": row, "line_edit": line_edit}

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        label.setMinimumWidth(92)
        label.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
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

    # 入力部品の配色・角丸・境界は theme.app_style() の基底に一本化した。
    # ここでは高さの指定だけを持つ（旧: #1a1d22/#2d3640 の重複定義を削除）。
    def _style_compact_line_field(self, widget: QLineEdit) -> None:
        widget.setFixedHeight(theme.INPUT_HEIGHT - 4)

    def _style_compact_combo_field(self, widget: QComboBox) -> None:
        widget.setFixedHeight(theme.INPUT_HEIGHT - 4)


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
        help_label.setStyleSheet(theme.note())
        layout.addWidget(help_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def conflict_policy(self) -> str:
        return str(self.policy_combo.currentData())


class MainWindow(QMainWindow):
    _api_lamp_signal = Signal(str)
    # ループ外スレッドのベースモデルスキャン完了をメインスレッドに返す。
    # スキャン本体はディスクI/Oだけを行い、Qtオブジェクトには触れない。
    lora_base_scan_done = Signal(object)

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
        self.fav_folder_children_map: dict[str, set[str]] = {}
        self._folder_tree_populating = False
        self._folder_counts: dict[str, int] = {}
        # お気に入り機能: お気に入りエントリの abs_path を保持するセット
        self.favorites_file = app_dir / "favorites.json"
        self.favorite_paths: set[str] = self._load_favorites()
        # LoRA名(lower) → BaseModel のマップ（cm-info.json由来）。
        # ill/aniバッジ描画とモデルフィルタの判定に使う。
        self._lora_base_model_map: dict[str, str] = {}
        # ワイルドカードから参照されているのにディスクに痕跡が無いLoRA名（lower）。
        # この名前を1つでも参照するカードは LOST 表示（減光＋ピル）になる。
        self._lost_lora_names: set[str] = set()
        # ベースモデルスキャンの多重起動ガード（ワーカースレッド実行中フラグ）
        self._lora_base_scan_running = False
        self.lora_base_scan_done.connect(self._apply_lora_base_model_result)
        self._startup_load_requested = False
        # rescan_library の完了コールバックで参照されるため、
        # 初期化前に _on_rescan_finished が発火しても AttributeError に
        # ならないよう __init__ で初期化しておく（B4 修正）。
        self._rescan_initial = False
        # generate_missing_thumbnails 用のバッチワーカー（H1 修正）
        self._missing_thumb_thread: QThread | None = None
        self._missing_thumb_worker: "MissingThumbnailBatchWorker | None" = None
        # 複数カード選択時のサムネイル生成キュー
        # sd-webui API は単一画像生成しかサポートしないため、アプリ側で
        # キューイングして順次生成する。
        self._thumbnail_batch_queue: list[WildcardEntry] = []
        self._thumbnail_batch_total: int = 0
        self._thumbnail_batch_use_random: bool = False
        self._thumbnail_batch_cancelled: bool = False

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
        self.list_model.favorite_paths = self.favorite_paths  # お気に入り参照を共有
        self.list_model.lora_base_model_map = self._lora_base_model_map  # ill/aniバッジ参照を共有
        self.list_model.lost_lora_names = self._lost_lora_names  # LOST表示参照を共有
        self._sync_gallery_source()
        self.item_delegate = ThumbnailCardDelegate(self)

        self.setWindowTitle("Wildcard Manager")
        self.setMinimumSize(1280, 760)
        self.resize(max(self.settings.window_width, 1280), max(self.settings.window_height, 760))
        self._build_ui()
        self._create_actions()

    def _build_ui(self) -> None:
        # 色・入力欄・ボタン・メニュー・スクロールバーの基底は app.setStyleSheet(theme.app_style())。
        # ここで基底を再宣言するとウィンドウのシートが app のシートより優先されて
        # 部品 styles が消えるので、差分だけ持つ。
        self.setStyleSheet(
            f"""
            QMainWindow {{ background: {theme.APP_BG}; }}
            QWidget#topControls {{
                background: {theme.PANEL_SIDE};
                border: 1px solid {theme.BORDER};
                border-radius: {theme.RADIUS_PANEL}px;
            }}
            /* カードは delegate が自前描画する。Qt標準の選択背景/ホバーを重ねると二重に光る */
            QListView#cardView {{
                background: {theme.APP_BG};
                border: none;
                outline: 0;
                selection-background-color: transparent;
                selection-color: {theme.TEXT};
            }}
            QListView#cardView::item:selected,
            QListView#cardView::item:selected:active,
            QListView#cardView::item:hover {{
                background: transparent;
                color: {theme.TEXT};
            }}
            /* ツリーとリストは標準描画。選択行はアクセントの深い色（仕様②）
               Qt は別のシートに ::item ルールがあると非該当状態を palette Base（白）で塗る
               ため、透過でも明示しておくのが安全（実測: 指定しないと行全体が白くなった）。 */
            QListView::item, QListWidget::item, QTreeView::item, QTreeWidget::item {{
                background: transparent;
                color: {theme.TEXT};
                border-radius: {theme.RADIUS_CHIP}px;
            }}
            QTreeView::item:selected, QTreeView::item:selected:active,
            QTreeView::item:selected:!active,
            QListWidget::item:selected, QListWidget::item:selected:active {{
                background: {theme.SELECTED};
                color: {theme.TEXT};
            }}
            /* ツリーの選択行は角丸ピルにしない。ピルだとインデント帯（選択背景が
               塗られる）と枝葉帯（transparent）が縞模様になって「||」マークに見える
               （ユーザー指摘）。行全体を一枚の帯にして、マークではなく選択と
               読めるようにする。 */
            QTreeView#folderTree::item:selected,
            QTreeView#folderTree::item:selected:active,
            QTreeView#folderTree::item:selected:!active {{
                border-radius: 0;
            }}
            QTreeView::branch {{ background: transparent; }}
            QTreeView::branch:selected, QTreeView::branch:selected:active,
            QTreeView::branch:selected:!active {{ background: {theme.SELECTED}; }}
            /* ホバーも行全体で塗る。::item:hover を書くと Qt は枝葉セル
               （branch）だけ既定の薄い四角を塗り、行本体は無変化になる。
               インデント帯に浮く小さな灰色箱が「変なハイライト」として
               見えていた（ユーザー指摘）ので、branch 側と同じ色で揃える。 */
            QTreeView#folderTree::item:hover {{
                background: {theme.HOVER_BG};
                border-radius: 0;
            }}
            QTreeView::branch:hover {{ background: {theme.HOVER_BG}; }}
            /* 選択行の上でホバーしても選択色を維持する（薄めて選別不能にしない） */
            QTreeView#folderTree::item:selected:hover,
            QTreeView#folderTree::item:selected:hover:active {{
                background: {theme.SELECTED};
            }}
            /* 行の高さだけ名指しで指定（カード一覧の QListView::item に触れないよう
               objectName で絞る。高さ未指定のプロパティは上の ::item ルールが生き続ける） */
            QTreeView#folderTree::item, QListWidget#loraCloud::item {{
                height: {theme.LIST_ROW_HEIGHT}px;
            }}
            QHeaderView::section {{
                background: transparent;
                color: {theme.TEXT_MUTED};
                font-size: {theme.HEADING_SIZE}px;
                font-weight: 600;
                border: none;
                padding: {theme.SPACE_SM}px {theme.SPACE}px;
            }}
            """
        )

        root = QWidget()
        root_layout = QVBoxLayout(root)
        # メニューバーとツールバーの間の隙間だけ詰める（左右下は据え置き）
        root_layout.setContentsMargins(UI_OUTER_MARGIN, 4, UI_OUTER_MARGIN, UI_OUTER_MARGIN)
        root_layout.setSpacing(UI_SECTION_SPACING)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("検索: ファイル名 / 本文 / カスタムタグ / lora:名前")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(200)
        self.search_edit.setMaximumWidth(640)
        self.search_edit.setFixedHeight(TOOLBAR_BUTTON_SIZE)
        # ツールバー行の高さに揃える（基底の入力欄は36pxなので here で差し替える）
        self.search_edit.setStyleSheet(theme.input_field(content_height=UI_BUTTON_HEIGHT))
        self.search_edit.textChanged.connect(self.schedule_apply_filters)

        self.thumbnail_size_slider = QSlider(Qt.Horizontal)
        self.thumbnail_size_slider.setRange(THUMBNAIL_SIZE_MIN, THUMBNAIL_SIZE_MAX)
        self.thumbnail_size_slider.setSingleStep(THUMBNAIL_SIZE_STEP)
        self.thumbnail_size_slider.setPageStep(THUMBNAIL_SIZE_STEP * 4)
        self.thumbnail_size_slider.setTickPosition(QSlider.NoTicks)
        self.thumbnail_size_slider.setValue(self.settings.thumbnail_size)
        self.thumbnail_size_slider.setFixedHeight(theme.CHIP_HEIGHT)
        self.thumbnail_size_slider.setMinimumWidth(140)
        self.thumbnail_size_slider.setMaximumWidth(220)
        self.thumbnail_size_slider.setToolTip("サムネイルサイズ　（Ctrl+- / Ctrl+= でも調整可）")
        self.thumbnail_size_slider.setStyleSheet(theme.slider())
        # ドラッグ中は毎ピクセル再レイアウトになると重いので、止まってから適用する
        self._size_apply_timer = QTimer(self)
        self._size_apply_timer.setSingleShot(True)
        self._size_apply_timer.setInterval(90)
        self._size_apply_timer.timeout.connect(
            lambda: self._set_thumbnail_size(self.thumbnail_size_slider.value()))
        self.thumbnail_size_slider.valueChanged.connect(self._on_thumbnail_size_slid)
        self.thumbnail_size_display = QLabel()
        self.thumbnail_size_display.setAlignment(Qt.AlignCenter)
        self.thumbnail_size_display.setFixedSize(56, theme.CHIP_HEIGHT)
        self.thumbnail_size_display.setStyleSheet(f"QLabel {{ {theme.chip()} }}")
        self._update_thumbnail_size_display(self.settings.thumbnail_size)

        self.size_group = QWidget()
        self.size_group.setFixedHeight(theme.CHIP_HEIGHT + 4)
        size_layout = QHBoxLayout(self.size_group)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.setSpacing(theme.GAP_PAIR)
        # ステータスバーがこの群を引き伸ばすと、バーと数値表示の間に隙間が
        # 生まれて「別々の部品」に見える。先頭ストレッチで群を右に固める。
        size_layout.addStretch(1)
        size_layout.addWidget(self.thumbnail_size_slider)
        size_layout.addWidget(self.thumbnail_size_display)

        # ツールバーのボタン階層（仕様①）: Primary=新規作成 / Secondary=ランダム /
        # Ghost=ペイン開閉・設定。行の高さ 34 に揃えるため content_height を明示する。
        primary_button_style = theme.button("primary", content_height=UI_BUTTON_HEIGHT)
        secondary_button_style = theme.button("secondary", content_height=UI_BUTTON_HEIGHT)
        ghost_button_style = theme.icon_button(content_height=UI_BUTTON_HEIGHT)

        # 2つのペイン開閉を1つの塊にまとめ、開いている側だけをハイライトする
        # （checkable で状態を表す）。接合部は1つの塊として読めるよう仕切り線だけ入れる。
        self.pane_tab_group = QWidget()
        self.pane_tab_group.setObjectName("paneTabGroup")
        # 高さはグループが持つ。セグメントはレイアウトが内法まで伸ばす
        # （ここで固定すると塗りだけが浮いてピルに収まらない）。
        self.pane_tab_group.setFixedHeight(TOOLBAR_BUTTON_SIZE)
        pane_tab_layout = QHBoxLayout(self.pane_tab_group)
        pane_tab_layout.setContentsMargins(2, 2, 2, 2)
        pane_tab_layout.setSpacing(0)
        pane_tab_style = f"""
            QWidget#paneTabGroup {{
                background: {theme.PANEL_MAIN};
                border: 1px solid {theme.BORDER};
                border-radius: {theme.RADIUS_CONTROL}px;
            }}
            QPushButton {{
                background: transparent;
                border: none;
                color: {theme.TEXT_MUTED};
                font-size: {theme.FONT_SIZE}px;
                padding: 0px {theme.BUTTON_PADDING_X}px;
                min-height: {PILL_SEGMENT_HEIGHT}px;
                max-height: {PILL_SEGMENT_HEIGHT}px;
            }}
            QPushButton#folderTab {{
                border-top-left-radius: {theme.RADIUS_CHIP}px;
                border-bottom-left-radius: {theme.RADIUS_CHIP}px;
            }}
            QPushButton#loraTab {{
                border-left: 1px solid {theme.BORDER};
                border-top-right-radius: {theme.RADIUS_CHIP}px;
                border-bottom-right-radius: {theme.RADIUS_CHIP}px;
            }}
            QPushButton:hover {{ color: {theme.TEXT}; background: {theme.GHOST_HOVER}; }}
            QPushButton:checked {{
                background: {theme.SELECTED};
                color: {theme.TEXT};
            }}
        """

        self.folder_tree_button = QPushButton("フォルダ")
        self.folder_tree_button.setObjectName("folderTab")
        self.folder_tree_button.setIcon(icon_folder(text_gap=True))
        self.folder_tree_button.setIconSize(icon_text_size())
        self.folder_tree_button.setCheckable(True)
        self.folder_tree_button.setToolTip("フォルダ一覧を開く/閉じる")
        self.folder_tree_button.clicked.connect(self._switch_to_folder_tab)
        self.folder_tree_button.setStyleSheet(pane_tab_style)
        pane_tab_layout.addWidget(self.folder_tree_button)

        self.lora_button = QPushButton("LoRA")
        self.lora_button.setObjectName("loraTab")
        self.lora_button.setIcon(icon_lora(text_gap=True))
        self.lora_button.setIconSize(icon_text_size())
        self.lora_button.setCheckable(True)
        self.lora_button.setToolTip("LoRA一覧（使用回数）を開く/閉じる")
        self.lora_button.clicked.connect(self._switch_to_lora_tab)
        self.lora_button.setStyleSheet(pane_tab_style)
        pane_tab_layout.addWidget(self.lora_button)

        # パネル開閉は「向き」の矢印ではなく、帯の位置でどちらのパネルか示す
        # sidebarアイコン（VS Code / Figma系）。塗り=表示中、点線枠=隠れている。
        self.left_toggle_button = QPushButton()
        self.left_toggle_button.setIcon(icon_panel("left", True))
        self.left_toggle_button.setIconSize(TOOLBAR_ICON_SIZE)
        self.left_toggle_button.setFixedSize(TOOLBAR_BUTTON_SIZE, TOOLBAR_BUTTON_SIZE)
        self.left_toggle_button.clicked.connect(self.toggle_left_panel)
        self.left_toggle_button.setStyleSheet(ghost_button_style)

        self.right_toggle_button = QPushButton()
        self.right_toggle_button.setIcon(icon_panel("right", True))
        self.right_toggle_button.setIconSize(TOOLBAR_ICON_SIZE)
        self.right_toggle_button.setFixedSize(TOOLBAR_BUTTON_SIZE, TOOLBAR_BUTTON_SIZE)
        self.right_toggle_button.clicked.connect(self.toggle_right_panel)
        self.right_toggle_button.setStyleSheet(ghost_button_style)

        self.new_wildcard_button = QPushButton("ワイルドカード新規作成")
        self.new_wildcard_button.setIcon(icon_new_card(text_gap=True))
        self.new_wildcard_button.setIconSize(icon_text_size())
        self.new_wildcard_button.setFixedHeight(TOOLBAR_BUTTON_SIZE)
        self.new_wildcard_button.setToolTip("ワイルドカード新規作成 (Ctrl+N)")
        self.new_wildcard_button.clicked.connect(lambda checked=False: self.create_wildcard())
        self.new_wildcard_button.setStyleSheet(primary_button_style)

        self.random_generation_button = QPushButton("ランダム生成")
        self.random_generation_button.setIcon(icon_random(text_gap=True))
        self.random_generation_button.setIconSize(icon_text_size())
        self.random_generation_button.setFixedHeight(TOOLBAR_BUTTON_SIZE)
        self.random_generation_button.setToolTip("ランダム生成")
        self.random_generation_button.clicked.connect(lambda checked=False: self.open_random_generation())
        self.random_generation_button.setStyleSheet(secondary_button_style)

        self.settings_button = QPushButton()
        self.settings_button.setIcon(icon_settings())
        self.settings_button.setIconSize(TOOLBAR_ICON_SIZE)
        self.settings_button.setFixedSize(TOOLBAR_BUTTON_SIZE, TOOLBAR_BUTTON_SIZE)
        self.settings_button.setToolTip("設定 (Ctrl+,)")
        self.settings_button.clicked.connect(lambda checked=False: self.open_settings())
        self.settings_button.setStyleSheet(ghost_button_style)

        self.api_lamp = ApiStatusIndicator()
        self.api_lamp.setFixedHeight(TOOLBAR_BUTTON_SIZE)
        self.api_lamp.setToolTip("API: 切断")

        self._api_lamp_signal.connect(self.api_lamp.set_state)

        self._api_monitor = ApiMonitorWorker(self.api, lambda: self.settings, self)
        self._api_monitor.state_changed.connect(self._api_lamp_signal.emit)

        self.top_controls = QWidget()
        self.top_controls.setObjectName("topControls")
        self.top_controls.setFixedHeight(TOOLBAR_HEIGHT)
        self.top_controls.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        toolbar_layout = QHBoxLayout(self.top_controls)
        toolbar_layout.setContentsMargins(4, 2, 4, 2)
        # 間隔は2段組: 同じ役割のペアは GAP_PAIR、役割の違う塊の間は GAP_CLUSTER。
        # addSpacing は基底 spacing に足されるので、差分だけたす。
        toolbar_layout.setSpacing(theme.GAP_PAIR)
        cluster_gap = theme.GAP_CLUSTER - theme.GAP_PAIR
        # 左=ペイン開閉、中央=検索、右=作成系 → 状態 → パネル開閉 → 設定。
        # サムネイルサイズはステータスバー右へ移したので、ここでは幅を使わない。
        toolbar_layout.addWidget(self.pane_tab_group)
        toolbar_layout.addSpacing(cluster_gap)
        toolbar_layout.addWidget(self.search_edit, 1)
        toolbar_layout.addStretch(1)
        toolbar_layout.addSpacing(cluster_gap)
        toolbar_layout.addWidget(self.random_generation_button)
        toolbar_layout.addWidget(self.new_wildcard_button)
        toolbar_layout.addSpacing(cluster_gap)
        toolbar_layout.addWidget(self.api_lamp)
        toolbar_layout.addSpacing(cluster_gap)
        toolbar_layout.addWidget(self.left_toggle_button)
        toolbar_layout.addWidget(self.right_toggle_button)
        toolbar_layout.addSpacing(cluster_gap)
        toolbar_layout.addWidget(self.settings_button)
        root_layout.addWidget(self.top_controls)
        self._update_top_controls_layout()

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(True)
        root_layout.addWidget(self.splitter, 1)

        self.folder_tree = FolderTreeWidget()
        self.folder_tree.setObjectName("folderTree")
        # 0列=名前 / 1列=件数（右寄せ）。仕様②「各フォルダの件数を右寄せ」
        self.folder_tree.setColumnCount(2)
        self.folder_tree.setHeaderLabels(["フォルダ", ""])
        self.folder_tree.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
        tree_header = self.folder_tree.header()
        tree_header.setStretchLastSection(False)
        tree_header.setHighlightSections(False)
        tree_header.setSectionsClickable(False)
        tree_header.setSectionResizeMode(0, QHeaderView.Stretch)
        tree_header.setSectionResizeMode(1, QHeaderView.Fixed)
        self.folder_tree.setColumnWidth(1, 46)
        self.folder_tree.setItemDelegateForColumn(1, TreeCountDelegate(self.folder_tree))
        self.folder_tree.itemSelectionChanged.connect(self._on_folder_changed)
        self.folder_tree.itemExpanded.connect(self._on_folder_item_expanded)
        self.folder_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.folder_tree.customContextMenuRequested.connect(self._show_folder_context_menu)
        self.folder_tree.folder_move_requested.connect(self._on_folder_tree_move_requested)
        self.folder_tree.setDragDropMode(QAbstractItemView.DragDrop)
        self.folder_tree.setDefaultDropAction(Qt.IgnoreAction)
        self.folder_tree.setDragEnabled(True)
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
        # フォルダツリーとLoRAフィルタの縦配分はドラッグで調整し、ui_state.jsonに保存する
        self.left_split = QSplitter(Qt.Vertical)
        self.left_split.setChildrenCollapsible(False)
        self.left_split.setHandleWidth(6)

        self.folder_tree_container = QWidget()
        folder_container_layout = QVBoxLayout(self.folder_tree_container)
        # LoRAコンテナ(左右4px)と幅を揃える
        folder_container_layout.setContentsMargins(4, 0, 4, 0)
        folder_container_layout.setSpacing(0)
        folder_container_layout.addWidget(self.folder_tree, 1)

        self.lora_container = QWidget()
        lora_container_layout = QVBoxLayout(self.lora_container)
        lora_container_layout.setContentsMargins(4, 2, 4, 4)
        lora_container_layout.setSpacing(2)
        lora_header_layout = QHBoxLayout()
        lora_header_layout.setContentsMargins(0, 0, 0, 0)
        lora_title = QLabel("LoRAフィルタ")
        lora_title.setStyleSheet(f"QLabel {{ {theme.heading()} }}")
        lora_header_layout.addWidget(lora_title)
        lora_header_layout.addStretch(1)
        self.lora_deselect_all = QCheckBox("すべて外す")
        self.lora_deselect_all.setChecked(True)
        # ネイティブのチェック印はダークテーマで潰えて見えるため、状態を面で見せる
        self.lora_deselect_all.setStyleSheet(theme.checkbox())
        self.lora_deselect_all.toggled.connect(self._on_lora_deselect_all)
        lora_header_layout.addWidget(self.lora_deselect_all)
        lora_container_layout.addLayout(lora_header_layout)
        self.lora_cloud = QListWidget()
        self.lora_cloud.setObjectName("loraCloud")
        self.lora_cloud.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lora_cloud.customContextMenuRequested.connect(self._show_lora_cloud_context_menu)
        self.lora_cloud.itemChanged.connect(self._on_lora_item_changed)
        lora_container_layout.addWidget(self.lora_cloud, 1)

        # BaseModelフィルタはカード一覧にも効く全体フィルタなので、左パネルの最上部（ツリーより上）に置く
        model_caption = QLabel("BaseModelで絞り込み")
        model_caption.setStyleSheet(f"QLabel {{ {theme.note()} }}")
        self.model_filter_combo = NoWheelComboBox()
        self.model_filter_combo.setToolTip("各LoRAのcm-info.jsonのBaseModelでカード一覧を絞り込む")
        self.model_filter_combo.currentIndexChanged.connect(lambda _idx: self.schedule_apply_filters())
        self._repopulate_model_filter_combo()
        model_filter_block = QWidget()
        model_filter_layout = QVBoxLayout(model_filter_block)
        model_filter_layout.setContentsMargins(4, 2, 4, 4)
        model_filter_layout.setSpacing(2)
        model_filter_layout.addWidget(model_caption)
        model_filter_layout.addWidget(self.model_filter_combo)

        left_layout.addWidget(model_filter_block)
        self.left_split.addWidget(self.folder_tree_container)
        self.left_split.addWidget(self.lora_container)
        self.left_split.setSizes(self.settings.left_split_sizes)
        left_layout.addWidget(self.left_split, 1)
        self.splitter.addWidget(self.left_panel)

        center = QWidget()
        center.setMinimumWidth(520)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(UI_SECTION_SPACING)

        self.card_view = SlowWheelListView(scroll_step=self.settings.scroll_step)
        self.card_view.setObjectName("cardView")
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
        right_scroll.setStyleSheet(theme.scroll_area())
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
        self.path_label.setStyleSheet(
            f"QLabel {{ background: {theme.PANEL_MAIN}; border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS_CONTROL}px; color: {theme.TEXT_MUTED};"
            f" font-size: {theme.SMALL_SIZE + 1}px; }}"
        )
        self._path_label_text = "ファイル未選択"

        preview_panel = QFrame()
        preview_panel.setObjectName("detailPreviewPanel")
        preview_panel.setMinimumWidth(0)
        preview_panel.setStyleSheet(theme.panel("detailPreviewPanel", theme.PANEL_SIDE))
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(UI_CARD_PADDING_X, UI_CARD_PADDING_Y, UI_CARD_PADDING_X, UI_CARD_PADDING_Y)
        preview_layout.setSpacing(UI_SECTION_SPACING)

        self.thumbnail_label = ResponsivePixmapLabel("サムネイルなし")
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        # プレビューを一段小さくし、下段（タグ・元テキスト面板）の縦空間を確保する
        self.thumbnail_label.setMinimumHeight(320)
        self.thumbnail_label.setMaximumHeight(440)
        self.thumbnail_label.setMinimumWidth(0)
        self.thumbnail_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.thumbnail_label.setStyleSheet(
            f"border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_CARD}px;"
            f" background: {theme.APP_BG};"
        )
        preview_layout.addWidget(self.thumbnail_label)
        preview_layout.addWidget(self.path_label)

        preview_layout.addWidget(QLabel("LoRA"))
        self.lora_list = QListWidget()
        self._apply_lora_list_height(0)
        self.lora_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.lora_list.setMinimumWidth(0)
        self.lora_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lora_list.customContextMenuRequested.connect(self._show_lora_context_menu)
        preview_layout.addWidget(self.lora_list)
        right_content_layout.addWidget(preview_panel, 2)

        editor_panel = QFrame()
        editor_panel.setObjectName("detailEditorPanel")
        editor_panel.setMinimumWidth(0)
        editor_panel.setStyleSheet(theme.panel("detailEditorPanel", theme.PANEL_SIDE))
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(UI_CARD_PADDING_X, UI_CARD_PADDING_Y, UI_CARD_PADDING_X, UI_CARD_PADDING_Y)
        editor_layout.setSpacing(UI_SECTION_SPACING)
        copy_buttons_row = QHBoxLayout()
        # コピー2つは同じ役割のペア。面板のセクション間隔（16）ではなくクラスタ間隔で寄せる
        copy_buttons_row.setSpacing(theme.GAP_CLUSTER)
        self.copy_all_with_lora_button = QPushButton("LoRAありでコピー")
        self.copy_all_with_lora_button.setToolTip("カード本文を <lora:...> タグ付きのままコピー")
        self.copy_all_with_lora_button.clicked.connect(lambda: self.copy_all_text(True))
        self.copy_all_without_lora_button = QPushButton("LoRAなしでコピー")
        self.copy_all_without_lora_button.setToolTip("<lora:...> タグだけ抜いてコピー（LoRAを削除しても安全）")
        self.copy_all_without_lora_button.clicked.connect(lambda: self.copy_all_text(False))
        self.copy_all_with_lora_button.setMinimumWidth(0)
        self.copy_all_without_lora_button.setMinimumWidth(0)
        self.copy_all_with_lora_button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.copy_all_without_lora_button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        # 仕様①の階層: 主操作(LoRAあり)=Primary / 副操作=Secondary
        self.copy_all_with_lora_button.setStyleSheet(theme.button("primary"))
        self.copy_all_without_lora_button.setStyleSheet(theme.button("secondary"))
        copy_buttons_row.addWidget(self.copy_all_with_lora_button)
        copy_buttons_row.addWidget(self.copy_all_without_lora_button)
        self.custom_tags_edit = TagEditorWidget()
        self.custom_tags_edit.setMinimumWidth(0)
        editor_layout.addWidget(self.custom_tags_edit)
        self.custom_tags_edit.hide()
        editor_layout.addWidget(QLabel("プロンプトタグ"))
        self.prompt_tags_edit = TagEditorWidget()
        self.prompt_tags_edit.setMinimumWidth(0)
        self.prompt_tags_edit.tagsChanged.connect(self._sync_editor_from_prompt_tags)
        self.prompt_tags_edit.searchRequested.connect(self._search_by_tag)
        # コピー成否のトースト。QToolTip はマウス移動で消えて読めないため、
        # ウィンドウ下部に出る ToastPopup を使う（ユーザー指摘の「短い」対策）
        self.prompt_tags_edit.tagCopied.connect(lambda t: self.show_toast(f"コピー: {t}", 4000))
        editor_layout.addWidget(self.prompt_tags_edit)
        # 階層順にコピーはタグの下（読む→使う順）
        editor_layout.addLayout(copy_buttons_row)
        right_content_layout.addWidget(editor_panel)

        # 仕様④: 元テキストは独立した面板にし、編集はその面板の右上に置く
        text_panel = QFrame()
        text_panel.setObjectName("detailTextPanel")
        text_panel.setMinimumWidth(0)
        text_panel.setStyleSheet(theme.panel("detailTextPanel", theme.PANEL_SIDE))
        text_layout = QVBoxLayout(text_panel)
        text_layout.setContentsMargins(UI_CARD_PADDING_X, UI_CARD_PADDING_Y, UI_CARD_PADDING_X, UI_CARD_PADDING_Y)
        text_layout.setSpacing(UI_SECTION_SPACING)
        editor_header_row = QHBoxLayout()
        editor_header_row.setContentsMargins(0, 0, 0, 0)
        editor_header_row.setSpacing(theme.GAP_PAIR)
        editor_header_row.addWidget(QLabel("元テキスト"))
        editor_header_row.addStretch(1)
        self.delete_tags_button = QPushButton("削除")
        self.delete_tags_button.setStyleSheet(theme.danger_button(content_height=UI_BUTTON_HEIGHT))
        self.delete_tags_button.setEnabled(False)
        self.delete_tags_button.clicked.connect(self._delete_selected_tags)
        self.delete_tags_button.hide()
        editor_header_row.addWidget(self.delete_tags_button)
        self.edit_button = QPushButton("編集")
        self.edit_button.setStyleSheet(theme.button("secondary"))
        self.edit_button.clicked.connect(self.toggle_edit_mode)
        editor_header_row.addWidget(self.edit_button)
        text_layout.addLayout(editor_header_row)
        self.editor = QTextEdit()
        self.editor.setMinimumHeight(80)
        self.editor.setMinimumWidth(0)
        self.editor.setReadOnly(True)
        self.editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.editor.textChanged.connect(self._sync_tags_from_editor)
        text_layout.addWidget(self.editor, 1)
        right_content_layout.addWidget(text_panel, 1)
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
        # 仕様⑤: 左=件数などの情報、右=サムネイルサイズのズーム、高さは抑えて常設バーにする
        status_bar.setStyleSheet(f"""
            QStatusBar {{
                background: {theme.PANEL_SIDE};
                border-top: 1px solid {theme.BORDER};
                color: {theme.TEXT_MUTED};
                font-size: {theme.SMALL_SIZE + 1}px;
            }}
            QStatusBar::item {{ border: none; }}
            QStatusBar QLabel {{ color: {theme.TEXT_MUTED}; background: transparent; }}
        """)
        status_bar.setFixedHeight(theme.SPACE + theme.CHIP_HEIGHT)
        self.setStatusBar(status_bar)
        # 参照デザインどおり、サムネイルサイズの操作は下部ステータスバー右に常設する
        status_bar.addPermanentWidget(self.size_group)

        self.card_view.selectionModel().currentChanged.connect(self._on_card_selection_changed)
        self.card_view.selectionModel().selectionChanged.connect(self._update_selection_count)
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
        self.action_copy_with_lora = add_action("LoRAありでコピー", lambda: self.copy_all_text(True), "Ctrl+C")
        self.action_copy_without_lora = add_action("LoRAなしでコピー", lambda: self.copy_all_text(False), "Ctrl+Shift+C")
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
        self.action_thumbnail_assign = add_action("サムネイルに画像を指定...", self.assign_thumbnail_from_current)
        self.action_thumbnail_add = add_action("サムネイルに画像を追加...", self.add_thumbnail_from_current)
        self.action_thumbnail_delete = add_action("表示中のサムネイルを削除...", self.delete_current_thumbnail)
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
        tools_menu.addAction(self.action_thumbnail_assign)
        tools_menu.addAction(self.action_thumbnail_add)
        tools_menu.addAction(self.action_thumbnail_delete)
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
        Ctrl+C - LoRAありでコピー<br>
        Ctrl+Shift+C - LoRAなしでコピー<br>
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
        # isVisible() だとウィンドウ表示前は常にFalseで「閉じる」が効かない。
        # isHidden()（明示的なhideフラグ）で反転させる。
        self.folder_tree_container.setVisible(self.folder_tree_container.isHidden())
        self._update_panel_toggle_buttons()

    def _switch_to_lora_tab(self) -> None:
        self.lora_container.setVisible(self.lora_container.isHidden())
        self._update_panel_toggle_buttons()

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
        # キーボード/設定からの変更もスライダーに反映する（valueChangedのループは防止）
        slider = getattr(self, "thumbnail_size_slider", None)
        if slider is not None and slider.value() != value:
            with QSignalBlocker(slider):
                slider.setValue(value)

    def _on_thumbnail_size_slid(self, value: int) -> None:
        """ドラッグ中は数値表示だけ動かし、止まってからカード再レイアウトを適用する。
        毎シグナル適用だと全サムネイルの読み直しが入って重くなる。
        値は THUMBNAIL_SIZE_STEP 刻みに寄せる（PySide6 の QSlider に setSnapToTicks は無い）。"""
        steps = round((value - THUMBNAIL_SIZE_MIN) / THUMBNAIL_SIZE_STEP)
        snapped = max(THUMBNAIL_SIZE_MIN, min(THUMBNAIL_SIZE_MAX, THUMBNAIL_SIZE_MIN + steps * THUMBNAIL_SIZE_STEP))
        if snapped != value:
            with QSignalBlocker(self.thumbnail_size_slider):
                self.thumbnail_size_slider.setValue(snapped)
        self.thumbnail_size_display.setText(f"{snapped} px")
        self._size_apply_timer.start()

    def _apply_uniform_control_height(self, height: int = UI_CONTROL_HEIGHT) -> None:
        widgets = [
            self.search_edit,
        ]
        for widget in widgets:
            widget.setFixedHeight(height)

        button_widgets = [
            self.copy_all_with_lora_button,
            self.copy_all_without_lora_button,
            self.edit_button,
        ]
        for widget in button_widgets:
            # app_style の既定ボタンは content 34px + border 2px = 外側36px。
            # styleSheet の max-height が setFixedHeight より優先されるので同じ値に揃える。
            widget.setFixedHeight(theme.BUTTON_HEIGHT)

        self.path_label.setMinimumHeight(height)
        self.path_label.setMaximumHeight(height)

    def _apply_top_controls_compact_height(self) -> None:
        # サムネイルサイズ操作はステータスバー側へ移したのでここでは固定しない
        for widget in (
            self.search_edit,
            self.random_generation_button,
            self.new_wildcard_button,
            self.pane_tab_group,
            self.left_toggle_button,
            self.right_toggle_button,
            self.settings_button,
        ):
            widget.setFixedHeight(TOOLBAR_BUTTON_SIZE)
        self.top_controls.setFixedHeight(TOOLBAR_HEIGHT)

    def _set_result_count_label(self, count: int) -> None:
        if self.current_folder_prefix == "__favorites__":
            folder_info = "お気に入り"
        elif self.current_folder_prefix.startswith("__favorites__/"):
            folder_info = "お気に入り/" + self.current_folder_prefix[len("__favorites__/"):]
        else:
            folder_info = self.current_folder_prefix or "すべて"
        thumb_count = sum(1 for e in self.filtered_entries if e.has_thumbnail)
        selected = len(self.card_view.selectionModel().selectedIndexes()) if self.card_view.selectionModel() else 0
        selected_unique = len(set(
            self.list_model.entry_at(idx).abs_path
            for idx in self.card_view.selectionModel().selectedIndexes()
            if self.list_model.entry_at(idx) is not None
        )) if self.card_view.selectionModel() else 0
        selection_str = f" | 選択: {selected_unique}" if selected_unique > 0 else ""
        self.statusBar().showMessage(
            f"{count}件表示中{selection_str} | フォルダ: {folder_info} | サムネ: {thumb_count}/{count}",
            0
        )

    def _update_selection_count(self) -> None:
        """選択数が変わった時にステータスバーの選択表示を更新する。"""
        self._set_result_count_label(len(self.filtered_entries))

    def _refresh_path_label(self) -> None:
        # L9 修正: _path_label_text は _build_ui で確実に初期化されるため、
        # 防衛的な getattr は不要。直接属性アクセスに変更。
        raw_text = self._path_label_text or "ファイル未選択"
        available_width = max(80, self.path_label.width() - 12)
        text = self.path_label.fontMetrics().elidedText(raw_text, Qt.ElideMiddle, available_width)
        self.path_label.setText(text)

    def _update_view_sizes(self) -> None:
        width = max(80, self.settings.thumbnail_size)
        height = int(width * theme.CARD_ICON_RATIO)
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

    def _apply_lora_list_height(self, rows: int) -> None:
        # 件数が少ないときに4行分の空白を残さないよう、表示行数に応じて高さを変える
        self.lora_list.setFixedHeight(self._lora_list_height(min(max(rows, 1), 4)))

    def _update_preview_pixmap(self) -> None:
        if self.current_entry is None:
            self._preview_cache_key = None
            self._preview_cache_pixmap = None
            self.thumbnail_label.setPixmap(QPixmap())
            self.thumbnail_label.setText("サムネイルなし")
            return
        if self.thumbnail_loading_message:
            return
        # めくった枚数を右パネルにも反映する（カードと同じ画像を見たい）
        preview_path = self.list_model.display_path_for(self.current_entry)
        if preview_path and Path(preview_path).exists():
            target_size = self.thumbnail_label.contentsRect().size()
            if target_size.width() > 0 and target_size.height() > 0:
                cache_key = (preview_path, target_size.width(), target_size.height())
                if self._preview_cache_key == cache_key and self._preview_cache_pixmap is not None:
                    self.thumbnail_label.setPixmap(self._preview_cache_pixmap)
                    self.thumbnail_label.setText("")
                    return
                self.thumbnail_label.setPixmap(QPixmap())
                self.thumbnail_label.setText("読み込み中...")
                self._preview_request_token += 1
                token = self._preview_request_token
                self._queue_preview_load(self.current_entry.abs_path, preview_path, target_size.width(), target_size.height(), token)
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
        # アイコンの帯が「塗り=表示中 / 点線枠=隠れている」。向き矢印はどちらの
        # パネルを指しているのか読めないので使わない（ユーザー指摘）。
        sizes = self.splitter.sizes()
        left_visible = len(sizes) == 3 and sizes[0] > 0
        right_visible = len(sizes) == 3 and sizes[2] > 0
        self.left_toggle_button.setIcon(icon_panel("left", left_visible))
        self.left_toggle_button.setToolTip("フォルダパネルを隠す" if left_visible else "フォルダパネルを表示")
        self.right_toggle_button.setIcon(icon_panel("right", right_visible))
        self.right_toggle_button.setToolTip("詳細パネルを隠す" if right_visible else "詳細パネルを表示")
        # isHidden() は親の表示状態に依存しない明示フラグなので、ウィンドウ表示前でも
        # 「開いている方」が正しく光る（isVisible() だと起動時に両方消灯する）
        self.folder_tree_button.setChecked(not self.folder_tree_container.isHidden())
        self.lora_button.setChecked(not self.lora_container.isHidden())

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
            self._sync_gallery_source()
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

            self._rebuild_folder_maps()
            self._rebuild_folder_tree()
            current_item = self.folder_tree.currentItem()
            self.current_folder_prefix = current_item.data(0, Qt.UserRole) if current_item else ""
            self._refresh_lora_base_model_map()
            self.apply_filters()
            # 【F5修正】再スキャン後に全サムネイルキャッシュをクリアして
            # 強制再描画。サムネイルファイルが同じパスに上書き保存された場合、
            # set_entries の fields_changed チェックでは検出できないため。
            self.list_model.force_reload_all()
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

    def _find_all_item(self) -> QTreeWidgetItem | None:
        for i in range(self.folder_tree.topLevelItemCount()):
            item = self.folder_tree.topLevelItem(i)
            if item and item.data(0, Qt.UserRole) == "":
                return item
        return None

    def _find_favorites_item(self) -> QTreeWidgetItem | None:
        for i in range(self.folder_tree.topLevelItemCount()):
            item = self.folder_tree.topLevelItem(i)
            if item and item.data(0, Qt.UserRole) == "__favorites__":
                return item
        return None

    def _rebuild_folder_maps(self) -> None:
        """エントリ由来のフォルダツリー + ディスク上の空フォルダ + お気に入り階層を再構築する。"""
        folder_children: dict[str, set[str]] = {}
        for entry in self.entries:
            if not entry.folder:
                continue
            chain = ""
            for part in entry.folder.split("/"):
                parent_key = chain
                chain = part if not chain else f"{chain}/{part}"
                folder_children.setdefault(parent_key, set()).add(part)
        # 空フォルダ（wildcard が1件も無いディレクトリ）もツリーに表示するため、
        # ディスク上の実ディレクトリを走査してマージする。
        root = Path(self.settings.library_root) if self.settings.library_root else None
        if root is not None and root.exists():
            try:
                for disk_dir in root.rglob("*"):
                    if not disk_dir.is_dir():
                        continue
                    try:
                        rel = disk_dir.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    chain = ""
                    for part in rel.split("/"):
                        parent_key = chain
                        chain = part if not chain else f"{chain}/{part}"
                        folder_children.setdefault(parent_key, set()).add(part)
            except OSError:
                log.debug("Failed to scan disk folders for tree", exc_info=True)
        self.folder_children_map = folder_children
        self._rebuild_fav_children_map()

    def _rebuild_fav_children_map(self) -> None:
        """お気に入り登録済みエントリの元フォルダ階層をマップ化する。"""
        fav_children: dict[str, set[str]] = {}
        for entry in self.entries:
            if entry.abs_path not in self.favorite_paths or not entry.folder:
                continue
            chain = ""
            for part in entry.folder.split("/"):
                parent_key = chain
                chain = part if not chain else f"{chain}/{part}"
                fav_children.setdefault(parent_key, set()).add(part)
        self.fav_folder_children_map = fav_children

    def _refresh_favorites_tree(self) -> None:
        """お気に入りのトグル後にお気に入り配下の子だけを再構築する（件数も更新）。"""
        self._rebuild_fav_children_map()
        fav_item = self._find_favorites_item()
        if fav_item is None:
            return
        self._folder_counts = self._build_folder_counts()
        self._set_folder_item_count(fav_item)
        if self.fav_folder_children_map.get(""):
            fav_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        if not self._is_folder_item_populated(fav_item):
            return
        self._folder_tree_populating = True
        try:
            fav_item.takeChildren()
            fav_item.setData(0, Qt.UserRole + 1, False)
            self._populate_folder_children(fav_item)
        finally:
            self._folder_tree_populating = False

    def _rebuild_folder_tree(self) -> None:
        self._folder_tree_populating = True
        self._folder_counts = self._build_folder_counts()
        try:
            self.folder_tree.clear()
            self.folder_tree.setUpdatesEnabled(False)
            self.folder_tree.blockSignals(True)

            fav_item = QTreeWidgetItem(["お気に入り", ""])
            fav_item.setData(0, Qt.UserRole, "__favorites__")
            fav_item.setIcon(0, icon_star())
            fav_item.setData(0, Qt.UserRole + 1, False)
            self._set_folder_item_count(fav_item)
            if self.fav_folder_children_map.get(""):
                fav_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            self.folder_tree.addTopLevelItem(fav_item)

            root = QTreeWidgetItem(["(all)", ""])
            root.setData(0, Qt.UserRole, "")
            root.setIcon(0, icon_home())
            root.setData(0, Qt.UserRole + 1, False)
            self._set_folder_item_count(root)
            self.folder_tree.addTopLevelItem(root)

            self._populate_folder_children(root)
            self.folder_tree.expandItem(root)
            self._ensure_folder_path(self.current_folder_prefix, root)
            target_item = self._find_folder_item(self.current_folder_prefix) or root
            self.folder_tree.setCurrentItem(target_item)
        finally:
            self.folder_tree.blockSignals(False)
            self.folder_tree.setUpdatesEnabled(True)
            self._folder_tree_populating = False

    def _folder_item_key(self, item: QTreeWidgetItem) -> str:
        return str(item.data(0, Qt.UserRole) or "")

    def _build_folder_counts(self) -> dict[str, int]:
        """ツリー各ノードの件数。表示フィルタと同じ「自分＋子孫」の再帰カウント。
        お気に入りノードは favorite_paths との積だけで数える（クリック時の結果と一致させる）。"""
        counts: dict[str, int] = {}
        for entry in self.entries:
            parts = entry.folder.split("/") if entry.folder else []
            ancestors = [""] + ["/".join(parts[: i + 1]) for i in range(len(parts))]
            for key in ancestors:
                counts[key] = counts.get(key, 0) + 1
            if entry.abs_path in self.favorite_paths:
                for key in ancestors:
                    fav_key = "__favorites__" if not key else f"__favorites__/{key}"
                    counts[fav_key] = counts.get(fav_key, 0) + 1
        return counts

    def _set_folder_item_count(self, item: QTreeWidgetItem) -> None:
        item.setText(1, str(self._folder_counts.get(self._folder_item_key(item), 0)))

    def _is_folder_item_populated(self, item: QTreeWidgetItem) -> bool:
        return bool(item.data(0, Qt.UserRole + 1))

    def _mark_folder_item_populated(self, item: QTreeWidgetItem) -> None:
        item.setData(0, Qt.UserRole + 1, True)

    def _populate_folder_children(self, item: QTreeWidgetItem) -> None:
        if self._is_folder_item_populated(item):
            return
        folder_key = self._folder_item_key(item)
        if folder_key == "__favorites__" or folder_key.startswith("__favorites__/"):
            # お気に入りツリー: 元フォルダ構造を ★ お気に入り 配下にミラーする
            sub_key = folder_key[len("__favorites__/"):] if folder_key.startswith("__favorites__/") else ""
            children_map = self.fav_folder_children_map
            key_prefix = folder_key
        else:
            sub_key = folder_key
            children_map = self.folder_children_map
            key_prefix = folder_key
        for child_name in sorted(children_map.get(sub_key, set()), key=natural_sort_key):
            child_rel = child_name if not sub_key else f"{sub_key}/{child_name}"
            child_key = f"{key_prefix}/{child_name}" if key_prefix else child_name
            child = QTreeWidgetItem([child_name, ""])
            child.setData(0, Qt.UserRole, child_key)
            child.setData(0, Qt.UserRole + 1, False)
            child.setIcon(0, icon_folder())
            self._set_folder_item_count(child)
            if children_map.get(child_rel):
                child.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            item.addChild(child)
        self._mark_folder_item_populated(item)

    def _find_folder_item(self, folder_key: str) -> QTreeWidgetItem | None:
        if folder_key == "__favorites__" or folder_key.startswith("__favorites__/"):
            fav_item = self._find_favorites_item()
            if fav_item is None:
                return None
            if folder_key == "__favorites__":
                return fav_item
            parts = folder_key[len("__favorites__/"):].split("/")
            item = fav_item
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
        all_item = self._find_all_item()
        if not folder_key:
            return all_item
        if all_item is None:
            return None
        parts = folder_key.split("/")
        item = all_item
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
        if folder_key == "__favorites__" or folder_key.startswith("__favorites__/"):
            fav_item = self._find_favorites_item()
            if fav_item is None:
                return
            if folder_key == "__favorites__":
                fav_item.setExpanded(True)
                return
            item = fav_item
            parts = folder_key[len("__favorites__/"):].split("/")
        else:
            item = root or self._find_all_item()
            if item is None:
                return
            parts = folder_key.split("/")
        for part in parts:
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

    @staticmethod
    def _base_model_from_name(text: str) -> str:
        """LoRA名／ベース名から 'Anima' / 'Illustrious' / '' を名前言依りで判定する。

        'anime' と 'anima' を取り違えないよう、照合はセパレータ区切りの
        トークン完全一致に限る。'il' も単独トークンまたは il+バージョン
        （ilv5 等）のみ有効で、'detail' / 'file' / 'wild' 等の単語内 'il' は
        反応しない。
        """
        tokens = [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]
        if "anima" in tokens:
            return "Anima"
        if any("illustrious" in t for t in tokens):
            return "Illustrious"
        if any(re.fullmatch(r"il(v\d\w*)?", t) for t in tokens):
            return "Illustrious"
        return ""

    @staticmethod
    def _classify_safetensor_header(path: Path) -> str:
        """safetensorsヘッダーのみで 'Anima' / 'Illustrious' / '' を返す。

        cm-info.json が無い・BaseModel が Unknown の場合のフォールバック。
        実測に基づく根拠:
        - anima はヘッダーで確定できる (modelspec.architecture='anima*/lora',
          ss_base_model_version='anima', テンソルキーが diffusion_model.blocks.*
          または lora_unet_blocks_* でSDXL系の lora_te1_* と重ならない)。
        - illustrious はヘッダー単体ではSDXL系までしか言えない
          (architecture='stable-diffusion-xl-v1-base/lora',
          ss_base_model_version='sdxl_*' はPony等と共通) ため、
          学習時ベース名・LoRA名のトークンに 'illustrious' または
          '_il' 系（'il', 'ilv5' 等）を含む場合のみ 'Illustrious' とする
          （ファイル名依存のヒューリスティック）。
        - 'anime' と 'anima' を取り違えないよう、名前の照合は
          セパレータ区切りのトークン完全一致で行う。素朴な部分一致は禁止
         （'pornmasterAnime_ilV5' を anima と誤判定する等の事故を防ぐ）。
          'il' も単独トークンまたは il+バージョンのみ有効とし、
          'detail' / 'file' / 'wild' 等の単語内 'il' には反応しない。
        """
        try:
            with path.open("rb") as handle:
                (size,) = struct.unpack("<Q", handle.read(8))
                if size > 10 * 1024 * 1024:
                    return ""
                header = json.loads(handle.read(size))
        except Exception:
            return ""
        try:
            md = header.get("__metadata__", {}) or {}
            arch = str(md.get("modelspec.architecture", "") or "").lower()
            ver = str(md.get("ss_base_model_version", "") or "").lower()
            sdm = str(md.get("ss_sd_model_name", "") or "")
            tokens: list[str] = []
            for text in (sdm, path.stem):
                tokens.extend(
                    t for t in re.split(r"[^a-z0-9]+", text.lower()) if t
                )
            if arch.startswith("anima") or ver == "anima" or "anima" in tokens:
                return "Anima"
            names = [k for k in header.keys() if k != "__metadata__"][:400]
            fingerprint = " ".join(names).lower()
            if "diffusion_model.blocks" in fingerprint or "lora_unet_blocks" in fingerprint:
                return "Anima"
            if any("illustrious" in t for t in tokens):
                return "Illustrious"
            if any(re.fullmatch(r"il(v\d\w*)?", t) for t in tokens):
                return "Illustrious"
        except Exception:
            return ""
        return ""

    def _lora_search_roots(self) -> list[Path]:
        """LoRA重みとメタデータが置かれ得るフォルダ一覧を返す。

        Neo/Forge は `<lora:x>` を Lora/ だけでなく LyCORIS/ からも読むため、
        cm-info.json は兄弟の LyCORIS にも存在する
        （例: LyCORIS/bunny girl_illustrious_V1.0.cm-info.json）。
        """
        lora_root = (self.settings.lora_root or "").strip()
        if not lora_root:
            return []
        primary = Path(lora_root)
        if not primary.is_dir():
            return []
        roots = [primary]
        sibling = primary.parent / "LyCORIS"
        if sibling.is_dir():
            roots.append(sibling)
        return roots

    @staticmethod
    def _walk_lora_files(roots: list[Path]) -> tuple[dict[str, Path], list[Path], list[Path]]:
        """LoRAフォルダ群を1回だけ歩き、重みとsidecarに振り分ける。

        返すのは ①重みパス索引 `{stem.lower(): Path}` ②cm-info.json 一覧
        ③civitai.info 一覧。ファイル内容は読まず、名前の拡張子だけで分類する。
        LOST 判定は ① の実在性だけで行う：sidecar（cm-info / civitai.info /
        metadata.json / preview画像）は重み削除後も残るため、sidecarの有無は
        「LoRAが読み込めるか」を意味しない。
        """
        weights: dict[str, Path] = {}
        cm_infos: list[Path] = []
        civitai_infos: list[Path] = []
        for root in roots:
            try:
                paths = list(root.rglob("*"))
            except Exception:
                continue
            for path in paths:
                low = path.name.lower()
                if low.endswith(".cm-info.json"):
                    cm_infos.append(path)
                elif low.endswith(".civitai.info"):
                    civitai_infos.append(path)
                elif low.endswith((".safetensors", ".ckpt", ".pt", ".bin")):
                    weights.setdefault(lora_name_key(path.stem), path)
        return weights, cm_infos, civitai_infos

    @staticmethod
    def _lora_tail_name(key: str) -> str:
        """`<lora:sub/name>` 形式の参照を末尾名でも拾えるようにする。"""
        return key.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]

    @classmethod
    def _lora_name_present(cls, key: str, index: dict[str, Path]) -> bool:
        """索引上で重みファイルが実在するか。フォルダ付き参照は末尾名でも見る。"""
        return key in index or cls._lora_tail_name(key) in index

    @classmethod
    def _compute_lost_lora_names(cls, referenced: set[str], index: dict[str, Path]) -> set[str]:
        return {key for key in referenced if not cls._lora_name_present(key, index)}

    def _refresh_lora_weight_index(self) -> dict[str, Path]:
        """名前walkだけで重み索引を作り直し、LOST集合にも反映する（実測 93ms）。

        baseモデル判定（sidecar読込＋ヘッダー抽出で1.5s）は行わないので、
        ill/aniバッジの鮮度は `_refresh_lora_base_model_map`（手動再スキャン）側で
        更新されるまで据え置きになる。
        `_lost_lora_names` は中身その入れ替えにする（list_model と同じオブジェクトを
        共有しているため、差し替えると表示側が古い集合を参照し続ける）。
        """
        roots = [root for root in self._lora_search_roots() if root.is_dir()]
        index, _cm_infos, _civitai_infos = self._walk_lora_files(roots)
        referenced: set[str] = set()
        for entry in self.entries:
            for name in entry.lora_names or []:
                key = lora_name_key(name)
                if key:
                    referenced.add(key)
        lost = self._compute_lost_lora_names(referenced, index)
        changed = lost != self._lost_lora_names
        self._lost_lora_names.clear()
        self._lost_lora_names.update(lost)
        if changed:
            self.card_view.viewport().update()
        return index

    def _missing_copy_lora_names(self, text: str) -> list[str]:
        """コピーするテキスト中の `<lora:x>` で、ディスク上に重みが無い物を元の表記で返す。

        QFileSystemWatcher が張っているのはワイルドカードライブラリ側だけで、
        LoRAフォルダの削除は追従しない。そのため索引を信じずコピー時に必ず
        名前walkをやり直す（実測 93〜130ms。ヘッダー解析1.5sは伴わない）。
        LoRAフォルダが未設定・未存在なら実在性そのものが判定不能なので、
        全LoRAを欠陥品として警告せずそのまま通す。
        """
        names = extract_lora_names(text)
        if not names:
            return []
        if not any(root.is_dir() for root in self._lora_search_roots()):
            return []
        index = self._refresh_lora_weight_index()
        return [name for name in names
                if not self._lora_name_present(lora_name_key(name), index)]

    @staticmethod
    def _scan_lora_weight_headers(weights: dict[str, Path]) -> dict[str, tuple[Path, dict[str, str]]]:
        """重みファイルのヘッダーからBaseModel判定に使う値だけを抽出する。

        返り値は `{lora名.lower(): (パス, {キー: 値})}`。値はすべて小文字化する。
        ヘッダーが異常に大きい・読めないファイルは値なしで登録する
        （重みが存在するという事実自体が次の判定に効くため）。
        """
        found: dict[str, tuple[Path, dict[str, str]]] = {}
        for key, path in weights.items():
            fields: dict[str, str] = {}
            try:
                with path.open("rb") as handle:
                    (size,) = struct.unpack("<Q", handle.read(8))
                    if size > LORA_HEADER_MAX_BYTES:
                        found[key] = (path, fields)
                        continue
                    blob = handle.read(size)
            except Exception:
                found[key] = (path, fields)
                continue
            for name, pattern in LORA_HEADER_PATTERNS.items():
                match = pattern.search(blob)
                if match:
                    fields[name] = match.group(1).decode("utf-8", "replace").strip().lower()
            found[key] = (path, fields)
        return found

    @staticmethod
    def _build_base_hash_registry(
        headers: dict[str, tuple[Path, dict[str, str]]],
        mapping: dict[str, str],
    ) -> dict[str, str]:
        """BaseModel確定済みLoRAの「学習時ベースチェックポイントのハッシュ」を集計する。

        kohyaはヘッダーに `ss_new_sd_model_hash`（ベース実体のSHA256）と
        `ss_sd_model_hash`（同レガシーハッシュ）を書き込む。これは学習に使った
        ベースモデルそのものの識別子なので、BaseModelが確定しているLoRAと同じ
        ハッシュなら、そのLoRAも同じBaseModelで学習されていることになる。
        `ss_sd_model_name` が `889818.safetensors` のようにCivitaiの数値IDだけの
        場合、名从前手が全く効かないが、このハッシュ経路なら確定できる。

        投票が割れているハッシュ（Illustrious/NoobAI 混在など）は除外する。
        """
        votes: dict[str, Counter[str]] = {}
        display: dict[str, str] = {}
        for name, (_path, fields) in headers.items():
            raw = str(mapping.get(name) or "").strip()
            base = raw.lower()
            if not base or base in ("unknown", "none", "other"):
                continue
            display.setdefault(base, raw)
            for key in ("new_hash", "old_hash"):
                digest = fields.get(key) or ""
                if digest:
                    votes.setdefault(digest, Counter())[base] += 1
        registry: dict[str, str] = {}
        for digest, counter in votes.items():
            base, top = counter.most_common(1)[0]
            if top / sum(counter.values()) >= LORA_HASH_REGISTRY_MIN_RATIO:
                registry[digest] = display.get(base, base)
        return registry

    def _refresh_lora_base_model_map(self) -> None:
        """ベースモデルスキャンをスレッドプールで開始する（メインスレッドを塞がない）。

        全LoRAフォルダの歩行＋全safetensorsヘッダー解析で実測1.5s以上かかるため、
        起動時にメインスレッドで実行するとWindowsの「応答なし」になる。入力
        （roots / referenced）だけメインスレッドで取り、結果は
        `lora_base_scan_done` シグナル経由で `_apply_lora_base_model_result`
        に反映する。実行中は `_lora_base_scan_running` で多重起動を防ぐ。
        """
        if self._lora_base_scan_running:
            return
        roots = self._lora_search_roots()
        referenced: set[str] = set()
        for entry in self.entries:
            for name in entry.lora_names or []:
                key = lora_name_key(name)
                if key:
                    referenced.add(key)
        self._lora_base_scan_running = True

        def work() -> None:
            try:
                result = MainWindow._scan_lora_base_model_snapshot(roots, referenced)
            except Exception:
                log.exception("LoRAベースモデルスキャン失敗")
                result = None
            self.lora_base_scan_done.emit(result)

        QThreadPool.globalInstance().start(work)

    def _apply_lora_base_model_result(self, result: object) -> None:
        """ワーカースレッドの走査結果をメインスレッドでUIへ反映する。"""
        self._lora_base_scan_running = False
        if not isinstance(result, tuple):
            return
        mapping, lost = result
        log.info("LoRAベースモデル走査完了 map=%d lost=%d", len(mapping), len(lost))
        # list_model と同じオブジェクトを共有し続けるため中身その入れ替えにする
        # （`_refresh_lora_weight_index` と同じ理由）
        self._lora_base_model_map.clear()
        self._lora_base_model_map.update(mapping)
        self._lost_lora_names.clear()
        self._lost_lora_names.update(lost)
        self._repopulate_model_filter_combo()
        self.card_view.viewport().update()
        self.schedule_apply_filters()

    @classmethod
    def _scan_lora_base_model_snapshot(
        cls,
        roots: "list[Path]",
        referenced: "set[str]",
    ) -> "tuple[dict[str, str], set[str]]":
        """{lora名.lower(): BaseModel} と LOST 集合を作る（ワーカースレッド実行）。

        根拠の強い順に埋める。
        ①`{lora名}.cm-info.json` の BaseModel（一次情報。Civitai Model Converterが書く）
        ②`{lora名}.civitai.info` の baseModel
        ③ベースチェックポイントのハッシュ registry（`_build_base_hash_registry`）
        ④safetensorsヘッダー自身（architecture / ss_base_model_version / 学習時ベース名）
        ⑤LoRA名自身のトークン（illustrious / anima / il系）
        ③は全重みを1回スキャンして作るが、確定済みLoRAの投票が9割以上一つに固まる
        ハッシュだけ使うので推測ではなく実測根拠になる。④⑤はワイルドカードから
        参照されている名前にだけ適用する。失敗ファイルは無視する。

        同じ歩行で「参照されているのに重みファイルが存在しないLoRA名」も求め、
        カードの LOST 表示（減光＋ピル）に使う。sidecarだけ残っている場合も
        Neo/Forge は読み込めないので LOST 側に入る。

        Qtオブジェクトには触れず、ディスクI/Oと純データだけを行うので
        ワーカースレッドから呼んで安全。
        """
        mapping: dict[str, str] = {}

        def record(name: str, base_model: str, *, overwrite: bool) -> None:
            key = lora_name_key(name)
            base_model = str(base_model or "").strip()
            if not key or not base_model:
                return
            if not overwrite:
                known = (mapping.get(key) or "").strip().lower()
                if known and known != "unknown":
                    return
            mapping[key] = base_model

        weight_index, cm_infos, civitai_infos = cls._walk_lora_files(roots)
        for cm_info in cm_infos:
            try:
                stem = cm_info.stem  # "{name}.cm-info"
                lora_name = stem[:-len(".cm-info")] if stem.endswith(".cm-info") else stem
                with cm_info.open("r", encoding="utf-8") as handle:
                    cm_data = json.load(handle)
                record(lora_name, cm_data.get("BaseModel", ""), overwrite=True)
            except Exception:
                continue
        for info in civitai_infos:
            try:
                with info.open("r", encoding="utf-8-sig") as handle:
                    info_data = json.load(handle)
                record(info.name[: -len(".civitai.info")],
                       info_data.get("baseModel", ""), overwrite=False)
            except Exception:
                continue
        headers = cls._scan_lora_weight_headers(weight_index)
        if headers:
            try:
                registry = cls._build_base_hash_registry(headers, mapping)
                for name, (_path, fields) in headers.items():
                    base = registry.get(fields.get("new_hash") or "") or \
                        registry.get(fields.get("old_hash") or "")
                    if base:
                        record(name, base, overwrite=False)
            except Exception:
                pass
        try:
            needed = {key for key in referenced
                      if not (mapping.get(key) or "").strip().lower()
                      or (mapping.get(key) or "").strip().lower() == "unknown"}
            for key in needed:
                target = weight_index.get(key)
                guessed = ""
                if target is not None:
                    # テンソルキーの指紋まで見る重い判定は参照済み名前だけに行う
                    guessed = cls._classify_safetensor_header(target)
                if not guessed:
                    # 重みファイルが消えているLoRAは名前の手がかりが唯一の根拠になる
                    guessed = cls._base_model_from_name(key)
                if guessed:
                    mapping[key] = guessed
        except Exception:
            pass
        # 重みファイルが1個も無い名前 = LoRA削除後に放置された参照
        lost = cls._compute_lost_lora_names(referenced, weight_index)
        return mapping, lost

    def _repopulate_model_filter_combo(self) -> None:
        """固定リストにせず、LoRAフォルダに実在するBaseModelだけを列挙する。"""
        combo = self.model_filter_combo
        previous = combo.currentData() if combo.count() else None
        display: dict[str, str] = {}
        for base_model in self._lora_base_model_map.values():
            text = str(base_model or "").strip()
            if not text or text.lower() == "unknown":
                continue
            display.setdefault(text.lower(), text)
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("すべて", "")
            for key in sorted(display):
                combo.addItem(display[key], key)
            combo.addItem("LoRAなし", NOLORA_FILTER)
            combo.setCurrentIndex(max(combo.findData(previous or ""), 0))
        finally:
            combo.blockSignals(False)

    def _entry_base_models(self, entry: WildcardEntry) -> set[str]:
        """エントリが使うLoRAのBaseModel集合（cm-info.jsonの値を小文字化）。Unknownは除外。"""
        models: set[str] = set()
        for name in entry.lora_names or []:
            base_model = (self._lora_base_model_map.get(lora_name_key(name)) or "").strip().lower()
            if base_model and base_model != "unknown":
                models.add(base_model)
        return models

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
        folder_key = self._folder_item_key(item)
        is_fav_node = folder_key == "__favorites__" or folder_key.startswith("__favorites__/")
        menu = QMenu(self)
        open_action = menu.addAction("エクスプローラーで開く")
        new_action = None
        rename_action = None
        move_action = None
        delete_action = None
        if not is_fav_node:
            menu.addSeparator()
            new_action = menu.addAction("新規フォルダ...")
            if folder_key:
                rename_action = menu.addAction("名前を変更...")
                move_action = menu.addAction("移動...")
                delete_action = menu.addAction("このフォルダを削除")
        chosen = menu.exec(self.folder_tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == new_action:
            self._create_folder_from_tree(folder_key)
            return
        if chosen == rename_action:
            self._rename_folder_from_tree(folder_key)
            return
        if chosen == move_action:
            self._show_move_folder_dialog_for_folder(folder_key)
            return
        if chosen == delete_action:
            self._delete_folder_from_tree(folder_key)
            return
        if chosen != open_action:
            return
        base = Path(self.settings.library_root)
        # folder_key は POSIX 区切り（"/"）なので OS 非依存に合成する。
        # 以前は folder_key.replace("/", "\\") を使って Windows 専用になっていた。
        if folder_key == "__favorites__":
            target = base
        elif is_fav_node:
            sub = folder_key[len("__favorites__/"):]
            target = base / Path(*sub.split("/"))
        else:
            target = base if not folder_key else base / Path(*folder_key.split("/"))
        from .ui_utils import open_in_file_manager
        if target.exists():
            open_in_file_manager(target)
        else:
            self._show_info("フォルダ", f"フォルダが見つかりません:\n{target}")

    def _create_folder_from_tree(self, parent_key: str) -> None:
        name, ok = QInputDialog.getText(self, "新規フォルダ", "フォルダ名:")
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        try:
            new_key = self.repo.create_folder(self.settings, parent_key, name)
        except Exception as exc:
            self._show_error("フォルダ作成エラー", str(exc))
            return
        self._rebuild_folder_maps()
        self.current_folder_prefix = new_key
        self._rebuild_folder_tree()
        self.apply_filters()
        self.statusBar().showMessage(f"フォルダを作成しました: {new_key}", 4000)

    def _rename_folder_from_tree(self, folder_key: str) -> None:
        if not folder_key:
            return
        old_name = folder_key.split("/")[-1]
        new_name, ok = QInputDialog.getText(self, "フォルダ名の変更", "新しい名前:", text=old_name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_name:
            return
        parent = "/".join(folder_key.split("/")[:-1])
        new_key = f"{parent}/{new_name}" if parent else new_name
        try:
            self.repo.rename_folder(self.settings, folder_key, new_name)
        except Exception as exc:
            self._show_error("フォルダ名の変更エラー", str(exc))
            return
        self._rewrite_paths_after_folder_op(folder_key, new_key)
        if self.current_folder_prefix == folder_key:
            self.current_folder_prefix = new_key
        elif self.current_folder_prefix.startswith(folder_key + "/"):
            self.current_folder_prefix = new_key + self.current_folder_prefix[len(folder_key):]
        self._rebuild_folder_maps()
        self._rebuild_folder_tree()
        self.apply_filters()
        self.statusBar().showMessage(f"フォルダ名を変更しました: {folder_key} → {new_key}", 5000)

    def _show_move_folder_dialog_for_folder(self, src_key: str) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("移動先のフォルダを選択")
        dialog.setMinimumSize(350, 450)
        dialog.setStyleSheet(self.styleSheet())

        layout = QVBoxLayout(dialog)

        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setRootIsDecorated(True)
        tree.setIndentation(12)
        tree.setAnimated(False)
        tree.setExpandsOnDoubleClick(True)

        root = QTreeWidgetItem(["(all)"])
        root.setData(0, Qt.UserRole, "")
        root.setIcon(0, self.style().standardIcon(QStyle.SP_DirHomeIcon))
        tree.addTopLevelItem(root)

        self._populate_folder_tree_items(root, "", exclude_key=src_key)
        tree.expandItem(root)

        layout.addWidget(tree)

        button_layout = QHBoxLayout()
        move_btn = QPushButton("移動")
        cancel_btn = QPushButton("キャンセル")
        move_btn.setDefault(True)
        button_layout.addStretch()
        button_layout.addWidget(move_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)

        cancel_btn.clicked.connect(dialog.reject)
        move_btn.clicked.connect(dialog.accept)

        tree.setCurrentItem(root)

        if dialog.exec() != QDialog.Accepted:
            return

        selected = tree.currentItem()
        if not selected:
            return
        dest_folder = str(selected.data(0, Qt.UserRole) or "")
        self._move_folder_to(src_key, dest_folder)

    def _on_folder_tree_move_requested(self, src_key: str, dest_parent_key: str) -> None:
        self._move_folder_to(src_key, dest_parent_key)

    def _move_folder_to(self, src_key: str, dest_parent_key: str) -> None:
        if not src_key or src_key == "__favorites__" or src_key.startswith("__favorites__/"):
            return
        if dest_parent_key == "__favorites__" or dest_parent_key.startswith("__favorites__/"):
            self._show_info("フォルダ移動", "お気に入りツリーへは移動できません。")
            return
        if dest_parent_key == src_key or dest_parent_key.startswith(src_key + "/"):
            self._show_info("フォルダ移動", "自分自身または子フォルダへは移動できません。")
            return
        name = src_key.split("/")[-1]
        new_key = f"{dest_parent_key}/{name}" if dest_parent_key else name
        if new_key == src_key:
            return
        result = self._ask_yes_no(
            "フォルダ移動",
            f"以下のフォルダを移動しますか？\n\n{src_key}\n→ {new_key}\n\n配下の wildcard とサムネイルも一緒に移動されます。",
            default_button=QMessageBox.Yes,
        )
        if result != QMessageBox.Yes:
            return
        try:
            self.repo.move_folder(self.settings, src_key, dest_parent_key)
        except Exception as exc:
            self._show_error("フォルダ移動エラー", str(exc))
            return
        self._rewrite_paths_after_folder_op(src_key, new_key)
        if self.current_folder_prefix == src_key:
            self.current_folder_prefix = new_key
        elif self.current_folder_prefix.startswith(src_key + "/"):
            self.current_folder_prefix = new_key + self.current_folder_prefix[len(src_key):]
        self._rebuild_folder_maps()
        self._rebuild_folder_tree()
        self.apply_filters()
        self.statusBar().showMessage(f"フォルダを移動しました: {src_key} → {new_key}", 5000)

    def _rewrite_paths_after_folder_op(self, old_key: str, new_key: str) -> None:
        """フォルダ rename/move 後に、メモリ上のお気に入りとエントリのパスを書き換える。"""
        library_root = _safe_resolve_path(Path(self.settings.library_root))
        thumbnail_root = _safe_resolve_path(Path(self.settings.thumbnail_root))

        def _abs(root: Path, key: str) -> str:
            if not key:
                return str(root)
            return str(_safe_resolve_path(root / Path(*key.split("/"))))

        old_abs = _abs(library_root, old_key)
        new_abs = _abs(library_root, new_key)
        new_thumb_root_abs = _abs(thumbnail_root, new_key)
        old_rel_prefix = old_key + "/" if old_key else ""

        # お気に入り(abs_path)のプレフィックスを書き換える
        # ※ list_model.favorite_paths と同一オブジェクトを共有しているため、
        # セットを差し替えず in-place で更新する。
        rewritten_favs: set[str] = set()
        for path in self.favorite_paths:
            if path == old_abs or path.startswith(old_abs + os.sep):
                rewritten_favs.add(new_abs + path[len(old_abs):])
            else:
                rewritten_favs.add(path)
        if rewritten_favs != self.favorite_paths:
            self.favorite_paths.clear()
            self.favorite_paths.update(rewritten_favs)
            self._save_favorites()

        for entry in self.entries:
            if entry.folder != old_key and not (old_rel_prefix and entry.folder.startswith(old_rel_prefix)):
                continue
            rest = entry.rel_path[len(old_rel_prefix):] if old_rel_prefix else entry.rel_path
            new_rel = f"{new_key}/{rest}" if new_key else rest
            old_rel = entry.rel_path
            entry.rel_path = new_rel
            posix_parent = Path(new_rel).parent.as_posix()
            entry.folder = "" if posix_parent == "." else posix_parent
            entry.abs_path = str(_safe_resolve_path(Path(new_abs) / Path(*rest.split("/"))))
            if entry.thumbnail_path:
                entry.thumbnail_path = str(_safe_resolve_path(Path(new_thumb_root_abs) / Path(entry.thumbnail_path).name))
            entry.search_text = entry.search_text.replace(old_rel.lower(), new_rel.lower(), 1)
        self._rebuild_entry_indexes()

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

    def _rename_card(self, entry: WildcardEntry) -> None:
        old_name = entry.stem
        new_name, ok = QInputDialog.getText(
            self, "名前を変更", "新しい名前:", text=old_name
        )
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        new_name = new_name.strip()
        try:
            src_txt = Path(entry.abs_path)
            src_sidecar = sidecar_metadata_path(src_txt)
            src_thumb = Path(entry.thumbnail_path) if entry.thumbnail_path else None

            new_txt = src_txt.with_stem(new_name)
            new_sidecar = sidecar_metadata_path(new_txt)
            # Preserve the full suffix pattern (e.g. .preview.png)
            thumb_suffix = src_thumb.name[len(src_thumb.stem):]
            new_thumb = src_thumb.parent / f"{new_name}{thumb_suffix}" if src_thumb else None

            if new_txt.exists() and new_txt != src_txt:
                QMessageBox.warning(self, "名前変更エラー", f"同名のファイルが既に存在します:\n{new_txt.name}")
                return
            if new_sidecar.exists() and new_sidecar != src_sidecar:
                QMessageBox.warning(self, "名前変更エラー", f"同名のsidecarが既に存在します:\n{new_sidecar.name}")
                return
            if new_thumb and new_thumb.exists() and new_thumb != src_thumb:
                QMessageBox.warning(self, "名前変更エラー", f"同名のサムネイルが既に存在します:\n{new_thumb.name}")
                return

            src_txt.rename(new_txt)
            if src_sidecar.exists():
                src_sidecar.rename(new_sidecar)
            if src_thumb and src_thumb.exists():
                src_thumb.rename(new_thumb)

            self.repo.delete_entry(entry.abs_path)
            self.repo.refresh_entry(self.settings, new_txt)

            self.load_cached_entries()
            self.statusBar().showMessage(f"名前を '{new_name}' に変更しました。", 6000)
        except Exception as exc:
            QMessageBox.critical(self, "リネームエラー", str(exc))

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
                old_abs = entry.abs_path
                new_entry = self.repo.move_entry(self.settings, entry, dest_folder)
                # お気に入りは abs_path で管理しているため、移動後も追従させる
                if old_abs in self.favorite_paths:
                    self.favorite_paths.discard(old_abs)
                    self.favorite_paths.add(new_entry.abs_path)
                moved += 1
            except Exception as exc:
                errors.append(f"{entry.name}: {exc}")

        if errors:
            self._show_error("移動エラー", f"次のファイルの移動に失敗しました:\n\n{chr(10).join(errors)}")
        if moved:
            self._save_favorites()
        self.statusBar().showMessage(f"{moved} 件のカードを「{folder_display}」に移動しました。", 6000)
        self.load_cached_entries()

    def schedule_apply_filters(self) -> None:
        self.filter_timer.start()

    def _search_by_tag(self, tag: str) -> None:
        """タグ右クリックメニュー「検索」: 検索欄に語を入れてカード一覧を絞り込む"""
        self.search_edit.setText(tag)  # 変化があれば textChanged 経由で schedule される
        self.search_edit.setFocus()
        self.schedule_apply_filters()

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

        model_filter = ""
        combo = getattr(self, "model_filter_combo", None)
        if combo is not None:
            try:
                model_filter = str(combo.currentData() or "")
            except Exception:
                model_filter = ""

        def matches(entry: WildcardEntry) -> bool:
            if self.current_folder_prefix == "__favorites__":
                # お気に入りフォルダ：お気に入り登録済みのエントリのみ表示
                if entry.abs_path not in self.favorite_paths:
                    return False
            elif self.current_folder_prefix.startswith("__favorites__/"):
                # お気に入りツリー配下のサブフォルダ：お気に入り かつ 元フォルダ一致
                if entry.abs_path not in self.favorite_paths:
                    return False
                fav_sub = self.current_folder_prefix[len("__favorites__/"):]
                if entry.folder != fav_sub and not entry.folder.startswith(f"{fav_sub}/"):
                    return False
            elif self.current_folder_prefix:
                if entry.folder != self.current_folder_prefix and not entry.folder.startswith(f"{self.current_folder_prefix}/"):
                    return False
            if selected_loras:
                if not selected_loras.issubset(set(entry.lora_names)):
                    return False
            if model_filter == NOLORA_FILTER:
                if entry.lora_names:
                    return False
            elif model_filter:
                # 単一BaseModel一致。混在エントリはどのモデルにも含まれない
                if self._entry_base_models(entry) != {model_filter}:
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
        self._apply_lora_list_height(0)
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
        # 2枚以上持つカードは「めくる」方に使い、1枚だけのカードは従来通りコピー。
        # コピー手順を失わないよう、多画像カードでも Ctrl+ダブルクリック＝コピー。
        if not (QApplication.queryKeyboardModifiers() & Qt.ControlModifier):
            page = self.list_model.flip_to_next(entry)
            if page is not None:
                self._update_preview_pixmap()
                total = self.list_model.variant_count(entry)
                self.show_toast(f"{page}/{total}枚目を表示")
                return
        # トーストは copy_all_text 側が出す（LoRA欠損時は警告に差し替わる）
        self.copy_all_text(True)

    def _show_entry(self, entry: WildcardEntry) -> None:
        self._set_edit_mode(False)
        self._path_label_text = entry.rel_path
        self._refresh_path_label()
        self.lora_list.clear()
        for name in entry.lora_names:
            self.lora_list.addItem(name)
        self._apply_lora_list_height(len(entry.lora_names))
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

        selected_indexes = self.card_view.selectionModel().selectedIndexes()
        selected_entries = [self.list_model.entry_at(idx) for idx in selected_indexes]
        selected_entries = [e for e in selected_entries if e is not None]
        if not selected_entries:
            selected_entries = [entry]

        # お気に入りトグル（メニュー最上部）
        is_fav = entry.abs_path in self.favorite_paths
        fav_label = "★ お気に入りを解除" if is_fav else "☆ お気に入りに追加"

        menu = QMenu(self)
        fav_action = menu.addAction(fav_label)
        menu.addSeparator()
        thumb_action = menu.addAction("サムネ生成")
        assign_thumb_action = menu.addAction("サムネイルに画像を指定...")
        add_thumb_action = menu.addAction("サムネイルに画像を追加...")
        # 複数枚カードは「どの枚を消すか」を menu 上で直接選べるようにする。
        # 表示中1枚だけを消す方式だと、目的の枚へめくり当てる手順が隠れる。
        gallery_pages = self.list_model.gallery_paths(entry)
        del_thumb_action = None
        if len(gallery_pages) >= 2:
            delete_menu = menu.addMenu("サムネイルを削除...")
            shown_path = self.list_model.display_path_for(entry)
            for page_pos, page_path in enumerate(gallery_pages):
                mark = "（表示中）" if thumbnail_paths_equal(page_path, shown_path) else ""
                page_action = delete_menu.addAction(f"{page_pos + 1}枚目: {Path(page_path).name}{mark}")
                page_action.triggered.connect(
                    lambda checked=False, pos=page_pos: self.delete_thumbnail_image(entry, page_index=pos))
        else:
            del_thumb_action = menu.addAction("このサムネイルを削除...")
        del_all_thumb_action = menu.addAction("サムネイルをすべて削除...")
        random_action = menu.addAction("固定プロンプトに入れる")
        menu.addSeparator()
        copy_all_action = menu.addAction("LoRAありでコピー")
        copy_all_without_action = menu.addAction("LoRAなしでコピー")
        if entry.lora_names:
            search_menu = menu.addMenu("LoRAで絞り込み")
            for name in entry.lora_names:
                action = search_menu.addAction(name)
                action.triggered.connect(lambda checked=False, n=name: self._check_lora_in_cloud(n))
        menu.addSeparator()
        explorer_action = menu.addAction("エクスプローラーで開く")
        thumb_folder_action = menu.addAction("サムネフォルダを開く")
        menu.addSeparator()
        rename_action = menu.addAction("名前を変更...")
        move_action = menu.addAction("移動...")
        menu.addSeparator()
        delete_action = menu.addAction("削除")
        action = menu.exec(self.card_view.viewport().mapToGlobal(pos))
        if action == fav_action:
            self._toggle_favorite(entry)
        elif action == thumb_action:
            self.generate_thumbnail_for_current()
        elif action == assign_thumb_action:
            self.assign_thumbnail_from_image(entry)
        elif action == add_thumb_action:
            self.add_thumbnail_image(entry)
        elif del_thumb_action is not None and action == del_thumb_action:
            self.delete_thumbnail_image(entry)
        elif action == del_all_thumb_action:
            self.delete_thumbnail_image(entry, all_pages=True)
        elif action == random_action:
            self.open_random_generation(entry)
        elif action == copy_all_action:
            self.copy_all_text(True)
        elif action == copy_all_without_action:
            self.copy_all_text(False)
        elif action == explorer_action:
            self._open_in_explorer(entry)
        elif action == thumb_folder_action:
            self._open_thumbnail_folder(entry)
        elif action == rename_action:
            self._rename_card(entry)
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
        dialog.setStyleSheet(self.styleSheet())

        layout = QVBoxLayout(dialog)

        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setRootIsDecorated(True)
        tree.setIndentation(12)
        tree.setAnimated(False)
        tree.setExpandsOnDoubleClick(True)

        root = QTreeWidgetItem(["(all)"])
        root.setData(0, Qt.UserRole, "")
        root.setIcon(0, self.style().standardIcon(QStyle.SP_DirHomeIcon))
        tree.addTopLevelItem(root)

        self._populate_folder_tree_items(root, "")
        tree.expandItem(root)

        layout.addWidget(tree)

        button_layout = QHBoxLayout()
        move_btn = QPushButton("移動")
        cancel_btn = QPushButton("キャンセル")
        move_btn.setDefault(True)
        button_layout.addStretch()
        button_layout.addWidget(move_btn)
        button_layout.addWidget(cancel_btn)
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

    def _populate_folder_tree_items(self, item: QTreeWidgetItem, parent_key: str, exclude_key: str | None = None) -> None:
        children = self.folder_children_map.get(parent_key, set())
        for child_name in sorted(children, key=natural_sort_key):
            child_key = child_name if not parent_key else f"{parent_key}/{child_name}"
            # 移動元サブツリーは移動先候補から除外する（自分自身・子へは移動不可のため）
            if exclude_key and (child_key == exclude_key or child_key.startswith(exclude_key + "/")):
                continue
            child_item = QTreeWidgetItem([child_name])
            child_item.setData(0, Qt.UserRole, child_key)
            child_item.setIcon(0, self.style().standardIcon(QStyle.SP_DirIcon))
            if self.folder_children_map.get(child_key):
                child_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            item.addChild(child_item)
            self._populate_folder_tree_items(child_item, child_key, exclude_key=exclude_key)

    def _show_lora_context_menu(self, pos) -> None:
        item = self.lora_list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        filter_action = menu.addAction("この LoRA で検索")
        action = menu.exec(self.lora_list.viewport().mapToGlobal(pos))
        if action == filter_action:
            self._check_lora_in_cloud(item.text())

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
            ok = open_in_file_manager(thumb_path, select=True)
            if not ok:
                self._show_info("サムネフォルダ", f"エクスプローラーの起動に失敗しました:\n{thumb_path}")
        else:
            parent = thumb_path.parent
            if parent.exists():
                ok = open_in_file_manager(parent)
                if not ok:
                    self._show_info("サムネフォルダ", f"エクスプローラーの起動に失敗しました:\n{parent}")
            else:
                self._show_info("サムネフォルダ", f"サムネイルフォルダが見つかりません:\n{thumb_path}\n\n親フォルダも存在しません:\n{parent}")

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
        """エントリを更新して UI に反映する。サムネイル生成後に左カードが即座に更新される。

        【方針B】パス正規化の不一致で row_by_path が miss する問題を解消するため、
        list_model 側は rel_path をキーに持つ。ここでも updated.rel_path を使って
        行を検索・キャッシュ更新・set_icon_directly を呼ぶ。
        """
        # 1) 自前で持っている entries / filtered_entries を更新
        for index, entry in enumerate(self.entries):
            if entry.abs_path == updated.abs_path or entry.rel_path == updated.rel_path:
                self.entries[index] = updated
                break
        self.current_entry = updated
        self._set_edit_mode(False)
        self._rebuild_entry_indexes()

        self.filter_timer.stop()

        # 2) list_model 側のエントリを更新（rel_path で検索）
        row_pre = self.list_model.row_by_path.get(updated.rel_path)
        if row_pre is not None and row_pre < len(self.list_model.entries):
            self.list_model.entries[row_pre] = updated

        # 3) キャッシュをクリア（保護パス機構により直前の set_icon_directly は保護される）
        self.list_model.clear_cache_for_path(updated.rel_path)

        # 4) 生成直後のサムネイルを同期的に読み込んでキャッシュへ格納。
        #    非同期パイプラインを待たず即座にカードが新しい画像を表示する。
        icon_loaded = False
        if updated.thumbnail_path and updated.has_thumbnail:
            try:
                thumb_file = Path(updated.thumbnail_path)
                if thumb_file.exists():
                    raw_bytes = thumb_file.read_bytes()
                    raw_image = QImage()
                    raw_image.loadFromData(raw_bytes)
                    if not raw_image.isNull():
                        thumb_size = max(80, self.list_model.thumbnail_size)
                        scaled = cover_crop(raw_image, thumb_size,
                                            int(thumb_size * theme.CARD_ICON_RATIO))
                        if not scaled.isNull():
                            # 【方針B】rel_path を渡す
                            self.list_model.set_icon_directly(updated.rel_path, scaled)
                            icon_loaded = True
            except Exception:
                log.debug("Failed to load thumbnail for %s", updated.rel_path, exc_info=True)

        self.apply_filters()
        if not icon_loaded:
            row = self.list_model.row_by_path.get(updated.rel_path)
            if row is not None and row < len(self.list_model.entries):
                idx = self.list_model.index(row, 0)
                self.list_model.dataChanged.emit(idx, idx, [Qt.DecorationRole, Qt.DisplayRole, Qt.SizeHintRole])

        self.card_view.viewport().update()
        self._show_entry(updated)

    def _prepare_copy_text(self, text: str, include_lora: bool) -> str:
        return text.strip() if include_lora else strip_lora_tags(text)

    def copy_all_text(self, include_lora: bool | None = None) -> list[str]:
        """全文をクリップボードへコピーし、実在しないLoRA名の一覧を返す。

        戻り値は `include_lora` が実効 True のときだけ中身が入る（LoRAなしコピーでは
        `<lora:...>` を書く必要が無いので判定しない）。コピー自体は実行済みで、
        見つからない名前があっても止めない ―― 生ファイルはNeo/Forge側で
        無視されるだけなので、警告して判断はユーザーに残す。
        """
        if not self.current_entry:
            return []
        include = self.settings.include_lora_on_copy if include_lora is None else include_lora
        text = self._prepare_copy_text(self.editor.toPlainText(), include)
        QApplication.clipboard().setText(text, QClipboard.Clipboard)
        missing = self._missing_copy_lora_names(text) if include else []
        if missing:
            shown = "、".join(missing[:3]) + ("…" if len(missing) > 3 else "")
            self.statusBar().showMessage(
                f"コピーしました（LoRAあり）／実在しないLoRA {len(missing)}件: {shown}"
                "　※「LoRAなし」でコピーすると安全です", 8000)
            # 名前を自力で読む時間が必要なので、確認トーストより長めに出す
            self.show_toast(f"LoRAが見つかりません（削除済み）\n{shown}", 5000)
        else:
            self.statusBar().showMessage(f"全文をコピーしました（{'LoRAあり' if include else 'LoRAなし'}）", 3000)
            self.show_toast("Copied" if include else "Copied（LoRAなし）")
        return missing
    # -------------------------------------------------------------------------
    # お気に入り機能
    # -------------------------------------------------------------------------

    def _load_favorites(self) -> set[str]:
        """favorites.json からお気に入りパスのセットを読み込む。"""
        try:
            if self.favorites_file.exists():
                with self.favorites_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return set(data)
        except Exception as e:
            log.warning("お気に入りの読み込みに失敗しました: %s", e)
        return set()

    def _save_favorites(self) -> None:
        """お気に入りパスのセットを favorites.json に保存する。"""
        try:
            self.favorites_file.parent.mkdir(parents=True, exist_ok=True)
            with self.favorites_file.open("w", encoding="utf-8") as f:
                json.dump(sorted(self.favorite_paths), f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("お気に入りの保存に失敗しました: %s", e)

    def _toggle_favorite(self, entry: WildcardEntry) -> None:
        """エントリのお気に入り状態をトグルし、ビューを更新する。"""
        if entry.abs_path in self.favorite_paths:
            self.favorite_paths.discard(entry.abs_path)
            msg = f"お気に入りを解除しました: {entry.stem}"
        else:
            self.favorite_paths.add(entry.abs_path)
            msg = f"お気に入りに追加しました: {entry.stem}"
        self._save_favorites()
        # カードの星バッジを即座に再描画
        for row, e in enumerate(self.list_model.entries):
            if e.abs_path == entry.abs_path:
                idx = self.list_model.index(row, 0)
                self.list_model.dataChanged.emit(idx, idx, [Qt.DecorationRole])
                break
        # お気に入りフォルダを表示中の場合はフィルターを再適用してカードを更新
        if self.current_folder_prefix == "__favorites__":
            self.apply_filters()
        # ★ お気に入り 配下の階層（元フォルダのミラー）を更新
        self._refresh_favorites_tree()
        self.statusBar().showMessage(msg, 3000)

    def show_toast(self, message: str, ms: int | None = None) -> None:
        """トースト表示。既定時間は ToastPopup.show_for の既定（2.6秒）に従う。

        読む量が要る警告（削除済みLoRA名の一覧など）だけ、呼び出し側で ms を
        明示して延ばす。
        """
        if self.toast_popup is not None:
            self.toast_popup.close()
        self.toast_popup = ToastPopup(self, message)
        if ms is None:
            self.toast_popup.show_for()
        else:
            self.toast_popup.show_for(ms)

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
        # 現在のカードのサムネイルを即時更新
        self._replace_entry(updated)
        done = self._thumbnail_batch_total - len(self._thumbnail_batch_queue)
        if self._thumbnail_batch_total > 1:
            self.statusBar().showMessage(
                f"サムネイル生成中... ({done}/{self._thumbnail_batch_total}) 完了: {filename}",
                3000,
            )
        else:
            self.statusBar().showMessage(f"サムネイルを生成しました: {filename}", 5000)
        self._cleanup_thumbnail_generation_worker()
        # バッチキューに残りがあれば次を開始
        if self._thumbnail_batch_queue and not self._thumbnail_batch_cancelled:
            QTimer.singleShot(100, self._start_next_thumbnail_in_batch)
        elif self._thumbnail_batch_total > 1 or self._thumbnail_batch_cancelled:
            self._finish_thumbnail_batch()

    def _on_thumbnail_generation_failed(self, message: str) -> None:
        self._stop_thumbnail_loading_feedback()
        # バッチ中はエラーダイアログをスキップして次へ進む（連続ダイアログ防止）
        if self._thumbnail_batch_total > 1:
            done = self._thumbnail_batch_total - len(self._thumbnail_batch_queue) - 1
            self.statusBar().showMessage(
                f"サムネイル生成失敗 ({done+1}/{self._thumbnail_batch_total}): {message}",
                5000,
            )
        else:
            self._show_error("サムネイル生成エラー", message)
            if self.current_entry is not None:
                self._show_entry(self.current_entry)
        self._cleanup_thumbnail_generation_worker()
        # バッチキューに残りがあれば次を開始（キャンセル時は停止）
        if self._thumbnail_batch_queue and not self._thumbnail_batch_cancelled:
            QTimer.singleShot(100, self._start_next_thumbnail_in_batch)
        elif self._thumbnail_batch_total > 1 or self._thumbnail_batch_cancelled:
            self._finish_thumbnail_batch()

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

    def _sync_gallery_source(self) -> None:
        """list_model に「2枚目以降」の走査元を配線する（設定変更でも再呼び出し）。"""
        root = _safe_resolve_path(Path(self.settings.thumbnail_root))
        self.list_model.thumbnail_root = root

        def listing(folder: str) -> list[str]:
            target = root / folder if folder else root
            try:
                with os.scandir(target) as entries:
                    return [de.name for de in entries if de.is_file()]
            except OSError:
                return []

        self.list_model.gallery_listing_provider = listing

    def _thumbnail_source_root(self) -> Path:
        return _safe_resolve_path(Path(self.settings.thumbnail_root))

    def _pick_image_path(self, title: str, entry: WildcardEntry) -> str:
        shown = self.list_model.display_path_for(entry)
        start_dir = str(Path(shown).parent) if shown else ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            title,
            start_dir,
            "画像 (*.png *.jpg *.jpeg *.webp *.bmp);;すべて (*)",
        )
        return path

    def _write_thumbnail_image(self, source: str, dest: Path) -> bool:
        """画像をサムネイル保存先（WEBP q82）へ書き出す。生成時と同じ形式・同じ上限。"""
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(source) as src:
                image = src.convert("RGB")
            longest = max(image.size)
            if longest > THUMBNAIL_ASSIGN_MAX:
                scale = THUMBNAIL_ASSIGN_MAX / longest
                image = image.resize(
                    (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                    Image.LANCZOS,
                )
            image.save(dest, "WEBP", quality=82, method=6)
            return True
        except Exception as exc:
            self._show_error("サムネイル登録エラー", f"{source}\n\n{exc}")
            return False

    def _refresh_entry_after_thumb_change(self, entry: WildcardEntry) -> WildcardEntry | None:
        try:
            updated = self.repo.refresh_entry(self.settings, Path(entry.abs_path))
        except Exception as exc:
            self._show_error("サムネイル反映エラー", f"ファイル操作は完了しましたが、DB反映に失敗しました。\n{exc}")
            return None
        self._replace_entry(updated)
        return updated

    def assign_thumbnail_from_current(self) -> None:
        self.assign_thumbnail_from_image(self.current_entry)

    def add_thumbnail_from_current(self) -> None:
        self.add_thumbnail_image(self.current_entry)

    def delete_current_thumbnail(self) -> None:
        self.delete_thumbnail_image(self.current_entry)

    def assign_thumbnail_from_image(self, entry: WildcardEntry | None) -> None:
        """既存画像をそのカードの1枚目のサムネイルとして登録する（API生成を使わない差し替え）。

        保存先は生成と同じ ``{thumbnail_root}/{rel.parent}/{stem}.preview.webp``
        （`ensure_thumbnail_destination`）。ここが最優先候補になるので、
        生成済み画像を後から差し替えた場合も表示が負けない。
        """
        if entry is None:
            self._show_info("サムネイル登録", "カードを選択してください。")
            return
        path = self._pick_image_path("サムネイルに使う画像を選択", entry)
        if not path:
            return
        dest = ensure_thumbnail_destination(self._thumbnail_source_root(), entry.rel_path)
        if dest.exists() and self._ask_yes_no(
            "サムネイル登録",
            f"既存の1枚目を上書きします。\n\n{dest.name}",
            default_button=QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        if not self._write_thumbnail_image(path, dest):
            return
        self.list_model.reset_variant(entry.rel_path)
        if self._refresh_entry_after_thumb_change(entry) is not None:
            self.statusBar().showMessage(f"1枚目を登録しました: {dest.name}", 5000)

    def add_thumbnail_image(self, entry: WildcardEntry | None) -> None:
        """2枚目以降の画像を追加する（ダブルクリックでめくれるカードになる）。"""
        if entry is None:
            self._show_info("サムネイル追加", "カードを選択してください。")
            return
        pages = self.list_model.gallery_paths(entry)
        path = self._pick_image_path("追加する画像を選択", entry)
        if not path:
            return
        if not pages:
            # 1枚目すら無いカードでは、g2 に置いても「2枚目のない1枚目」になる。
            # そのまま1枚目として登録する（枚数の穴を埋めない）。
            dest = ensure_thumbnail_destination(self._thumbnail_source_root(), entry.rel_path)
        else:
            dest = next_gallery_slot(self._thumbnail_source_root(), entry.rel_path)
        if not self._write_thumbnail_image(path, dest):
            return
        self.list_model.invalidate_gallery(entry.rel_path)
        updated = self._refresh_entry_after_thumb_change(entry)
        if updated is not None:
            shown = self.list_model.variant_count(updated)
            self.show_toast(f"{shown}枚目を追加しました（ダブルクリックでめくれます）")
            self.statusBar().showMessage(f"サムネイルを追加しました: {dest.name}", 5000)

    def delete_thumbnail_image(self, entry: WildcardEntry | None, *, all_pages: bool = False,
                              page_index: int | None = None) -> None:
        """カードは消さずにサムネイル画像だけゴミ箱へ移動する。

        既定は「表示中の1枚」。複数枚あるカードで目的の枚を消したいときは
        `page_index`（0起点）で枚を直接指定する — めくって合わせなくても
        右クリックから選べるほうが意図とズレない（消す対象を間違えると
        手元に戻すのにごみ箱を開けなければならない）。
        """
        if entry is None:
            self._show_info("サムネイル削除", "カードを選択してください。")
            return
        pages = self.list_model.gallery_paths(entry)
        if not pages:
            self._show_info("サムネイル削除", "このカードにサムネイルはありません。")
            return
        if all_pages:
            picked = list(enumerate(pages))
        elif page_index is not None:
            if not 0 <= page_index < len(pages):
                self._show_info("サムネイル削除",
                                f"{page_index + 1}枚目は存在しません（現在 {len(pages)} 枚）。")
                return
            picked = [(page_index, pages[page_index])]
        else:
            # 表示中パスは DB 由来（`H:\`）で pages（`h:\`）と大小文字がズレるため、
            # 枚目索引は `pages.index()` ではなく実パス同一性で引き当てる。
            shown = self.list_model.display_path_for(entry) or pages[0]
            pos = next((i for i, p in enumerate(pages) if thumbnail_paths_equal(p, shown)), 0)
            picked = [(pos, pages[pos])]
        targets = [Path(p) for _i, p in picked]
        names = "\n".join(f"  {i + 1}枚目: {Path(p).name}" for i, p in picked[:6])
        if len(picked) > 6:
            names += f"\n  ... 他 {len(picked) - 6} 件"
        if self._ask_yes_no(
            "サムネイル削除",
            f"カードは残し、サムネイル画像だけをゴミ箱に移動します。\n\n{names}",
            default_button=QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        moved = [p for p in targets if self._move_to_trash(p)]
        if not moved:
            self._show_error("サムネイル削除", "ゴミ箱に移動できませんでした。パス権限をご確認ください。")
            return
        # 削除で枚番号がずれる（g3 が残って g2 が消える等）ので、
        # カード側の表示枚目は 1枚目へ戻して組み直す
        self.list_model.forget_thumbnail(entry.rel_path)
        updated = self._refresh_entry_after_thumb_change(entry)
        if updated is not None:
            rest = self.list_model.gallery_paths(updated)
            self.show_toast(f"{len(moved)}枚のサムネイルを削除しました" + (f"（残り {len(rest)} 枚）" if rest else ""))

    def generate_thumbnail_for_current(self) -> None:
        """選択中のカード（単数または複数）のサムネイルを生成する。

        複数カードが選択されている場合はキューに積んで順次生成する。
        sd-webui API は単一画像生成しかサポートしないため、アプリ側で
        キューイングして1枚ずつ処理する。
        """
        if self.thumbnail_generation_thread is not None and self.thumbnail_generation_thread.isRunning():
            self._show_info("サムネ生成", "別のサムネ生成が実行中です。")
            return

        # 選択中の全エントリを取得
        indexes = self.card_view.selectionModel().selectedIndexes()
        selected_entries: list[WildcardEntry] = []
        if indexes:
            seen_paths: set[str] = set()
            for idx in indexes:
                e = self.list_model.entry_at(idx)
                if e is not None and e.abs_path not in seen_paths:
                    seen_paths.add(e.abs_path)
                    selected_entries.append(e)

        # 選択がない場合は現在のエントリのみ
        if not selected_entries:
            if not self.current_entry:
                self._show_info("サムネ生成", "先に wildcard を選択してください。")
                return
            selected_entries = [self.current_entry]

        # ランダムワイルドカード使用可否は1回だけ尋ねる
        use_random = self._ask_use_random_wildcard()

        # バッチ状態を初期化
        self._thumbnail_batch_queue = list(selected_entries)
        self._thumbnail_batch_total = len(selected_entries)
        self._thumbnail_batch_use_random = use_random
        self._thumbnail_batch_cancelled = False

        # 最初の1枚を開始
        self._start_next_thumbnail_in_batch()

    def _start_next_thumbnail_in_batch(self) -> None:
        """バッチキューから次の1枚を取り出してサムネイル生成を開始する。"""
        # キャンセルチェック
        if self._thumbnail_batch_cancelled:
            self._finish_thumbnail_batch()
            return

        # キューが空ならバッチ終了
        if not self._thumbnail_batch_queue:
            self._finish_thumbnail_batch()
            return

        # まだ生成スレッドが動いている場合は何もしない（二重起動防止）
        if self.thumbnail_generation_thread is not None and self.thumbnail_generation_thread.isRunning():
            return

        # 次のエントリを取り出す
        entry = self._thumbnail_batch_queue.pop(0)
        # 既に current_entry になっているエントリでなくても生成可能
        # （右パネルの表示は現在の選択を維持するため、current_entry は更新しない）

        # 進捗表示
        done = self._thumbnail_batch_total - len(self._thumbnail_batch_queue)
        if self._thumbnail_batch_total > 1:
            msg = f"Generating thumbnail... ({done}/{self._thumbnail_batch_total})\n{entry.stem}"
        else:
            msg = "Generating thumbnail...\nPreparing image..."
        self._start_thumbnail_loading_feedback(msg)
        self.statusBar().showMessage(
            f"Generating thumbnail... ({done}/{self._thumbnail_batch_total})" if self._thumbnail_batch_total > 1 else "Generating thumbnail...",
            0,
        )

        worker_settings = copy.copy(self.settings)
        worker_settings.thumbnail_random_wildcard = self._thumbnail_batch_use_random
        self.thumbnail_generation_thread = QThread(self)
        self.thumbnail_generation_worker = SingleThumbnailWorker(self.db_path, worker_settings, entry)
        self.thumbnail_generation_worker.moveToThread(self.thumbnail_generation_thread)
        self.thumbnail_generation_thread.started.connect(self.thumbnail_generation_worker.run)
        self.thumbnail_generation_worker.finished.connect(self._on_thumbnail_generation_finished)
        self.thumbnail_generation_worker.failed.connect(self._on_thumbnail_generation_failed)
        self.thumbnail_generation_thread.start()

    def _finish_thumbnail_batch(self) -> None:
        """バッチ生成の終了処理。キューと状態をクリアする。"""
        self._stop_thumbnail_loading_feedback()
        total = self._thumbnail_batch_total
        cancelled = self._thumbnail_batch_cancelled
        self._thumbnail_batch_queue = []
        self._thumbnail_batch_total = 0
        self._thumbnail_batch_use_random = False
        self._thumbnail_batch_cancelled = False
        if total > 1:
            if cancelled:
                self.statusBar().showMessage("サムネイル生成を中断しました", 5000)
            else:
                self.statusBar().showMessage(f"{total} 件のサムネイルを生成しました", 5000)

    def _cancel_thumbnail_batch(self) -> None:
        """バッチ生成をキャンセルする（現在の生成の完了後に停止）。"""
        self._thumbnail_batch_cancelled = True
        self._thumbnail_batch_queue = []

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
            if existing.abs_path == updated.abs_path or existing.rel_path == updated.rel_path:
                self.entries[pos] = updated
                break
        for pos, existing in enumerate(self.filtered_entries):
            if existing.abs_path == updated.abs_path or existing.rel_path == updated.rel_path:
                self.filtered_entries[pos] = updated
                break
        # 【方針B】rel_path をキーにキャッシュ操作
        self.list_model.clear_cache_for_path(updated.rel_path)
        row = self.list_model.row_by_path.get(updated.rel_path)
        if row is not None and row < len(self.list_model.entries):
            self.list_model.entries[row] = updated
            idx = self.list_model.index(row, 0)
            self.list_model.dataChanged.emit(idx, idx, [Qt.DecorationRole, Qt.DisplayRole, Qt.SizeHintRole])
            if updated.thumbnail_path:
                try:
                    thumb_file = Path(updated.thumbnail_path)
                    if thumb_file.exists():
                        raw_bytes = thumb_file.read_bytes()
                        raw_image = QImage()
                        raw_image.loadFromData(raw_bytes)
                        if not raw_image.isNull():
                            thumb_size = max(80, self.list_model.thumbnail_size)
                            scaled = cover_crop(raw_image, thumb_size,
                                                int(thumb_size * theme.CARD_ICON_RATIO))
                            if not scaled.isNull():
                                # 【方針B】rel_path を渡す
                                self.list_model.set_icon_directly(updated.rel_path, scaled)
                except Exception:
                    log.debug("Failed to load thumbnail for %s", updated.rel_path, exc_info=True)
        self.card_view.viewport().update()
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
            log.debug("Failed to cleanup preview thumbnails", exc_info=True)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._confirm_discard_or_save_if_needed():
            event.ignore()
            return
        # サムネイルバッチ生成をキャンセル
        self._cancel_thumbnail_batch()
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
        self.settings.left_split_sizes = self.left_split.sizes()
        # "__favorites__" 系は仮想フォルダなので保存しない（再起動時に復元不能）
        self.settings.last_folder = "" if self.current_folder_prefix.startswith("__favorites__") else self.current_folder_prefix
        self.ui_state_store.save(self.settings)
        super().closeEvent(event)

