"""SettingsDialog extracted from main_window.py."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .api import ThumbnailApiClient
from .models import AppSettings
from .repository import WildcardRepository
from .ui_utils import setup_japanese_context_menu as _setup_japanese_context_menu
from .widgets import NoWheelComboBox

log = logging.getLogger(__name__)

# UI constants (must match main_window.py values)
UI_RADIUS = 10
UI_CARD_MARGIN_TOP = 18
UI_CARD_MARGIN_X = 12
UI_CARD_PADDING_X = 14
UI_CARD_PADDING_Y = 12
UI_CONTROL_HEIGHT = 32
UI_BUTTON_HEIGHT = 30
UI_BUTTON_PADDING_X = 14
UI_SECTION_SPACING = 16


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
            QPushButton:pressed {{
                background: #1e4a9a;
                border-color: #1e4a9a;
            }}
            """
        )

        self.library_root = self._path_row(settings.library_root)
        self.thumbnail_root = self._path_row(settings.thumbnail_root)
        self.thumbnail_root_mgmt = self._path_row(settings.thumbnail_root)
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
