"""Widget classes extracted from main_window.py."""

from __future__ import annotations

from PySide6.QtCore import (
    Qt,
    QTimer,
    QPoint,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QPropertyAnimation,
    QEasingCurve,
)
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QGraphicsOpacityEffect,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListView,
    QVBoxLayout,
    QWidget,
)

from . import theme

# main_window の寸法と必ず揃える（別々に持つとツールバー行の高さがずれる）
TOOLBAR_BUTTON_SIZE = theme.ICON_BUTTON_SIZE


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event) -> None:
        event.ignore()


class ApiStatusIndicator(QWidget):
    """API接続状態を示すミニマルインジケーター。「API ●」形式。"""

    _LAMP_COLORS = {
        "off":        theme.DANGER_TEXT,
        "connected":  theme.OK,
        "generating": theme.WARNING,
    }
    _STATE_TOOLTIPS = {
        "off":        "API: 切断",
        "connected":  "API: 接続済",
        "generating": "API: 生成中",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "off"
        self.setStyleSheet(f"""
            QFrame#apiIndicator {{
                background: {theme.PANEL_MAIN};
                border: 1px solid {theme.BORDER};
                border-radius: {theme.RADIUS_CHIP}px;
            }}
        """)
        container = QFrame()
        container.setObjectName("apiIndicator")
        container.setFixedHeight(TOOLBAR_BUTTON_SIZE)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(6)

        self._label = QLabel("API")
        # アプリ全体の QWidget{background} 指定がラベルに効いて pill 色とずれるため、透過で抜く
        self._label.setStyleSheet(
            f"background: transparent; color: {theme.TEXT_MUTED};"
            f" font: 600 {theme.SMALL_SIZE + 1}px 'Segoe UI';"
        )
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
            self._lamp.setStyleSheet(f"background: {theme.APP_BG}; border-radius: 5px;")


class ResponsivePixmapLabel(QLabel):
    """リサイズ対応・角丸表示のサムネイルラベル。"""

    _CORNER_RADIUS = theme.RADIUS_CARD

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._base_pixmap: QPixmap | None = None

    def setPixmap(self, pixmap: QPixmap | None) -> None:  # type: ignore[override]
        self._base_pixmap = pixmap
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update()

    def paintEvent(self, event) -> None:
        if self._base_pixmap is None or self._base_pixmap.isNull():
            super().paintEvent(event)
            return
        cr = self.contentsRect()
        if cr.width() <= 0 or cr.height() <= 0:
            return
        scaled = self._base_pixmap.scaled(cr.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        x = cr.x() + (cr.width() - scaled.width()) // 2
        y = cr.y() + (cr.height() - scaled.height()) // 2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, scaled.width(), scaled.height()), self._CORNER_RADIUS, self._CORNER_RADIUS)
        painter.setClipPath(path)
        painter.drawPixmap(x, y, scaled)
        painter.end()


from PySide6.QtCore import QRectF  # noqa: E402


class SlowWheelListView(QListView):
    """QListView whose mouse-wheel scroll step is reduced for finer scrolling."""

    def __init__(self, scroll_step: int = 30, parent=None):
        super().__init__(parent)
        self.scroll_step = scroll_step
        self._anchor_row: int | None = None
        self._suppress_native_mouse: bool = False

    def setModel(self, model) -> None:
        super().setModel(model)
        self._anchor_row = None

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        bar = self.verticalScrollBar()
        notches = delta / 120.0
        bar.setValue(bar.value() - int(notches * self.scroll_step))
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            index = self.indexAt(event.pos())
            if index.isValid():
                if event.modifiers() & Qt.ShiftModifier:
                    if self._anchor_row is not None:
                        self._select_range(index)
                        self._suppress_native_mouse = True
                        event.accept()
                        return
                elif not (event.modifiers() & Qt.ControlModifier):
                    self._anchor_row = index.row()
        self._suppress_native_mouse = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._suppress_native_mouse:
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._suppress_native_mouse:
            self._suppress_native_mouse = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _select_range(self, target: QModelIndex) -> None:
        anchor_row = self._anchor_row
        if anchor_row is None:
            return
        model = self.model()
        if model is None:
            return
        target_row = target.row()
        if target_row < 0:
            return

        start_row = min(anchor_row, target_row)
        end_row = max(anchor_row, target_row)

        selection = QItemSelection(model.index(start_row, 0), model.index(end_row, 0))
        sel_model = self.selectionModel()
        sel_model.select(selection, QItemSelectionModel.ClearAndSelect)
        sel_model.setCurrentIndex(target, QItemSelectionModel.NoUpdate)


class ToastPopup(QFrame):
    # 出現はフェードイン＋下からの短いスライド、消えるときはフェードアウト。
    # 子ウィジェットなので windowOpacity は使えず、QGraphicsOpacityEffect で操作する
    _FADE_IN_MS = 140
    _FADE_OUT_MS = 240
    _SLIDE_PX = 12

    def __init__(self, parent: QWidget, message: str):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.SubWindow)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setObjectName("toastPopup")
        self.setStyleSheet(
            f"""
            QFrame#toastPopup {{
                background: rgba(27, 37, 51, 235);
                border: 1px solid {theme.BORDER};
                border-radius: {theme.RADIUS_PANEL}px;
            }}
            QLabel {{
                color: {theme.TEXT};
                font-size: {theme.FONT_SIZE}px;
                padding: 10px 14px;
            }}
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(message)
        layout.addWidget(label)
        self.adjustSize()
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._anims: list[QPropertyAnimation] = []

    def show_for(self, ms: int = 2600) -> None:
        parent = self.parentWidget()
        if parent is None:
            self.show()
            QTimer.singleShot(ms, self.close)
            return
        x = max(12, (parent.width() - self.width()) // 2)
        y = max(12, parent.height() - self.height() - 36)
        self.move(x, y + self._SLIDE_PX)
        self.raise_()
        self.show()
        fade_in = self._start(self._opacity, b"opacity", 0.0, 1.0, self._FADE_IN_MS, QEasingCurve.Type.OutCubic)
        slide = QPropertyAnimation(self, b"pos", self)
        slide.setDuration(self._FADE_IN_MS)
        slide.setStartValue(self.pos())
        slide.setEndValue(QPoint(x, y))
        slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anims.append(slide)
        slide.start()
        QTimer.singleShot(ms, lambda: self._fade_out((fade_in, slide)))

    def _fade_out(self, running) -> None:
        for a in running:
            a.stop()
        # フェードイン途中の割込みでは現在値から減衰させる（1.0へ戻すとカクつく）
        fade_out = self._start(self._opacity, b"opacity", self._opacity.opacity(), 0.0,
                               self._FADE_OUT_MS, QEasingCurve.Type.InQuad)
        fade_out.finished.connect(self.close)

    def _start(self, target, prop, start: float, end: float, duration: int, easing: QEasingCurve.Type) -> QPropertyAnimation:
        anim = QPropertyAnimation(target, prop, self)
        anim.setDuration(duration)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(easing)
        self._anims.append(anim)
        anim.start()
        return anim
