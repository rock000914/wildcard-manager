from __future__ import annotations

from PySide6.QtCore import Qt, QSize, QPoint, QRect, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QLayout,
)

from . import theme


def copy_tag_to_clipboard(tag: str) -> None:
    clipboard = QApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(tag)


class FlowLayout(QLayout):
    """A layout that wraps widgets like words in a paragraph."""

    def __init__(self, parent=None, h_spacing: int = 4, v_spacing: int = 4):
        super().__init__(parent)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._items: list = []

    def addItem(self, item) -> None:
        self._items.append(item)
        # addWidget() は内部でこの addItem() を呼ぶだけで、Qtに対して
        # 「レイアウトが変わったので再計算が必要」と知らせる invalidate() を
        # 自動では呼ばない。これを怠ると、QScrollArea(setWidgetResizable(True))
        # 側がコンテンツサイズの変化に気づかず再配置されない
        # （ウィンドウをリサイズした時だけ setGeometry() 経由で正しく表示される）
        # という不具合になるため、明示的に invalidate() してレイアウト要求を発行する。
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            self.invalidate()
            return item
        return None

    def removeWidget(self, widget) -> None:
        for i, item in enumerate(self._items):
            if item.widget() is widget:
                self._items.pop(i)
                self.invalidate()
                return

    def expandingDirections(self):
        return Qt.Orientations()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), dry_run=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, dry_run=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, dry_run: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y = effective.x(), effective.y()
        row_height = 0
        for item in self._items:
            w = item.widget()
            if w is not None and not w.isVisible():
                if not dry_run:
                    w.move(-9999, -9999)
                continue
            hint = w.size() if (w is not None and w.size().isValid() and w.size().width() > 0) else (w.sizeHint() if w is not None else item.sizeHint())
            next_x = x + hint.width()
            if next_x > effective.right() + 1 and row_height > 0:
                x = effective.x()
                y += row_height + self._v_spacing
                next_x = x + hint.width()
                row_height = 0
            if not dry_run and w is not None:
                w.move(x, y)
            x = next_x + self._h_spacing
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + margins.bottom()


class TagChip(QWidget):
    doubleClicked = Signal(str)
    clicked = Signal(str)
    rightClicked = Signal(str, QPoint)
    copied = Signal(str)

    # 語の高さに足される分: layout の上下 3+3 と QSS の枠 1+1
    _BOX_EXTRA = 8

    def __init__(self, tag: str, parent=None):
        super().__init__(parent)
        self.tag = tag
        self._selected = False
        self._select_mode = False
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(8, 3, 8, 3)
        self._layout.setSpacing(4)
        self._layout.setSizeConstraint(QLayout.SetFixedSize)

        self.label = QLabel(tag)
        self.label.setWordWrap(False)
        # フォントメトリクスだけだと 18px まで潰れる環境がある（仕様はタグ 22〜26px）。
        # 面の高さは theme.CHIP_HEIGHT で固定して、行の高さ計算 _chip_height() と揃える。
        self.label.setMinimumHeight(theme.CHIP_HEIGHT - self._BOX_EXTRA)
        self._update_tooltip()

        self._layout.addWidget(self.label)

        self._update_style()

    def mouseDoubleClickEvent(self, event) -> None:
        if not self._select_mode:
            self.doubleClicked.emit(self.tag)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        if self._select_mode:
            self._selected = not self._selected
            self._update_style()
        else:
            self.copied.emit(self.tag)
        self.clicked.emit(self.tag)

    def contextMenuEvent(self, event) -> None:
        # PySide6 の QContextMenuEvent は globalPos() を公開（globalPosition() は無い）
        self.rightClicked.emit(self.tag, event.globalPos())

    def set_tag(self, tag: str) -> None:
        self.tag = tag
        self.label.setText(tag)

    def set_select_mode(self, enabled: bool) -> None:
        self._select_mode = enabled
        if not enabled:
            self._selected = False
        self._update_tooltip()
        self._update_style()

    def _update_tooltip(self) -> None:
        action = "クリックで選択" if self._select_mode else "クリックでコピー"
        self.setToolTip(f"{action}・右クリックで編集／コピー／検索")

    def is_selected(self) -> bool:
        return self._selected

    def _is_lora_tag(self) -> bool:
        """LoRAタグかどうかを判定"""
        tag = self.tag.strip()
        return tag.startswith("<lora:") or tag.startswith("<lyco:")

    def _update_style(self) -> None:
        if self._selected:
            bg, border, text_color = theme.DANGER_BG, theme.DANGER, theme.DANGER_TEXT
        elif self._is_lora_tag():
            # LoRAタグ: OK色。面は暗く語だけ明るく（仕様③④の「地味・小型」）
            bg, border, text_color = theme.OK_BG, theme.OK_BORDER, theme.OK
        else:
            bg, border, text_color = theme.SURFACE_RAISED, theme.BORDER, theme.TEXT
        self.setStyleSheet(
            f"""
            TagChip {{
                background: {bg};
                border: 1px solid {border};
                border-radius: {theme.RADIUS_CHIP}px;
            }}
            TagChip QLabel {{
                color: {text_color};
                font-size: {theme.SMALL_SIZE + 1}px;
                background: transparent;
                border: none;
            }}
            """
        )


class TagEditorWidget(QWidget):
    """タグチップエディタ。

    M2 修正: 以前は ``parent = self.window()`` で親をたぐり、
    ``hasattr(parent, "_update_delete_button_state")`` で親の実装を
    暗黙に仮定していた。再利用性・テスト容易性が低いため、
    ``selectionChanged`` シグナルを新設して親側で接続する形式に変更。
    後方互換のため旧 hasattr フォールバックも残す。
    """
    tagsChanged = Signal()
    selectionChanged = Signal(bool)  # 選択状態の有無が変わった時に発火
    searchRequested = Signal(str)    # 右クリックメニュー「検索」: 語を親の検索欄へ渡す
    tagCopied = Signal(str)          # クリック/メニューでのコピー完了。表示フィードバックは親側

    _CHIP_H_SPACING = 4
    _CHIP_V_SPACING = 4
    _AREA_PADDING = 6
    _AREA_MIN_ROWS = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._editable = False
        self._select_mode = False
        self._chips: list[TagChip] = []
        self._tag_set: set[str] = set()
        self._overflow_label: QLabel | None = None
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self.help_label = QLabel("タグ一覧")
        self.help_label.setStyleSheet(f"QLabel {{ {theme.heading()} padding: 0 2px 4px 2px; }}")

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setStyleSheet(
            "QScrollArea {"
            " background: transparent;"
            " border: none;"
            "}"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { width: 6px; background: transparent; }"
            "QScrollBar::handle:vertical { background: " + theme.SURFACE_RAISED + "; border-radius: 3px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )

        # QScrollArea自体にborderを付けるとviewportの配置計算とQSSの枠線幅が
        # 一致せず、特に下端で枠線がviewportの背景に隠れてしまう(Qtの既知の挙動)。
        # そのため枠線専用のQFrameでスクロールエリアをラップし、描画を分離する。
        self._BORDER_WIDTH = 3
        self.border_frame = QFrame()
        self.border_frame.setObjectName("tagBorderFrame")
        self.border_frame.setStyleSheet(
            "#tagBorderFrame {"
            f" background: {theme.PANEL_MAIN};"
            f" border: {self._BORDER_WIDTH}px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS_PANEL}px;"
            "}"
        )
        border_layout = QVBoxLayout(self.border_frame)
        border_layout.setContentsMargins(
            self._BORDER_WIDTH, self._BORDER_WIDTH, self._BORDER_WIDTH, self._BORDER_WIDTH
        )
        border_layout.setSpacing(0)
        border_layout.addWidget(self.scroll_area)

        self.flow_container = QWidget()
        self.flow_container.setStyleSheet("background: transparent;")
        self.flow_layout = FlowLayout(self.flow_container,
                                      h_spacing=self._CHIP_H_SPACING,
                                      v_spacing=self._CHIP_V_SPACING)
        self.flow_layout.setContentsMargins(self._AREA_PADDING, self._AREA_PADDING,
                                            self._AREA_PADDING, self._AREA_PADDING)
        self.scroll_area.setWidget(self.flow_container)

        self.scroll_area.setFixedHeight(self._area_height() - self._BORDER_WIDTH * 2)

        self.empty_label = QLabel("タグなし")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent;")
        self.empty_label.setParent(self.scroll_area)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        outer.addWidget(self.border_frame)

        self._refresh_empty_state()

    def _chip_height(self) -> int:
        metrics = QFontMetrics(self.font())
        return max(theme.CHIP_HEIGHT, metrics.height() + self._AREA_PADDING * 2)

    def _area_height(self) -> int:
        ch = self._chip_height()
        rows = self._AREA_MIN_ROWS
        return rows * ch + (rows - 1) * self._CHIP_V_SPACING + self._AREA_PADDING * 2 + 6

    def sizeHint(self) -> QSize:
        return QSize(320, self.help_label.sizeHint().height() + self._area_height() + 2)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.empty_label.setGeometry(self.scroll_area.viewport().rect())

    MAX_CHIPS = 300

    def set_tags(self, tags: list[str]) -> None:
        for chip in self._chips:
            self.flow_layout.removeWidget(chip)
            chip.deleteLater()
        self._chips.clear()
        self._tag_set.clear()
        if self._overflow_label is not None:
            self._overflow_label.deleteLater()
            self._overflow_label = None
        for tag in tags[:self.MAX_CHIPS]:
            self._append_tag(tag)
        overflow = len(tags) - self.MAX_CHIPS
        if overflow > 0:
            self._overflow_label = QLabel(f"...他 {overflow} タグ", self.flow_container)
            self._overflow_label.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-size: {theme.SMALL_SIZE}px; background: transparent; border: none;"
            )
            self.flow_layout.addWidget(self._overflow_label)
        self._refresh_empty_state()
        self.tagsChanged.emit()

    def tags(self) -> list[str]:
        return [chip.tag for chip in self._chips]

    def set_editable(self, editable: bool) -> None:
        self._editable = editable
        if not editable:
            self._exit_select_mode()
        else:
            self._enter_select_mode()

    def set_delete_mode_border(self, active: bool) -> None:
        """削除モードに応じてタグエリアの枠線色を変更する"""
        if active:
            border_color = theme.DANGER
        else:
            border_color = theme.BORDER
        self.border_frame.setStyleSheet(
            "#tagBorderFrame {"
            f" background: {theme.PANEL_MAIN};"
            f" border: {self._BORDER_WIDTH}px solid {border_color};"
            f" border-radius: {theme.RADIUS_PANEL}px;"
            "}"
        )

    def _append_tag(self, tag: str) -> None:
        clean = tag.strip()
        if not clean or clean in self._tag_set:
            return
        chip = TagChip(clean)
        chip.doubleClicked.connect(self._edit_chip)
        chip.clicked.connect(self._on_chip_clicked)
        chip.rightClicked.connect(self._show_chip_menu)
        chip.copied.connect(self._on_chip_copied)
        self.flow_layout.addWidget(chip)
        self._chips.append(chip)
        self._tag_set.add(clean)
        chip.set_select_mode(self._select_mode)
        chip.show()
        # NOTE: addWidget() 内の addItem() が呼ぶ invalidate() は、この時点では
        # まだ chip.show() 前なので isVisible()==False。FlowLayout._do_layout() は
        # 「まだ表示されていないウィジェット」を (-9999, -9999) へ移動してスキップする
        # 仕様のため、show() 前に走るレイアウトパスでは新規チップが画面外に飛ばされて
        # しまう。show() の後にもう一度 invalidate() して、表示済み状態で
        # 正しく再レイアウトされるようにする。
        self.flow_layout.invalidate()
        self._refresh_empty_state()
        self.tagsChanged.emit()

    def _on_chip_clicked(self, tag: str) -> None:
        # M2 修正: 親ウィンドウの実装を hasattr で仮定する代わりに
        # selectionChanged シグナルで通知する。後方互換のため旧経路も残す。
        self.selectionChanged.emit(self.has_selected_tags())
        parent = self.window()
        if hasattr(parent, "_update_delete_button_state"):
            parent._update_delete_button_state()

    def _remove_tag(self, tag: str) -> None:
        if not self._editable:
            parent = self.window()
            if hasattr(parent, "_set_edit_mode"):
                parent._set_edit_mode(True)
        for chip in list(self._chips):
            if chip.tag == tag:
                self.flow_layout.removeWidget(chip)
                chip.deleteLater()
                self._chips.remove(chip)
                self._tag_set.discard(tag)
                self._refresh_empty_state()
                self.tagsChanged.emit()
                return

    def _enter_select_mode(self) -> None:
        self._select_mode = True
        for chip in self._chips:
            chip.set_select_mode(True)

    def _exit_select_mode(self) -> None:
        self._select_mode = False
        for chip in self._chips:
            chip.set_select_mode(False)

    def _delete_selected(self) -> None:
        selected_tags = [chip.tag for chip in self._chips if chip.is_selected()]
        if not selected_tags:
            return
        for tag in selected_tags:
            for chip in list(self._chips):
                if chip.tag == tag:
                    self.flow_layout.removeWidget(chip)
                    chip.deleteLater()
                    self._chips.remove(chip)
                    self._tag_set.discard(tag)
                    break
        self._refresh_empty_state()
        self.tagsChanged.emit()
        self.selectionChanged.emit(False)

    def _edit_chip(self, tag: str) -> None:
        if not self._editable:
            parent = self.window()
            if hasattr(parent, "_set_edit_mode"):
                parent._set_edit_mode(True)
            else:
                self.set_editable(True)
        text, ok = QInputDialog.getText(self, "タグ編集", "タグ名", text=tag)
        if not ok:
            return
        updated = text.strip()
        if not updated:
            self._remove_tag(tag)
            return
        if updated != tag and updated in self._tag_set:
            return
        for chip in self._chips:
            if chip.tag == tag:
                self._tag_set.discard(tag)
                chip.set_tag(updated)
                self._tag_set.add(updated)
                break
        self.tagsChanged.emit()

    def _show_chip_menu(self, tag: str, pos: QPoint) -> None:
        menu = QMenu(self)
        act_edit = menu.addAction("編集")
        act_copy = menu.addAction("コピー")
        act_search = menu.addAction("検索")
        chosen = menu.exec(pos)
        if chosen is act_edit:
            self._edit_chip(tag)
        elif chosen is act_copy:
            self._on_chip_copied(tag)
        elif chosen is act_search:
            self.searchRequested.emit(tag)

    def _on_chip_copied(self, tag: str) -> None:
        copy_tag_to_clipboard(tag)
        self.tagCopied.emit(tag)

    def has_selected_tags(self) -> bool:
        return any(chip.is_selected() for chip in self._chips)

    def delete_selected_tags(self) -> None:
        self._delete_selected()

    def _refresh_empty_state(self) -> None:
        has_tags = bool(self._chips)
        self.empty_label.setVisible(not has_tags)
        if not has_tags:
            self.empty_label.setGeometry(self.scroll_area.viewport().rect())
