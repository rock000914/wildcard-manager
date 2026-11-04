"""ツールバー用の線画アイコン群。

絵文字（📁🎲など）はフォールバック先が環境依存で、環境によっては豆腐になるうえ
色付き絵文字はダークUIで浮いて見える。代わりに 16px のストローク記号を描画する。
2倍解像度で描いて devicePixelRatio を後付けするので、高DPIでもにじまない。
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QSize, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

from . import theme

ICON_PX = 16
_SCALE = 2
_STROKE = theme.ICON_STROKE
_BAND = theme.ICON_BAND
_MUTED = theme.ICON_MUTED


def icon_text_size() -> QSize:
    """アイコン+テキストボタンで setIconSize に渡す矩形（右に透明帯を含む）。"""
    return QSize(ICON_PX + theme.ICON_TEXT_PAD, ICON_PX)


def _begin(width: int = ICON_PX) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(width * _SCALE, ICON_PX * _SCALE)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(_SCALE, _SCALE)
    pen = QPen(QColor(_STROKE))
    pen.setWidthF(1.35)
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    return pm, painter


def _finish(pm: QPixmap, painter: QPainter) -> QIcon:
    painter.end()
    pm.setDevicePixelRatio(_SCALE)
    return QIcon(pm)


def _canvas_width(text_gap: bool) -> int:
    """テキスト併用ボタンはキャンバスを右に広げ、図は左に据えたまま余白にする。"""
    return ICON_PX + (theme.ICON_TEXT_PAD if text_gap else 0)


def _diamond(painter: QPainter, cx: float, cy: float, rx: float, ry: float) -> None:
    painter.drawPolygon(QPolygonF([
        QPointF(cx, cy - ry),
        QPointF(cx + rx, cy),
        QPointF(cx, cy + ry),
        QPointF(cx - rx, cy),
    ]))


def icon_folder(text_gap: bool = False) -> QIcon:
    pm, painter = _begin(_canvas_width(text_gap))
    painter.drawRoundedRect(QRectF(1.6, 4.4, 12.8, 8.6), 1.6, 1.6)
    painter.drawPolyline(QPolygonF([
        QPointF(1.6, 4.4),
        QPointF(1.6, 2.9),
        QPointF(5.4, 2.9),
        QPointF(6.8, 4.4),
    ]))
    return _finish(pm, painter)


def icon_lora(text_gap: bool = False) -> QIcon:
    """LoRA=重ねるもの。レイヤースタック（ひし形2枚＋下線）。"""
    pm, painter = _begin(_canvas_width(text_gap))
    _diamond(painter, 8.0, 4.6, 5.4, 2.7)
    painter.drawPolyline(QPolygonF([
        QPointF(2.6, 8.6),
        QPointF(8.0, 11.3),
        QPointF(13.4, 8.6),
    ]))
    painter.drawPolyline(QPolygonF([
        QPointF(2.6, 11.6),
        QPointF(8.0, 14.3),
        QPointF(13.4, 11.6),
    ]))
    return _finish(pm, painter)


def icon_new_card(text_gap: bool = False) -> QIcon:
    """ドキュメント＋プラス。"""
    pm, painter = _begin(_canvas_width(text_gap))
    painter.drawPolyline(QPolygonF([
        QPointF(9.4, 2.0),
        QPointF(4.2, 2.0),
        QPointF(4.2, 14.0),
        QPointF(11.8, 14.0),
        QPointF(11.8, 4.4),
        QPointF(9.4, 2.0),
    ]))
    painter.drawPolyline(QPolygonF([
        QPointF(9.4, 2.0),
        QPointF(9.4, 4.4),
        QPointF(11.8, 4.4),
    ]))
    painter.drawLine(QPointF(8.0, 7.4), QPointF(8.0, 11.2))
    painter.drawLine(QPointF(6.1, 9.3), QPointF(9.9, 9.3))
    return _finish(pm, painter)


def icon_random(text_gap: bool = False) -> QIcon:
    """サイコロの目。ランダム生成の意味がそのまま伝わる。"""
    pm, painter = _begin(_canvas_width(text_gap))
    painter.drawRoundedRect(QRectF(2.2, 2.2, 11.6, 11.6), 2.6, 2.6)
    painter.setBrush(QColor(_STROKE))
    painter.setPen(Qt.NoPen)
    for cx, cy in ((5.3, 5.3), (8.0, 8.0), (10.7, 10.7)):
        painter.drawEllipse(QPointF(cx, cy), 1.05, 1.05)
    return _finish(pm, painter)


def icon_settings() -> QIcon:
    """歯車。外周の歯を8枚、中心に穴。"""
    pm, painter = _begin()
    painter.save()
    painter.translate(8.0, 8.0)
    path = QPainterPath()
    teeth = 8
    r_out, r_in = 6.1, 4.5
    for i in range(teeth):
        a0 = math.radians(i * 360 / teeth)
        a1 = math.radians(i * 360 / teeth + 17)
        a2 = math.radians(i * 360 / teeth + 28)
        a3 = math.radians((i + 1) * 360 / teeth)
        pts = (
            (math.cos(a0) * r_in, math.sin(a0) * r_in),
            (math.cos(a1) * r_out, math.sin(a1) * r_out),
            (math.cos(a2) * r_out, math.sin(a2) * r_out),
            (math.cos(a3) * r_in, math.sin(a3) * r_in),
        )
        if i == 0:
            path.moveTo(*pts[0])
        else:
            path.lineTo(*pts[0])
        for px, py in pts[1:]:
            path.lineTo(px, py)
    path.closeSubpath()
    painter.setPen(QPen(QColor(_STROKE)))
    painter.drawPath(path)
    painter.drawEllipse(QPointF(0, 0), 2.1, 2.1)
    painter.restore()
    return _finish(pm, painter)


def icon_panel(side: str = "left", visible: bool = True) -> QIcon:
    """パネル開閉の標準的な表記: 枠の中に「その位置の帯」を塗り、
    隠れているときは帯をアウトラインだけにして色でも状態を示す。
    （VS Code / Figma / Obsidian の sidebar トグルと同じ系譜）
    """
    pm, painter = _begin()
    outer = QRectF(1.9, 2.9, 12.2, 10.2)
    band_w = 4.0
    band = QRectF(outer.left(), outer.top(), band_w, outer.height()) if side == "left" \
        else QRectF(outer.right() - band_w, outer.top(), band_w, outer.height())
    pen = QPen(QColor(_STROKE if visible else _MUTED))
    pen.setWidthF(1.35)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(outer, 2.0, 2.0)
    if visible:
        inset = QRectF(band.left() + 1.0, band.top() + 1.0, band.width() - 1.0, band.height() - 2.0)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(_BAND))
        painter.drawRoundedRect(inset, 1.2, 1.2)
    else:
        # 隠れている状態も「どちらのパネルか」が残るよう、帯は輪郭だけを描く
        # （点線1本は16pxで消えて左右が判別できない）。
        dash = QPen(QColor(_MUTED))
        dash.setStyle(Qt.DotLine)
        dash.setWidthF(1.0)
        painter.setPen(dash)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(QRectF(band.left() + 0.8, band.top() + 1.4,
                                band.width() - 0.8, band.height() - 2.8))
    return _finish(pm, painter)


def icon_star(filled: bool = False) -> QIcon:
    """お気に入り。★文字はフォールバックが環境依存なので図形で描く。"""
    pm, painter = _begin()
    cx, cy, r_out, r_in = 8.0, 8.4, 5.9, 2.5
    pts = []
    for i in range(10):
        r = r_out if i % 2 == 0 else r_in
        a = math.radians(-90 + i * 36)
        pts.append(QPointF(cx + r * math.cos(a), cy + r * math.sin(a)))
    pen = QPen(QColor(_STROKE))
    pen.setWidthF(1.2)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QColor(_STROKE) if filled else Qt.NoBrush)
    painter.drawPolygon(QPolygonF(pts))
    return _finish(pm, painter)


def icon_home() -> QIcon:
    """ライブラリルート（all）。塗りシルエットの家。

    線画（屋根＋箱）は16pxで記号として潰れ「合」のような文字glyphに
    見える（ユーザー指摘「名前がわからない」）。塗り＋扉の抜くりで
    一目で家と読める形にする。
    """
    pm, painter = _begin()
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(_STROKE))
    painter.drawPolygon(QPolygonF([
        QPointF(8.0, 2.2),
        QPointF(14.2, 7.8),
        QPointF(12.3, 7.8),
        QPointF(12.3, 13.6),
        QPointF(3.7, 13.6),
        QPointF(3.7, 7.8),
        QPointF(1.8, 7.8),
    ]))
    painter.setCompositionMode(QPainter.CompositionMode_Source)
    painter.setBrush(QColor(0, 0, 0, 0))
    painter.drawRoundedRect(QRectF(6.5, 9.3, 3.0, 4.3), 0.8, 0.8)
    painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
    return _finish(pm, painter)
