from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QEvent, QObject, Signal, Slot, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
    QPlainTextEdit, QRadioButton, QScrollArea, QSplitter, QTabWidget,
    QVBoxLayout, QWidget, QDialog, QMenu,
    QSizePolicy,
)

from .api import ThumbnailApiClient
from .models import AppSettings, WildcardEntry
from .repository import WildcardRepository
from .single_thumbnail_worker import SingleThumbnailWorker
from .tag_editor import FlowLayout, TagEditorWidget as _BaseTagEditorWidget
from .ui_utils import setup_japanese_context_menu as _setup_japanese_context_menu

# サムネイル生成時の一時プレビューディレクトリ名
PREVIEW_THUMB_SUBDIR = "_preview_thumbs"


class TagEditorWidget(_BaseTagEditorWidget):
    """タグクリック時にシグナルを発行するTagEditorWidget"""
    tagClicked = Signal(str)

    def _on_chip_clicked(self, tag: str) -> None:
        self.tagClicked.emit(tag)
        super()._on_chip_clicked(tag)

    def _append_tag(self, tag: str) -> None:
        super()._append_tag(tag)
        if self._chips:
            self._chips[-1].setToolTip("シングルクリックでコピー")


class _FolderPicker(QWidget):
    """パス入力 + 参照ボタンの複合ウィジェット"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._edit = QLineEdit()
        self._edit.setFixedHeight(34)
        layout.addWidget(self._edit, 1)
        self._btn = QPushButton("参照")
        self._btn.setFixedHeight(34)
        self._btn.setMinimumWidth(50)
        layout.addWidget(self._btn)
        self._btn.clicked.connect(self._browse)
        _setup_japanese_context_menu(self._edit)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "フォルダを選択", self._edit.text())
        if path:
            self._edit.setText(path)

    def currentText(self) -> str:
        return self._edit.text().strip()

    def setText(self, text: str):
        self._edit.setText(text)

    def text(self) -> str:
        return self._edit.text().strip()

    def setFixedHeight(self, h: int):
        self._edit.setFixedHeight(h)
        self._btn.setFixedHeight(h)


class NoDropPlainTextEdit(QPlainTextEdit):
    """ファイルドロップを自分で処理せずダイアログへ委譲する"""
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.ignore()   # ← 親（ダイアログ）に伝播させる
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.ignore()
        else:
            super().dragMoveEvent(ev)

    def dropEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.ignore()   # ← 親（ダイアログ）の dropEvent へ
        else:
            super().dropEvent(ev)


class LoraCardWidget(QWidget):
    """未作成LoRAサムネイルカード"""

    CARD_W = 130
    CARD_H = 160
    THUMB_SIZE = 120

    def __init__(self, item_data: dict, parent=None):
        super().__init__(parent)
        self.item_data = item_data
        self.setFixedSize(self.CARD_W, self.CARD_H)
        self.setCursor(Qt.PointingHandCursor)
        self._hovered = False

    def enterEvent(self, ev):
        self._hovered = True
        self.update()

    def leaveEvent(self, ev):
        self._hovered = False
        self.update()

    def paintEvent(self, ev):
        from PySide6.QtGui import QPainter, QColor, QPen, QFont
        from PySide6.QtCore import QRect
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # save() なしで restore() を呼ぶと Qt が警告を出すため、
        # 対になる save() を先に呼ぶ（B3 修正）。
        p.save()

        bg = QColor("#2a3a50") if self._hovered else QColor("#171b21")
        p.setBrush(bg)
        p.setPen(QPen(QColor("#2a3440"), 1))
        p.drawRoundedRect(0, 0, self.CARD_W, self.CARD_H, 8, 8)

        thumb_rect = QRect(5, 5, self.THUMB_SIZE, self.THUMB_SIZE)
        preview = self.item_data.get("preview")
        if preview and Path(preview).exists():
            px = QPixmap(preview).scaled(
                self.THUMB_SIZE, self.THUMB_SIZE,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            x = thumb_rect.x() + (self.THUMB_SIZE - px.width()) // 2
            y = thumb_rect.y() + (self.THUMB_SIZE - px.height()) // 2
            p.drawPixmap(x, y, px)
        else:
            p.setBrush(QColor("#1a2030"))
            p.setPen(QPen(QColor("#2a3440"), 1))
            p.drawRoundedRect(thumb_rect, 6, 6)
            p.setPen(QColor("#6a7a8a"))
            p.setFont(QFont("Yu Gothic UI", 9))
            p.drawText(thumb_rect, Qt.AlignCenter, "No img")

        name = self.item_data.get("name", "")
        p.setPen(QColor("#edf2f7"))
        p.setFont(QFont("Yu Gothic UI", 9))
        name_rect = QRect(0, self.THUMB_SIZE + 6, self.CARD_W, 20)
        elided = p.fontMetrics().elidedText(name, Qt.ElideRight, self.CARD_W - 8)
        p.drawText(name_rect, Qt.AlignHCenter | Qt.AlignTop, elided)

        p.restore()


class _LoraScanWorker(QObject):
    """バックグラウンドでLoRAフォルダをスキャンして未作成LoRA一覧を返すワーカー。

    L11 修正: 以前は main_window.UnmadeWildcardScanWorker とほぼ完全に
    重複したコードを持っていた。main_window 側を直接 import して再利用する。
    シグナル互換性のため、本クラスは thin wrapper として残す。
    """
    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, lora_root: str, haystack: str):
        super().__init__()
        # main_window 側の UnmadeWildcardScanWorker をそのまま再利用
        from .main_window import UnmadeWildcardScanWorker
        self._inner = UnmadeWildcardScanWorker(lora_root, haystack)
        self._inner.finished.connect(self.finished.emit)
        self._inner.failed.connect(self.failed.emit)

    @Slot()
    def run(self):
        self._inner.run()


class UnmadeLoraGridWidget(QWidget):
    """未作成LoRAサムネイルグリッドパネル"""

    lora_selected = Signal(dict)

    def __init__(self, items: list[dict], lora_root: str = "",
                 search_haystack: str = "", parent=None):
        super().__init__(parent)
        self._all_items = list(items)
        self._cards: list[LoraCardWidget] = []
        self._lora_root = lora_root
        self._search_haystack = search_haystack
        self._scan_thread: QThread | None = None
        self._scan_worker: _LoraScanWorker | None = None
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        folder_row = QWidget()
        folder_row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        fr = QHBoxLayout(folder_row)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.setSpacing(4)
        fr.addWidget(QLabel("LoRA:"), 0)
        self._folder_picker = _FolderPicker()
        self._folder_picker._edit.setFixedHeight(28)
        self._folder_picker._btn.setFixedHeight(28)
        self._folder_picker._btn.setMinimumWidth(40)
        if self._lora_root:
            self._folder_picker.setText(Path(self._lora_root).as_posix())
        fr.addWidget(self._folder_picker, 1)
        self._btn_rescan = QPushButton("再読込")
        self._btn_rescan.setFixedHeight(28)
        self._btn_rescan.setMinimumWidth(56)
        self._btn_rescan.clicked.connect(self._rescan_folder)
        fr.addWidget(self._btn_rescan, 0)
        layout.addWidget(folder_row)

        self._sort_combo = QComboBox()
        self._sort_combo.addItem("名前順", "name")
        self._sort_combo.addItem("追加日（新しい順）", "added_desc")
        self._sort_combo.addItem("更新日（新しい順）", "modified_desc")
        self._sort_combo.addItem("フォルダ順", "folder")
        self._sort_combo.setFixedHeight(30)
        self._sort_combo.currentIndexChanged.connect(self._apply_sort)
        layout.addWidget(self._sort_combo)

        self._search = QLineEdit()
        self._search.setPlaceholderText("LoRAを検索...")
        self._search.setFixedHeight(30)
        self._search.textChanged.connect(self._apply_filter)
        layout.addWidget(self._search)
        _setup_japanese_context_menu(self._search)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none}"
            "QScrollBar:vertical{background:#0f1216;width:8px}"
            "QScrollBar::handle:vertical{background:#2a3440;border-radius:4px;min-height:30px}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}"
        )
        self._grid_widget = QWidget()
        self._grid_widget.setStyleSheet("background:transparent")
        self._flow = FlowLayout(self._grid_widget, h_spacing=6, v_spacing=6)
        self._flow.setContentsMargins(4, 4, 4, 4)
        self._grid_widget.setLayout(self._flow)
        self._scroll.setWidget(self._grid_widget)
        layout.addWidget(self._scroll, 1)

        self._apply_sort()

    def _populate_cards(self):
        for item in self._all_items:
            card = LoraCardWidget(item)
            card.mousePressEvent = lambda ev, d=item, c=card: self._on_click(d, c)
            self._cards.append(card)
            self._flow.addWidget(card)

    def _on_click(self, item_data: dict, card: LoraCardWidget):
        self.lora_selected.emit(item_data)

    def _apply_filter(self, text: str):
        q = text.strip().lower()
        for card in self._cards:
            name = card.item_data.get("name", "").lower()
            card.setVisible(q in name if q else True)

    def _apply_sort(self):
        """プルダウンで選択された並び順で _all_items を並び替え、グリッドを作り直す"""
        if not hasattr(self, "_sort_combo"):
            return
        mode = self._sort_combo.currentData()
        if not mode:
            return
        self._all_items.sort(key=lambda item: self._sort_key(item, mode))
        self._rebuild_grid()

    def _rebuild_grid(self):
        for card in self._cards:
            self._flow.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._populate_cards()
        self._apply_filter(self._search.text())

    def _rescan_folder(self):
        lora_root = self._folder_picker.text().strip()
        if not lora_root:
            return
        self._btn_rescan.setEnabled(False)
        self._btn_rescan.setText("読込中...")
        self._scan_thread = QThread(self)
        self._scan_worker = _LoraScanWorker(lora_root, self._search_haystack)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.finished.connect(self._on_rescan_finished)
        self._scan_worker.failed.connect(self._on_rescan_failed)
        self._scan_worker.finished.connect(self._cleanup_scan)
        self._scan_worker.failed.connect(self._cleanup_scan)
        self._scan_thread.start()

    def _on_rescan_finished(self, items: list[dict]):
        self._all_items = items
        self._rebuild_grid()

    def _on_rescan_failed(self, message: str):
        self._btn_rescan.setEnabled(True)
        self._btn_rescan.setText("再読込")

    def _cleanup_scan(self):
        if self._scan_thread is not None:
            self._scan_thread.quit()
            self._scan_thread.wait(2000)
            self._scan_thread.deleteLater()
        if self._scan_worker is not None:
            self._scan_worker.deleteLater()
        self._scan_thread = None
        self._scan_worker = None
        self._btn_rescan.setEnabled(True)
        self._btn_rescan.setText("再読込")

    def _sort_key(self, item: dict, mode: str):
        if mode == "added_desc":
            return -self._get_timestamp(item, ("added", "added_at", "created", "created_at", "ctime"), use_ctime=True)
        if mode == "modified_desc":
            return -self._get_timestamp(item, ("modified", "modified_at", "updated", "updated_at", "mtime"), use_ctime=False)
        if mode == "folder":
            return (self._get_folder(item).lower(), (item.get("name", "") or "").lower())
        return (item.get("name", "") or "").lower()

    @staticmethod
    def _get_folder(item: dict) -> str:
        folder = item.get("folder")
        if folder:
            return str(folder)
        path = item.get("path")
        if path:
            try:
                return Path(path).parent.name
            except Exception:
                pass
        return ""

    @staticmethod
    def _to_timestamp(value) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                from datetime import datetime
                return datetime.fromisoformat(value).timestamp()
            except Exception:
                return None
        return None

    def _get_timestamp(self, item: dict, keys: tuple[str, ...], use_ctime: bool) -> float:
        for key in keys:
            ts = self._to_timestamp(item.get(key))
            if ts is not None:
                return ts
        path = item.get("path")
        if path:
            try:
                p = Path(path)
                if p.exists():
                    stat = p.stat()
                    return getattr(stat, "st_ctime", stat.st_mtime) if use_ctime else stat.st_mtime
            except Exception:
                pass
        return 0.0

    def remove_by_name(self, name: str):
        for i, item in enumerate(self._all_items):
            if item.get("name") == name:
                self._all_items.pop(i)
                card = self._cards.pop(i)
                self._flow.removeWidget(card)
                card.setParent(None)
                card.deleteLater()
                break

    def is_empty(self) -> bool:
        return len(self._all_items) == 0


# ─── キャラクタータブ内容クラス ────────────────────────────────────


class CharacterTabContent(QWidget):
    """1つのキャラクタータブのUIとロジックをカプセル化するウィジェット。

    全タブ（初期キャラクタータブ含む）はこのクラスのインスタンスを持つ。
    タブ名と保存先はダイアログを閉じても保持される。
    中身（衣装・プロンプト等）は毎回空で初期化される。
    """

    THUMB_MAX_W = 220
    THUMB_MAX_H = 320

    def __init__(self, settings: AppSettings, repo: WildcardRepository,
                 api: ThumbnailApiClient, folder_map: dict[str, set[str]],
                 default_root: str = "", parent=None):
        super().__init__(parent)
        self._settings = settings
        self._repo = repo
        self._api = api
        self._folder_map = folder_map

        # 衣装データ
        self._costumes: list[dict] = []
        self._prev_costume_row: int = -1

        # サムネイル
        self._thumb_path: str | None = None
        self._selected_thumbnail_path: str | None = None
        self._thumb_thread: QThread | None = None
        self._thumb_worker: SingleThumbnailWorker | None = None
        self._thumb_gen_row: int | None = None  # 生成開始時点の衣装行（完了時にcurrentRowを使わない）

        # アニメーション
        self._btn_gen: QPushButton | None = None
        self._btn_gen_all: QPushButton | None = None
        self._thumb_loading_timer = QTimer(self)
        self._thumb_loading_timer.setInterval(120)
        self._thumb_loading_timer.timeout.connect(self._advance_thumb_loading)
        self._thumb_loading_frames = ["[    ]", "[=   ]", "[==  ]", "[=== ]", "[ ===]", "[  ==]", "[   =]"]
        self._thumb_loading_frame = 0
        self._thumb_loading_active = False
        self._batch_gen_queue: list[int] = []
        self._batch_gen_active = False

        # 結合モード
        self._merge_mode = "replace"

        # タグ同期フラグ
        self._syncing_tags = False

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._build(default_root)

    # ── UI構築 ──────────────────────────────────────────────────

    def _build(self, default_root: str = ""):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        # ── 上段: カード名 / 保存先 ─────────────────────────
        header = QWidget()
        hg = QGridLayout(header)
        hg.setContentsMargins(0, 0, 0, 0)
        hg.setSpacing(4)
        hg.setColumnStretch(1, 1)
        hg.setColumnStretch(3, 2)

        hg.addWidget(QLabel("カード名:"), 0, 0)
        self.inp_name = QLineEdit()
        self.inp_name.setPlaceholderText("例: Alice")
        self.inp_name.setFixedHeight(34)
        hg.addWidget(self.inp_name, 0, 1)

        hg.addWidget(QLabel("保存先:"), 0, 2)
        self.inp_folder = self._make_folder(default_root)
        hg.addWidget(self.inp_folder, 0, 3)

        outer.addWidget(header)

        # ── 中段: 衣装一覧(左) | 衣装詳細(右) + サムネ ────
        mid_splitter = QSplitter(Qt.Horizontal)
        mid_splitter.setChildrenCollapsible(False)

        # 左: 衣装一覧 + 結合モード
        left_container = QWidget()
        left_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(6, 0, 6, 0)
        left_layout.setSpacing(6)

        list_box = QGroupBox("カード一覧")
        list_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        list_layout = QVBoxLayout(list_box)
        list_layout.setContentsMargins(6, 18, 6, 16)
        list_layout.setSpacing(6)
        self.costume_list = QListWidget()
        self.costume_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.costume_list.currentRowChanged.connect(self._costume_row_changing)
        self.costume_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        list_layout.addWidget(self.costume_list)
        left_layout.addWidget(list_box, 1)

        # 結合モード
        merge_group = QGroupBox("結合モード")
        merge_layout = QVBoxLayout(merge_group)
        merge_layout.setContentsMargins(8, 12, 8, 12)
        merge_layout.setSpacing(8)
        merge_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        radio_row = QHBoxLayout()
        radio_row.setSpacing(12)
        self.radio_replace = QRadioButton("選択")
        self.radio_replace.setChecked(True)
        self.radio_replace.toggled.connect(lambda c: setattr(self, "_merge_mode", "replace") if c else None)
        self.radio_append_up = QRadioButton("上に追加")
        self.radio_append_up.toggled.connect(lambda c: setattr(self, "_merge_mode", "append_up") if c else None)
        self.radio_append_down = QRadioButton("下に追加")
        self.radio_append_down.toggled.connect(lambda c: setattr(self, "_merge_mode", "append_down") if c else None)
        radio_row.addWidget(self.radio_replace)
        radio_row.addWidget(self.radio_append_up)
        radio_row.addWidget(self.radio_append_down)
        radio_row.addStretch(1)
        merge_layout.addLayout(radio_row)

        # 結合ボタン群
        btns = QWidget()
        btns_l = QHBoxLayout(btns)
        btns_l.setContentsMargins(0, 0, 0, 0)
        btns_l.setSpacing(6)
        self.btn_merge = QPushButton("結合")
        self.btn_merge.setFixedHeight(34)
        self.btn_merge.clicked.connect(self._on_merge)
        btns_l.addWidget(self.btn_merge)
        self.btn_merge_all = QPushButton("すべて結合")
        self.btn_merge_all.setFixedHeight(34)
        self.btn_merge_all.clicked.connect(self._merge_all)
        btns_l.addWidget(self.btn_merge_all)
        self.btn_expand_merge = QPushButton("展開結合")
        self.btn_expand_merge.setFixedHeight(34)
        self.btn_expand_merge.clicked.connect(self._expand_merge)
        self.btn_expand_merge.setToolTip("展開結合には2つ以上のカードが必要です")
        btns_l.addWidget(self.btn_expand_merge)
        merge_layout.addWidget(btns)

        # 衣装追加/削除ボタン
        costume_btn_row = QHBoxLayout()
        costume_btn_row.setContentsMargins(0, 0, 0, 0)
        costume_btn_row.setSpacing(6)
        self.btn_add = QPushButton("+ カード追加")
        self.btn_add.setFixedHeight(34)
        self.btn_add.clicked.connect(self._add_costume)
        costume_btn_row.addWidget(self.btn_add)
        self.btn_del = QPushButton("- カード削除")
        self.btn_del.setFixedHeight(34)
        self.btn_del.clicked.connect(self._del_costume)
        costume_btn_row.addWidget(self.btn_del)
        costume_btn_row.addStretch(1)
        merge_layout.addLayout(costume_btn_row)

        left_layout.addSpacing(6)
        left_layout.addWidget(merge_group)
        self.btn_expand_merge.setEnabled(False)

        mid_splitter.addWidget(left_container)

        # 右: 衣装詳細 + サムネ横並び
        right_container = QWidget()
        right_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        right_h = QHBoxLayout(right_container)
        right_h.setContentsMargins(6, 0, 6, 0)
        right_h.setSpacing(6)

        # 右-左: 衣装詳細
        det = QGroupBox("カード詳細")
        det.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        det_v = QVBoxLayout(det)
        det_v.setContentsMargins(8, 12, 8, 8)
        det_v.setSpacing(8)

        lbl_name = QLabel("カード名:")
        det_v.addWidget(lbl_name)
        self.inp_cname = QLineEdit()
        self.inp_cname.setPlaceholderText("省略可")
        self.inp_cname.setFixedHeight(34)
        det_v.addWidget(self.inp_cname)

        lbl_prompt = QLabel("プロンプト:")
        det_v.addWidget(lbl_prompt)
        self.inp_prompt = NoDropPlainTextEdit()
        self.inp_prompt.setPlaceholderText("カンマ区切り")
        self.inp_prompt.textChanged.connect(self._autosave_current_costume)
        det_v.addWidget(self.inp_prompt, 1)

        # タグ一覧エリア
        tag_header = QHBoxLayout()
        tag_header.setContentsMargins(0, 0, 0, 0)
        tag_header.setSpacing(4)
        self.tag_editor = TagEditorWidget()
        self.tag_editor.tagsChanged.connect(self._on_tags_changed)
        self.tag_editor.tagClicked.connect(self._on_tag_clicked)
        self.btn_delete_mode = QPushButton("削除モード")
        self.btn_delete_mode.setFixedHeight(26)
        self.btn_delete_mode.setMinimumWidth(70)
        self.btn_delete_mode.setCheckable(True)
        self.btn_delete_mode.toggled.connect(self._toggle_delete_mode)
        self.btn_delete_selected = QPushButton("選択削除")
        self.btn_delete_selected.setFixedHeight(26)
        self.btn_delete_selected.setMinimumWidth(70)
        self.btn_delete_selected.setVisible(False)
        self.btn_delete_selected.clicked.connect(self._delete_selected_tags)
        tag_header.addWidget(self.tag_editor.help_label)
        tag_header.addStretch(1)
        tag_header.addWidget(self.btn_delete_selected)
        tag_header.addWidget(self.btn_delete_mode)
        det_v.addLayout(tag_header)
        det_v.addWidget(self.tag_editor)

        right_h.addWidget(det, 1)

        # 右-右: サムネ + LoRA
        right_h.addWidget(self._build_sidebar())

        mid_splitter.addWidget(right_container)
        mid_splitter.setSizes([220, 780])

        outer.addWidget(mid_splitter, 1)

        # コンテキストメニューを日本語化
        _setup_japanese_context_menu(self.inp_name)
        _setup_japanese_context_menu(self.inp_cname)
        _setup_japanese_context_menu(self.inp_prompt)

    def _build_sidebar(self) -> QWidget:
        """サムネ + LoRA + 生成ボタンのサイドバー"""
        w = QWidget()
        w.setFixedWidth(260)
        g = QVBoxLayout(w)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(6)

        self.thumb = QLabel()
        self.thumb.setFixedSize(self.THUMB_MAX_W, self.THUMB_MAX_W)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet(
            "background:#121a27;border:1px solid #2a3440;border-radius:6px;"
        )
        g.addWidget(self.thumb, 0, Qt.AlignHCenter)

        lora_row = QWidget()
        lora_row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lr = QVBoxLayout(lora_row)
        lr.setContentsMargins(6, 6, 6, 0)
        lr.setSpacing(4)
        lr.addWidget(QLabel("LoRA:"))
        self.inp_lora = NoDropPlainTextEdit()
        self.inp_lora.setPlaceholderText("<lora:name:1>")
        self.inp_lora.setFixedHeight(56)
        self.inp_lora.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lr.addWidget(self.inp_lora)
        g.addWidget(lora_row)

        self.chk_use_random_wc = QCheckBox("ランダムワイルドカードを使用")
        self.chk_use_random_wc.setChecked(False)
        g.addWidget(self.chk_use_random_wc)

        self._btn_gen = QPushButton("サムネイル生成")
        self._btn_gen.setFixedHeight(36)
        self._btn_gen.clicked.connect(self._gen_thumb)
        g.addWidget(self._btn_gen)

        self._btn_gen_all = QPushButton("すべて作成")
        self._btn_gen_all.setFixedHeight(36)
        self._btn_gen_all.clicked.connect(self._gen_all_thumbs)
        g.addWidget(self._btn_gen_all)

        g.addStretch(1)
        return w

    def _make_folder(self, default_root: str = "") -> _FolderPicker:
        picker = _FolderPicker()
        default = default_root or self._settings.random_character_root
        if default:
            picker.setText(Path(default).as_posix())
        else:
            picker.setText(Path(self._settings.library_root).as_posix())
        return picker

    # ── 衣装管理 ────────────────────────────────────────────────

    def _add_costume(self):
        n = len(self._costumes) + 1
        self._costumes.append({"name": f"{n}", "prompt": "", "tags": []})
        self.costume_list.addItem(f"{n}")
        if len(self._costumes) == 1:
            self.costume_list.setCurrentRow(0)
        self._update_expand_merge_state()

    def _del_costume(self):
        if len(self._costumes) <= 1:
            return
        row = self.costume_list.currentRow()
        if row < 0:
            return
        self._save_detail()
        self._costumes.pop(row)
        self._rebuild_costume_list(select_row=row)
        self._update_expand_merge_state()

    def _costume_row_changing(self, new_row: int):
        self._save_detail_for_row(self._prev_costume_row)
        self._prev_costume_row = new_row
        if 0 <= new_row < len(self._costumes):
            self._load_detail(new_row)

    def _load_detail(self, row: int):
        c = self._costumes[row]
        self.inp_cname.setText(c["name"].replace("衣装", "") if c["name"].startswith("衣装") else c["name"])
        self.inp_prompt.blockSignals(True)
        self.inp_prompt.setPlainText(c["prompt"])
        self.inp_prompt.blockSignals(False)
        self._sync_prompt_to_tags()
        thumb_path = c.get("thumbnail")
        if thumb_path and Path(thumb_path).exists():
            self._set_thumb(thumb_path)
        else:
            self._clear_thumb_pixmap()

    def _autosave_current_costume(self):
        row = self.costume_list.currentRow()
        if 0 <= row < len(self._costumes):
            self._costumes[row]["prompt"] = self.inp_prompt.toPlainText()
            self._sync_prompt_to_tags()

    def _save_detail(self):
        self._save_detail_for_row(self.costume_list.currentRow())

    def _save_detail_for_row(self, row: int):
        if row < 0 or row >= len(self._costumes):
            return
        nm = self.inp_cname.text().strip()
        new_name = f"{row+1}" if not nm else nm
        old_name = self._costumes[row].get("name", "")
        # _load_detail() は表示時に先頭の「衣装」を除去しているため、同じ正規化で比較する
        # （除去しないと、編集せず切り替えただけで毎回「改名」扱いになってしまう）
        old_name_display = old_name[2:] if old_name.startswith("衣装") else old_name
        if old_name_display != new_name and self._costumes[row].get("thumbnail"):
            self._costumes[row]["thumbnail"] = None
            if self.costume_list.currentRow() == row:
                self._clear_thumb_pixmap()
        self._costumes[row]["name"] = new_name
        self._costumes[row]["prompt"] = self.inp_prompt.toPlainText()
        item = self.costume_list.item(row)
        if item:
            item.setText(self._costumes[row]["name"])

    def _rebuild_costume_list(self, select_row: int = -1):
        self.costume_list.blockSignals(True)
        self.costume_list.clear()
        for c in self._costumes:
            self.costume_list.addItem(c["name"])
        new_row = -1
        if self._costumes:
            new_row = max(0, min(select_row, len(self._costumes) - 1))
            self.costume_list.setCurrentRow(new_row)
        self.costume_list.blockSignals(False)
        if new_row >= 0:
            self._prev_costume_row = new_row
            self._load_detail(new_row)
        else:
            self._prev_costume_row = -1
            self.inp_cname.clear()
            self.inp_prompt.blockSignals(True)
            self.inp_prompt.clear()
            self.inp_prompt.blockSignals(False)

    # ── 結合 ────────────────────────────────────────────────────

    def _on_merge(self):
        sel_items = self.costume_list.selectedItems()
        if not sel_items:
            return
        self._save_detail()
        target_row = None
        if len(sel_items) == 1:
            row = self.costume_list.row(sel_items[0])
            if self._merge_mode == "append_up" and row > 0:
                above = self._costumes[row - 1]["prompt"]
                cur = self._costumes[row]["prompt"]
                self._costumes[row - 1]["prompt"] = f"{above.rstrip()}, {cur.lstrip()}" if above and cur else (above or cur)
                self._costumes.pop(row)
                target_row = row - 1
            elif self._merge_mode == "append_down" and row < len(self._costumes) - 1:
                cur = self._costumes[row]["prompt"]
                below = self._costumes[row + 1]["prompt"]
                self._costumes[row]["prompt"] = f"{cur.rstrip()}, {below.lstrip()}" if cur and below else (cur or below)
                self._costumes.pop(row + 1)
                target_row = row
        else:
            sel_rows = sorted([self.costume_list.row(it) for it in sel_items])
            target = sel_rows[0]
            merged = ", ".join(self._costumes[r]["prompt"] for r in sel_rows if self._costumes[r]["prompt"])
            self._costumes[target]["prompt"] = merged
            for r in reversed(sel_rows):
                if r != target:
                    self._costumes.pop(r)
            target_row = target
        if target_row is None:
            return
        self._rebuild_costume_list(select_row=target_row)

    def _merge_all(self):
        count = self.costume_list.count()
        if count <= 1:
            return
        self.costume_list.selectAll()
        self._on_merge()

    def _expand_merge(self):
        row = self.costume_list.currentRow()
        if row < 0 or len(self._costumes) < 2:
            return
        self._save_detail_for_row(row)
        axis = self._costumes[row]
        others = [c for i, c in enumerate(self._costumes) if i != row]
        if not others:
            return
        confirm = QMessageBox.question(
            self, "展開結合確認",
            f"カード「{axis['name']}」を軸に展開結合します。\n"
            f"軸カードと他の{len(others)}個のカードは削除され、{len(others)}個の新しいカードが生成されます。\nよろしいですか？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return
        new_costumes = []
        for other in others:
            new_name = f"{axis['name']}+{other['name']}"
            new_prompt = f"{axis['prompt']}, {other['prompt']}" if axis['prompt'] and other['prompt'] else (axis['prompt'] or other['prompt'])
            new_costumes.append({"name": new_name, "prompt": new_prompt, "tags": []})
        self._costumes = new_costumes
        self._rebuild_costume_list(select_row=0)
        self._update_expand_merge_state()

    def _update_expand_merge_state(self):
        count = self.costume_list.count()
        self.btn_expand_merge.setEnabled(count >= 2)

    # ── タグ ────────────────────────────────────────────────────

    def _on_tags_changed(self):
        if self._syncing_tags:
            return
        self._syncing_tags = True
        tags = self.tag_editor.tags()
        self.inp_prompt.setPlainText(", ".join(tags))
        self._syncing_tags = False
        self._autosave_current_costume()

    def _sync_prompt_to_tags(self):
        if self._syncing_tags:
            return
        self._syncing_tags = True
        text = self.inp_prompt.toPlainText()
        tags = [t.strip() for t in text.split(",") if t.strip()]
        self.tag_editor.set_tags(tags)
        self._syncing_tags = False

    def _toggle_delete_mode(self, checked: bool):
        if checked:
            self.tag_editor.set_editable(True)
        else:
            self.tag_editor.set_editable(False)
        self.btn_delete_selected.setVisible(False)

    def _delete_selected_tags(self):
        self.tag_editor.delete_selected_tags()
        self.btn_delete_selected.setVisible(False)

    def _update_delete_button_state(self):
        if self.tag_editor._select_mode:
            self.btn_delete_selected.setVisible(self.tag_editor.has_selected_tags())

    def _on_tag_clicked(self, tag: str):
        if not self.tag_editor._select_mode:
            from PySide6.QtWidgets import QApplication, QToolTip
            from PySide6.QtGui import QCursor
            clipboard = QApplication.clipboard()
            if clipboard:
                clipboard.setText(tag)
                QToolTip.showText(QCursor.pos(), f"コピー: {tag}", self)

    # ── サムネイル ──────────────────────────────────────────────

    def _set_thumb(self, path: str):
        self._thumb_path = path
        self._selected_thumbnail_path = path
        raw = QPixmap(path)
        if raw.isNull():
            self._clear_thumb_pixmap()
            return
        px = raw.scaled(
            self.THUMB_MAX_W, self.THUMB_MAX_H,
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self.thumb.setFixedSize(px.size())
        self.thumb.setPixmap(px)
        self.thumb.setStyleSheet(
            "background:#121a27;border:1px solid #2a3440;border-radius:6px;"
        )
        self.thumb.setToolTip("クリックして別の画像に変更できます")

    def _clear_thumb_pixmap(self):
        self.thumb.setFixedSize(self.THUMB_MAX_W, self.THUMB_MAX_W)
        self.thumb.setPixmap(QPixmap())
        self.thumb.setText("")
        self.thumb.setStyleSheet(
            "background:#121a27;border:1px solid #2a3440;border-radius:6px;"
        )

    def _animate_button_press(self):
        if not self._btn_gen:
            return
        self._btn_gen.setStyleSheet(
            "QPushButton{background:#1a3a6a;border:1px solid #1a3a6a;border-radius:4px;color:white;padding:0 10px;font-size:12px;min-height:32px}"
        )
        QTimer.singleShot(100, self._reset_button_style)

    def _reset_button_style(self):
        if self._btn_gen:
            self._btn_gen.setStyleSheet("")

    def _start_thumb_loading_animation(self, message: str = "サムネイル生成中..."):
        self._thumb_loading_active = True
        self._thumb_loading_frame = 0
        self.thumb.setFixedSize(self.THUMB_MAX_W, self.THUMB_MAX_W)
        self.thumb.setPixmap(QPixmap())
        self._advance_thumb_loading(message)
        self._thumb_loading_timer.start()

    def _advance_thumb_loading(self, message: str = "サムネイル生成中..."):
        if not self._thumb_loading_active:
            return
        frame = self._thumb_loading_frames[self._thumb_loading_frame % len(self._thumb_loading_frames)]
        self._thumb_loading_frame += 1
        self.thumb.setStyleSheet(
            "background:#121a27;border:1px solid #2a3440;border-radius:6px;color:#edf2f7;font-size:12px;"
        )
        self.thumb.setText(f"{frame}\n{message}")

    def _stop_thumb_loading_animation(self):
        self._thumb_loading_active = False
        self._thumb_loading_timer.stop()

    def _pick_thumb(self):
        p, _ = QFileDialog.getOpenFileName(self, "画像を選択", "", "画像 (*.png *.jpg *.jpeg *.webp *.bmp);;すべて (*)")
        if p:
            self._set_thumb(p)

    def _gen_thumb(self):
        if self._thumb_thread and self._thumb_thread.isRunning():
            return
        self._animate_button_press()
        name = self.inp_name.text().strip()
        if not name:
            QMessageBox.warning(self, "エラー", "ファイル名を入力してください。"); return
        folder = self.inp_folder.currentText().strip()
        if not folder:
            QMessageBox.warning(self, "エラー", "保存先を選択してください。"); return
        row = self.costume_list.currentRow()
        prompt = ""
        if 0 <= row < len(self._costumes):
            prompt = self._costumes[row]["prompt"].strip()
        if not prompt:
            QMessageBox.warning(self, "エラー", "プロンプトを入力してください。"); return
        lora = self.inp_lora.toPlainText().strip()
        if lora:
            prompt = f"{lora},{prompt}"
        # プレビュー用サムネは本番の保存先(name.txt)とは別パスに、衣装行(row)で一意化して生成する。
        # ここを本番と同じrel_pathにすると、衣装ごとに生成しても同じファイルに上書きされてしまう。
        preview_stem = f"{name}_{row:02d}"
        rel = f"{PREVIEW_THUMB_SUBDIR}/{Path(folder).name}/{preview_stem}.txt"
        abs_path = str(Path(folder) / f"{name}.txt")
        e = WildcardEntry(rel_path=rel, abs_path=abs_path,
                          folder=f"{PREVIEW_THUMB_SUBDIR}/{Path(folder).name}", name=f"{preview_stem}.txt", stem=preview_stem,
                          content=prompt)
        import copy
        worker_settings = copy.copy(self._settings)
        worker_settings.thumbnail_random_wildcard = self.chk_use_random_wc.isChecked()
        self._thumb_gen_row = row
        self._btn_gen.setText("生成中...")
        self._btn_gen.setEnabled(False)
        self._btn_gen_all.setEnabled(False)
        self._start_thumb_loading_animation()
        self._thumb_thread = QThread(self)
        self._thumb_worker = SingleThumbnailWorker(self._repo.db_path, worker_settings, e)
        self._thumb_worker.moveToThread(self._thumb_thread)
        self._thumb_thread.started.connect(self._thumb_worker.run)
        self._thumb_worker.finished.connect(self._thumb_done)
        self._thumb_worker.failed.connect(self._on_thumb_failed)
        self._thumb_thread.start()

    def _gen_all_thumbs(self):
        if self._thumb_thread and self._thumb_thread.isRunning():
            return
        name = self.inp_name.text().strip()
        if not name:
            QMessageBox.warning(self, "エラー", "ファイル名を入力してください。"); return
        folder = self.inp_folder.currentText().strip()
        if not folder:
            QMessageBox.warning(self, "エラー", "保存先を選択してください。"); return
        self._batch_gen_queue = [i for i, c in enumerate(self._costumes) if c.get("prompt", "").strip()]
        if not self._batch_gen_queue:
            QMessageBox.warning(self, "エラー", "プロンプトが入力されたカードがありません。"); return
        self._batch_gen_active = True
        self._btn_gen.setEnabled(False)
        self._btn_gen_all.setEnabled(False)
        self._start_thumb_loading_animation(f"サムネイル生成中... (0/{len(self._batch_gen_queue)})")
        self._process_batch_queue()

    def _process_batch_queue(self):
        if not self._batch_gen_queue:
            self._batch_gen_active = False
            self._stop_thumb_loading_animation()
            self._btn_gen.setText("サムネイル生成")
            self._btn_gen.setEnabled(True)
            self._btn_gen_all.setEnabled(True)
            self._cleanup_thumb()
            return
        row = self._batch_gen_queue[0]
        name = self.inp_name.text().strip()
        folder = self.inp_folder.currentText().strip()
        prompt = self._costumes[row]["prompt"].strip()
        lora = self.inp_lora.toPlainText().strip()
        if lora:
            prompt = f"{lora},{prompt}"
        total = len(self._batch_gen_queue)
        done = total - len(self._batch_gen_queue)
        self._advance_thumb_loading(f"サムネイル生成中... ({done}/{total})")
        preview_stem = f"{name}_{row:02d}"
        rel = f"{PREVIEW_THUMB_SUBDIR}/{Path(folder).name}/{preview_stem}.txt"
        abs_path = str(Path(folder) / f"{name}.txt")
        e = WildcardEntry(rel_path=rel, abs_path=abs_path,
                          folder=f"{PREVIEW_THUMB_SUBDIR}/{Path(folder).name}",
                          name=f"{preview_stem}.txt", stem=preview_stem,
                          content=prompt)
        import copy
        worker_settings = copy.copy(self._settings)
        worker_settings.thumbnail_random_wildcard = self.chk_use_random_wc.isChecked()
        self._thumb_gen_row = row
        self._thumb_thread = QThread(self)
        self._thumb_worker = SingleThumbnailWorker(self._repo.db_path, worker_settings, e)
        self._thumb_worker.moveToThread(self._thumb_thread)
        self._thumb_thread.started.connect(self._thumb_worker.run)
        self._thumb_worker.finished.connect(self._on_batch_thumb_done)
        self._thumb_worker.failed.connect(self._on_batch_thumb_failed)
        self._thumb_thread.start()

    def _on_batch_thumb_done(self, entry: WildcardEntry, _: str):
        if entry.thumbnail_path:
            row = self._thumb_gen_row
            if row is not None and 0 <= row < len(self._costumes):
                self._costumes[row]["thumbnail"] = entry.thumbnail_path
        self._thumb_gen_row = None
        self._cleanup_thumb()
        self._batch_gen_queue.pop(0)
        self._process_batch_queue()

    def _on_batch_thumb_failed(self, message: str):
        self._thumb_gen_row = None
        self._cleanup_thumb()
        self._batch_gen_queue.pop(0)
        self._process_batch_queue()

    def _on_thumb_failed(self, message: str):
        self._stop_thumb_loading_animation()
        self._btn_gen.setText("サムネイル生成")
        self._btn_gen.setEnabled(True)
        self._btn_gen_all.setEnabled(True)
        QMessageBox.warning(self, "エラー", message)
        self._cleanup_thumb()

    def _thumb_done(self, entry: WildcardEntry, _: str):
        self._stop_thumb_loading_animation()
        self._btn_gen.setText("サムネイル生成")
        self._btn_gen.setEnabled(True)
        self._btn_gen_all.setEnabled(True)
        if entry.thumbnail_path:
            row = self._thumb_gen_row
            if row is not None and 0 <= row < len(self._costumes):
                self._costumes[row]["thumbnail"] = entry.thumbnail_path
                # 生成中に別の衣装行へ切り替えられていた場合、表示中のサムネは更新しない
                if self.costume_list.currentRow() == row:
                    self._set_thumb(entry.thumbnail_path)
        self._thumb_gen_row = None
        self._cleanup_thumb()

    def _cleanup_thumb(self):
        if self._thumb_thread:
            self._thumb_thread.quit(); self._thumb_thread.wait(2000)
            self._thumb_thread = None; self._thumb_worker = None

    # ── フォーム状態 ────────────────────────────────────────────

    def clear_form(self):
        """フォームをクリア（中身は毎回空でOK）"""
        self.inp_name.clear()
        self.inp_lora.clear()
        self.inp_cname.clear()
        self.inp_prompt.clear()
        self.tag_editor.set_tags([])
        self.btn_delete_mode.setChecked(False)
        self._costumes.clear()
        self.costume_list.clear()
        self._prev_costume_row = -1
        self._thumb_path = None
        self._selected_thumbnail_path = None
        self._clear_thumb_pixmap()
        self._update_expand_merge_state()

    def capture_snapshot(self) -> str:
        """フォームの現在の内容を比較用の文字列にして取得する。"""
        self._save_detail()
        payload = {
            "name": self.inp_name.text().strip(),
            "lora": self.inp_lora.toPlainText().strip(),
            "costumes": [
                {"name": c.get("name", ""), "prompt": c.get("prompt", "")}
                for c in self._costumes
            ],
            "tags": self.tag_editor.tags(),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def get_save_path(self) -> str:
        """現在の保存先パスを返す"""
        return self.inp_folder.currentText().strip()

    def set_save_path(self, path: str):
        """保存先パスを設定する"""
        if path:
            self.inp_folder.setText(path)

    # ── 作成 ────────────────────────────────────────────────────

    def _confirm_overwrite(self, fname: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("確認")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"既に存在します: {fname}\n上書きしますか？")
        btn_overwrite = box.addButton("上書き", QMessageBox.AcceptRole)
        btn_cancel = box.addButton("キャンセル", QMessageBox.RejectRole)
        box.setDefaultButton(btn_cancel)
        box.exec()
        return box.clickedButton() is btn_overwrite

    def _next_available_filename(self, folder: str, fname: str) -> str:
        """folder内でfnameが衝突する場合に、空いている連番ファイル名を返す。"""
        stem = Path(fname).stem
        suffix = Path(fname).suffix
        n = 2
        while True:
            candidate = f"{stem}_{n}{suffix}"
            if not (Path(folder) / candidate).exists():
                return candidate
            n += 1

    def create_entries(self) -> list[WildcardEntry]:
        """このタブの内容からワイルドカードエントリを作成する。"""
        name = self.inp_name.text().strip()
        if not name:
            QMessageBox.warning(self, "エラー", "カード名を入力してください。"); return []
        folder = self.inp_folder.currentText().strip()
        if not folder:
            QMessageBox.warning(self, "エラー", "保存先を選択してください。"); return []
        if not self._costumes:
            QMessageBox.warning(self, "エラー", "カードを1件以上追加してください。"); return []
        lora = self.inp_lora.toPlainText().strip()
        created = []
        self._save_detail()
        for i, c in enumerate(self._costumes, 1):
            prompt = c["prompt"].strip()
            if not prompt:
                continue
            cn = c["name"].strip()
            is_default_name = not cn or cn == f"{i}" or cn == f"衣装{i}"
            if is_default_name:
                fname = f"{name}_{i:02d}.txt"
            else:
                fname = f"{name}_{cn}.txt"
            if lora:
                prompt = f"{lora},{prompt}"
            ap = str(Path(folder) / fname)
            if Path(ap).exists():
                try:
                    existing_content = self._repo.load_entry_content(Path(ap))
                except OSError:
                    existing_content = None
                if existing_content is not None and existing_content.strip() == prompt.strip():
                    # 中身が同じ＝実質的に同一ファイル → 上書きするか確認
                    if not self._confirm_overwrite(fname):
                        continue
                else:
                    # 同名だが中身が違う別物 → 上書きせず連番で新規ファイルとして保存
                    fname = self._next_available_filename(folder, fname)
                    ap = str(Path(folder) / fname)
            e = WildcardEntry(rel_path=f"{Path(folder).name}/{fname}", abs_path=ap,
                              folder=Path(folder).name, name=fname, stem=Path(fname).stem)
            e = self._repo.save_entry(self._settings, e, prompt, [])
            # 事前生成済みサムネイルをコピー
            pre_thumb = c.get("thumbnail")
            if pre_thumb and Path(pre_thumb).exists():
                from .repository import ensure_thumbnail_destination
                dest = ensure_thumbnail_destination(Path(self._settings.thumbnail_root), e.rel_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(pre_thumb, dest)
                e.thumbnail_path = str(dest)
            created.append(e)
        if not created:
            QMessageBox.warning(self, "エラー", "プロンプトが入力されたカードがありません。")
        else:
            self._cleanup_preview_thumbs(folder, name)
        return created

    def _cleanup_preview_thumbs(self, folder: str, name: str) -> None:
        """作成完了後、このカード用に一時生成したプレビュー用サムネイルを削除する。
        他のカード/タブが同じ保存先フォルダを使っていてもファイル名が name_行番号 で
        一意なため、このカード分(name_*)だけを安全に削除できる。"""
        preview_dir = Path(self._settings.thumbnail_root) / PREVIEW_THUMB_SUBDIR / Path(folder).name
        if not preview_dir.exists():
            return
        for f in preview_dir.glob(f"{name}_*.preview.webp"):
            try:
                f.unlink()
            except OSError:
                pass


# ─── ダイアログ本体 ──────────────────────────────────────────────


class NewWildcardDialog(QDialog):
    def __init__(self, settings: AppSettings, repo: WildcardRepository,
                 api: ThumbnailApiClient, folder_children_map: dict[str, set[str]],
                 unmade_items: list[dict] | None = None,
                 custom_tabs: list[dict] | None = None,
                 parent=None):
        super().__init__(parent)
        self._settings = settings
        self._repo = repo
        self._api = api
        self._folder_map = folder_children_map
        self.created_entries: list[WildcardEntry] = []
        self._unmade_items = unmade_items
        self._costume_forms: list[dict] = []
        self._selected_thumbnail_path: str | None = None
        # JSONインポート後の変更検知用ベーススナップショット
        self._baseline_snapshot: str | None = None
        # タブ参照を保持（findChild廃止）
        self.tabs: QTabWidget | None = None
        # JSONドロップオーバーレイ
        self._json_overlay: QLabel | None = None
        # 未作成LoRAグリッド
        self._unmade_grid: UnmadeLoraGridWidget | None = None
        self._current_unmade_item: dict | None = None
        # "+" タブ自動追加トリガーの誤発火防止フラグ
        self._suppress_tab_auto_add = False
        # 永続化されたタブ設定
        self._initial_custom_tabs = custom_tabs or []
        # accept()/reject() 実行時点（self.tabsがまだ生きている間）に
        # get_custom_tabs() の結果をスナップショットしておくためのプレーンな
        # Python属性。WA_DeleteOnClose下では exec() が呼び出し元に戻った時点で
        # 既にQTabWidgetのC++実体が破棄されている場合があり、呼び出し側
        # （main_window.py）が exec() の後から dialog.get_custom_tabs() を
        # 呼ぶと RuntimeError: Internal C++ object already deleted で落ちて
        # しまう。代わりにこの属性（プレーンなlist）を呼び出し側で読む。
        self.custom_tabs_result: list[dict] = list(self._initial_custom_tabs)

        self.setWindowTitle("ワイルドカード新規作成")
        # ダイアログを閉じた際にC++オブジェクトを確実に破棄する。
        # これが無いと reject()/accept() で隠れるだけで実体は残り続け、
        # 開くたびにタブ・イベントフィルタ・シグナル接続が親(MainWindow)に
        # 積み重なっていく（メモリリーク兼クラッシュ要因）。
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        if unmade_items is not None:
            self.setMinimumSize(1400, 700)
            self.resize(1600, 800)
        else:
            self.setMinimumSize(1100, 680)
            self.resize(1280, 760)
        self.setAcceptDrops(True)
        self._apply_style()
        self._build()

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog{background:#0f1216;color:#edf2f7}
            QLabel{color:#edf2f7;font-size:12px;background:transparent;border:none}
            QLineEdit,QPlainTextEdit,QComboBox{background:#161b22;border:1px solid #2a3440;border-radius:4px;color:#edf2f7;padding:4px 8px;font-size:12px}
            QGroupBox{border:1px solid #2a3440;border-radius:4px;margin-top:4px;color:#edf2f7;font-weight:600;font-size:14px}
            QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px}
            QPushButton{background:#2e5fb8;border:1px solid #2e5fb8;border-radius:4px;color:white;padding:0 10px;font-size:12px}
            QPushButton:hover{background:#3a71c6;border-color:#3a71c6}
            QListWidget{background:#161b22;border:1px solid #2a3440;border-radius:4px;color:#edf2f7;font-size:12px}
            QListWidget{font-size:13px}
            QListWidget::item{padding:4px 8px}
            QListWidget::item:selected{background:#2e5fb8}
            QListWidget::item:hover{background:#1d2630}
            QScrollArea{background:transparent;border:none}
            QSplitter::handle{background:#2a3440}
            QRadioButton{color:#edf2f7;font-size:11px;spacing:6px}
            QRadioButton::indicator{width:14px;height:14px;border:2px solid #4a5568;border-radius:8px;background:#161b22}
            QRadioButton::indicator:checked{border-color:#2e5fb8;background:#2e5fb8}
            QRadioButton::indicator:hover{border-color:#3a71c6}
        """)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # 初期キャラクタータブ (index 0)
        char_tab = CharacterTabContent(
            self._settings, self._repo, self._api, self._folder_map,
            parent=self,
        )
        # 保存先変更時に settings.random_character_root も更新
        char_tab.inp_folder._edit.textChanged.connect(self._on_character_folder_changed)
        self.tabs.addTab(char_tab, "キャラクター")

        # 永続化されたカスタムタブを復元
        for tab_cfg in self._initial_custom_tabs:
            name = tab_cfg.get("name", "")
            save_path = tab_cfg.get("save_path", "")
            ct = CharacterTabContent(
                self._settings, self._repo, self._api, self._folder_map,
                default_root=save_path, parent=self,
            )
            plus_idx = self.tabs.count()
            self.tabs.insertTab(plus_idx, ct, name)

        # "+" タブを最後に追加
        self._add_plus_tab()

        # 未作成LoRAモード: スプリッターで左右ペイン構成
        if self._unmade_items is not None:
            splitter = QSplitter(Qt.Horizontal)
            splitter.setChildrenCollapsible(False)
            haystack = "\n".join(
                (e.search_text or "").lower()
                for e in self._repo.load_entries_summary(self._settings)
            )
            self._unmade_grid = UnmadeLoraGridWidget(
                self._unmade_items,
                lora_root=self._settings.lora_root,
                search_haystack=haystack,
            )
            self._unmade_grid.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self._unmade_grid.setMinimumWidth(300)
            self._unmade_grid.setMaximumWidth(360)
            self._unmade_grid.lora_selected.connect(self._on_unmade_lora_clicked)
            self._unmade_grid.setContentsMargins(0, 0, 6, 0)
            self.tabs.setContentsMargins(6, 0, 0, 0)
            splitter.addWidget(self._unmade_grid)
            splitter.addWidget(self.tabs)
            splitter.setSizes([320, 1100])
            root.addWidget(splitter, 1)
        else:
            root.addWidget(self.tabs, 1)

        # ボタン行
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 8, 0, 0)
        btn_row.setSpacing(8)
        btn_row.addStretch(1)

        self.btn_cancel = QPushButton("キャンセル")
        self.btn_cancel.setFixedHeight(36)
        self.btn_cancel.setMinimumWidth(80)
        self.btn_cancel.clicked.connect(self._on_cancel_clicked)
        if self._unmade_items is not None:
            self.btn_cancel.setVisible(False)
        btn_row.addWidget(self.btn_cancel)
        if self._unmade_items is not None:
            self.btn_close = QPushButton("キャンセル")
            self.btn_close.setFixedHeight(36)
            self.btn_close.setMinimumWidth(80)
            self.btn_close.clicked.connect(self._on_close_clicked)
            btn_row.addWidget(self.btn_close)
        self.btn_create = QPushButton("作成")
        self.btn_create.setFixedHeight(36)
        self.btn_create.setMinimumWidth(80)
        self.btn_create.clicked.connect(self._do_create)
        btn_row.addWidget(self.btn_create)
        root.addLayout(btn_row)

        self.tabs.currentChanged.connect(self._tab_changed)
        self._tab_changed(0)

        # JSONドロップオーバーレイ
        self._json_overlay = QLabel("📄 画像(.png/.jpg/.webp) または JSON(.json) をドロップしてインポート", self)
        self._json_overlay.setAlignment(Qt.AlignCenter)
        self._json_overlay.setStyleSheet(
            "background:rgba(30,60,120,0.88);color:#7eb8f7;font-size:18px;"
            "font-weight:700;border:3px dashed #4a90d9;border-radius:12px;padding:8px"
        )
        self._json_overlay.hide()

    def _on_character_folder_changed(self, text: str):
        path = text.strip()
        if path:
            self._settings.random_character_root = path

    # ── 現在アクティブなタブの取得 ──────────────────────────────

    def _current_tab_content(self) -> CharacterTabContent | None:
        """現在選択中のタブの CharacterTabContent を返す。+タブの場合は None。"""
        if not self.tabs:
            return None
        idx = self.tabs.currentIndex()
        if idx < 0 or self._is_plus_tab(idx):
            return None
        w = self.tabs.widget(idx)
        if isinstance(w, CharacterTabContent):
            return w
        return None

    # ── 未作成LoRAグリッドクリック ──────────────────────────────

    def _on_unmade_lora_clicked(self, item_data: dict):
        """LoRAサムネイルクリック時: cm-info.jsonを読み込みフォームへ反映"""
        lora_name = item_data["name"]
        lora_path = Path(item_data["path"])
        cm_info = lora_path.parent / f"{lora_name}.cm-info.json"
        if cm_info.exists():
            if not self.import_json_path(str(cm_info)):
                return
            self._current_unmade_item = item_data
        else:
            if not self._confirm_discard_changes():
                return
            self._current_unmade_item = item_data
            tab = self._current_tab_content()
            if not tab:
                self.tabs.setCurrentIndex(0)
                tab = self._current_tab_content()
            if tab:
                tab.inp_name.setText(lora_name)
                tab.inp_lora.setPlainText(f"<lora:{lora_name}:1>")
                self._update_baseline_snapshot()

    # ── タグコールバック (TagEditorWidgetから) ───────────────────

    def _update_delete_button_state(self):
        tab = self._current_tab_content()
        if tab:
            tab._update_delete_button_state()

    def _set_edit_mode(self, enabled: bool):
        tab = self._current_tab_content()
        if tab:
            tab.btn_delete_mode.setChecked(enabled)

    # ── ダイアログ全体のドラッグ＆ドロップ ───────────────────────

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._json_overlay:
            self._json_overlay.setGeometry(20, 20, self.width() - 40, self.height() - 40)

    def dragEnterEvent(self, ev):
        if not ev.mimeData().hasUrls():
            ev.ignore(); return
        urls = [u.toLocalFile() for u in ev.mimeData().urls()]
        has_image = any(u.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp')) for u in urls)
        has_json  = any(u.lower().endswith('.json') for u in urls)
        if has_image or has_json:
            ev.acceptProposedAction()
            if has_json and self._json_overlay:
                self._json_overlay.show()
                self._json_overlay.raise_()
        else:
            ev.ignore()

    def dragLeaveEvent(self, ev):
        if self._json_overlay:
            self._json_overlay.hide()
        super().dragLeaveEvent(ev)

    def dropEvent(self, ev):
        if self._json_overlay:
            self._json_overlay.hide()
        for u in ev.mimeData().urls():
            fp = u.toLocalFile()
            low = fp.lower()
            if low.endswith('.json'):
                self.import_json_path(fp)
                ev.accept(); return
            if low.endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp')):
                tab = self._current_tab_content()
                if tab:
                    tab._set_thumb(fp)
                ev.accept(); return
        ev.ignore()

    def _show_toast(self, message: str, duration_ms: int = 1800):
        toast = QLabel(message, self)
        toast.setWordWrap(True)
        toast.setStyleSheet(
            "background:#1f2a37;color:#e6edf3;border:1px solid #3a4654;"
            "border-radius:8px;padding:10px 16px;font-size:13px;"
        )
        toast.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        max_w = max(200, self.width() - 48)
        toast.setMaximumWidth(max_w)
        toast.adjustSize()
        x = max(12, self.width() - toast.width() - 24)
        y = max(12, self.height() - toast.height() - 24)
        toast.move(x, y)
        toast.show()
        toast.raise_()
        QTimer.singleShot(duration_ms, toast.deleteLater)

    def _on_cancel_clicked(self):
        if self._confirm_discard_changes():
            self.reject()

    def _on_close_clicked(self):
        if self._confirm_discard_changes():
            self.accept()

    # ── 未保存の変更検知 ────────────────────────────────────────

    def _capture_form_snapshot(self) -> str:
        tab = self._current_tab_content()
        if tab:
            return tab.capture_snapshot()
        return ""

    def _update_baseline_snapshot(self):
        self._baseline_snapshot = self._capture_form_snapshot()

    def _confirm_discard_changes(self) -> bool:
        if self._baseline_snapshot is None:
            return True
        if self._capture_form_snapshot() == self._baseline_snapshot:
            return True
        reply = QMessageBox.question(
            self, "変更が保存されていません",
            "現在の入力内容に変更があります。\n破棄してよろしいですか？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        return reply == QMessageBox.Yes

    def import_json_path(self, path: str) -> bool:
        """cm-info.json / Civitai JSON をインポートして現在のタブへ反映する。"""
        if not self._confirm_discard_changes():
            return False
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.warning(self, "JSONエラー", f"読み込みに失敗しました:\n{e}")
            return False

        # タブが "+" の場合は最初のタブへ切り替え
        tab = self._current_tab_content()
        if not tab:
            if self.tabs:
                self.tabs.setCurrentIndex(0)
            tab = self._current_tab_content()
            if not tab:
                return False

        # キャラ名
        json_stem = Path(path).name
        for suffix in (".cm-info.json", ".json"):
            if json_stem.endswith(suffix):
                json_stem = json_stem[: -len(suffix)]
                break
        char_name = json_stem

        # LoRA stem
        remote_file = data.get("RemoteFileName") or ""
        lora_stem = Path(remote_file).stem if remote_file else json_stem

        if char_name:
            tab.inp_name.setText(str(char_name))

        if lora_stem:
            tab.inp_lora.setPlainText(f"<lora:{lora_stem}:1>")

        # トリガーワード
        trained: list[str] = [str(t).strip() for t in (data.get("TrainedWords") or []) if str(t).strip()]
        if trained:
            tab._costumes.clear()
            tab.costume_list.clear()
            tab._prev_costume_row = -1

            for i, prompt_text in enumerate(trained, 1):
                tab._costumes.append({"name": f"{i}", "prompt": prompt_text, "tags": []})
                tab.costume_list.addItem(f"{i}")

            tab.costume_list.setCurrentRow(0)
            tab._prev_costume_row = 0
            tab._load_detail(0)
            tab._update_expand_merge_state()

        tags = data.get("Tags", [])

        imported = []
        if char_name:  imported.append(f"キャラ名: {char_name}")
        if lora_stem:  imported.append(f"LoRA: <lora:{lora_stem}:1>")
        if trained:    imported.append(f"カード: {len(trained)}件")
        if tags:       imported.append(f"タグ: {len(tags)}件")
        msg = "\n".join(imported) if imported else "インポート可能なデータが見つかりませんでした"
        self._show_toast(f"JSONインポート完了\n{msg}")
        self._update_baseline_snapshot()
        return True

    # ── タブ管理 ────────────────────────────────────────────────

    def _tab_changed(self, idx: int):
        # 全タブが CharacterTabContent なので特別な表示切替は不要
        pass

    def _is_plus_tab(self, idx: int) -> bool:
        return idx == self.tabs.count() - 1

    def _add_plus_tab(self):
        plus_label = QLabel("+")
        plus_label.setAlignment(Qt.AlignCenter)
        plus_label.setStyleSheet(
            "QLabel{color:#6a7a8a;font-size:16px;font-weight:700;padding:4px 12px}"
            "QLabel:hover{color:#edf2f7}"
        )
        plus_label.setFixedWidth(40)
        plus_idx = self.tabs.addTab(plus_label, "+")
        self.tabs.setTabToolTip(plus_idx, "新しいタブを追加")
        self.tabs.tabBar().setTabData(plus_idx, "plus")
        self.tabs.tabBar().tabBarClicked.connect(self._on_tab_bar_clicked)

        class _TabBarFilter(QObject):
            def __init__(self, dialog):
                super().__init__(dialog)
                self._dialog = dialog

            def eventFilter(self, obj, event):
                if event.type() == QEvent.MouseButtonDblClick:
                    bar = self._dialog.tabs.tabBar()
                    if hasattr(event, 'position'):
                        pos = event.position().toPoint()
                    else:
                        pos = event.pos()
                    idx = bar.tabAt(pos)
                    if idx >= 0:
                        self._dialog._on_tab_double_clicked(idx)
                        return True
                if event.type() == QEvent.ContextMenu:
                    bar = self._dialog.tabs.tabBar()
                    pos = event.pos()
                    idx = bar.tabAt(pos)
                    if idx >= 0 and not self._dialog._is_plus_tab(idx):
                        self._dialog._on_tab_context_menu(idx, event.globalPos())
                        return True
                return super().eventFilter(obj, event)

        self._tab_bar_filter = _TabBarFilter(self)
        self.tabs.tabBar().installEventFilter(self._tab_bar_filter)

    def _add_custom_tab(self, name: str = "", save_path: str = ""):
        count = self.tabs.count()
        if not name:
            name = f"タブ{count}"
        tab_widget = CharacterTabContent(
            self._settings, self._repo, self._api, self._folder_map,
            default_root=save_path, parent=self,
        )
        plus_idx = count - 1
        self.tabs.insertTab(plus_idx, tab_widget, name)
        self.tabs.setCurrentIndex(plus_idx)

    def _on_tab_bar_clicked(self, idx: int):
        if idx < 0:
            return
        if self._suppress_tab_auto_add:
            return
        data = self.tabs.tabBar().tabData(idx)
        if data == "plus":
            # tabBarClicked シグナルの発火中（QTabBar自身のイベント処理中）に
            # 同じQTabBarへ insertTab() を同期実行すると、QTabBarの内部状態
            # （プレス追跡・レイアウトキャッシュ等）を再入的に書き換えてしまい、
            # Qt/PySide6側でC++レベルのクラッシュを引き起こす可能性がある。
            # そのため挿入処理はイベント処理が完全に終わった後（次のイベントループ）
            # まで遅延させる。
            self._suppress_tab_auto_add = True
            QTimer.singleShot(0, self._deferred_add_custom_tab)

    def _deferred_add_custom_tab(self):
        try:
            self._add_custom_tab()
        finally:
            self._suppress_tab_auto_add = False

    def _on_tab_double_clicked(self, idx: int):
        if idx < 0 or self._is_plus_tab(idx):
            return
        from PySide6.QtWidgets import QInputDialog
        current_name = self.tabs.tabText(idx)
        new_name, ok = QInputDialog.getText(self, "タブ名の変更", "タブ名:", text=current_name)
        if ok and new_name.strip():
            self.tabs.setTabText(idx, new_name.strip())

    def _on_tab_context_menu(self, idx: int, global_pos):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        rename_action = menu.addAction("名前を変更")
        delete_action = menu.addAction("タブを削除")
        action = menu.exec(global_pos)
        if action == rename_action:
            self._on_tab_double_clicked(idx)
        elif action == delete_action:
            self._remove_custom_tab(idx)

    def _remove_custom_tab(self, idx: int):
        if idx < 0 or self._is_plus_tab(idx) or idx == 0:
            return
        tab_name = self.tabs.tabText(idx)
        reply = QMessageBox.question(
            self, "タブの削除",
            f"タブ「{tab_name}」を削除しますか？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        self._suppress_tab_auto_add = True
        try:
            self.tabs.removeTab(idx)
            new_idx = self.tabs.currentIndex()
            if self._is_plus_tab(new_idx):
                self.tabs.setCurrentIndex(max(0, new_idx - 1))
        finally:
            self._suppress_tab_auto_add = False

    # ── タブ設定の永続化 ────────────────────────────────────────

    def get_custom_tabs(self) -> list[dict]:
        """現在のタブ構成から保存すべきタブ設定を返す。

        戻り値: [{"name": "タブ名", "save_path": "/path/to/folder"}, ...]
        index 0 (キャラクター) は常に自動生成されるため含めない。

        注意: このメソッドは self.tabs（QTabWidget）に直接アクセスするため、
        WA_DeleteOnClose によりダイアログのC++実体が既に破棄された後に
        呼び出すと RuntimeError（libshiboken: already deleted）になる。
        ダイアログが閉じた後の値が必要な場合は、このメソッドではなく
        accept()/reject() 実行時にスナップショットされる
        self.custom_tabs_result を参照すること。
        """
        result: list[dict] = []
        if not self.tabs:
            return result
        try:
            for idx in range(self.tabs.count()):
                if idx == 0:  # キャラクタータブはスキップ（毎回自動生成）
                    continue
                if self._is_plus_tab(idx):
                    continue
                w = self.tabs.widget(idx)
                if isinstance(w, CharacterTabContent):
                    result.append({
                        "name": self.tabs.tabText(idx),
                        "save_path": w.get_save_path(),
                    })
        except RuntimeError:
            # C++側が既に破棄されている場合は、直前に取得済みのスナップショット
            # （custom_tabs_result）にフォールバックする。
            return list(self.custom_tabs_result)
        return result

    def _snapshot_custom_tabs(self) -> None:
        """self.tabsがまだ生きている間にタブ設定をプレーンなPython属性へ保存する。"""
        try:
            self.custom_tabs_result = self.get_custom_tabs()
        except RuntimeError:
            pass

    def accept(self) -> None:
        # WA_DeleteOnClose により super().accept() の呼び出し後は
        # self.tabs のC++実体が破棄されている可能性があるため、
        # 呼び出す前に必ずスナップショットを取っておく。
        self._snapshot_custom_tabs()
        super().accept()

    def reject(self) -> None:
        # キャンセルボタン・×ボタン・Escキーは最終的にすべてここを通る。
        self._snapshot_custom_tabs()
        super().reject()

    # ── 作成 ────────────────────────────────────────────────────

    def _do_create(self):
        if not self.tabs:
            return
        idx = self.tabs.currentIndex()
        if self._is_plus_tab(idx):
            return
        tab = self._current_tab_content()
        if not tab:
            return
        entries = tab.create_entries()
        if entries:
            self.created_entries.extend(entries)
            if self._unmade_grid is not None:
                if self._current_unmade_item:
                    self._unmade_grid.remove_by_name(self._current_unmade_item["name"])
                    self._current_unmade_item = None
                tab.clear_form()
                self._update_baseline_snapshot()
                if self._unmade_grid.is_empty():
                    QMessageBox.information(self, "完了", "すべての未作成LoRAの処理が完了しました。")
            else:
                self.accept()
