from __future__ import annotations

import math
import os
import re
import time
from collections import OrderedDict
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QRectF, QSize, Qt, QThreadPool, QTimer, Slot, QObject, QPropertyAnimation, QEasingCurve, Property
from PySide6.QtGui import QFont, QFontMetrics, QIcon, QImage, QPainter, QColor, QPainterPath, QPen, QPixmap, QTextLayout
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem, QStyledItemDelegate

from . import theme
from .models import WildcardEntry
from .repository import build_thumbnail_gallery_index, thumbnail_folder_listing
from .thumbnail_workers import (ThumbnailLoadSignals, ThumbnailLoadTask,
                                decode_cover_image)

CARD_FRAME_PADDING = 14

# 2枚目以降へめくるアニメーションの1回ぶん時間。短すぎると「切り替わった」
# ことが読めず、長すぎると連続ダブルクリックが詰まる。
FLIP_DURATION_MS = 340


def rel_folder_of(rel_path: str) -> str:
    """rel_path（POSIX）の親フォルダ。直下は "" に揃える（"" と "." の二表記防止）。"""
    parent = PurePosixPath(rel_path).parent.as_posix()
    return "" if parent == "." else parent


def thumbnail_paths_equal(a, b) -> bool:
    r"""サムネイル実パスが同一ファイルを指すかの判定。

    Windows はパスの大小文字と区切り記号が違っても同じファイルなので、`==` や
    `in` で素直に比べると重複として扱う。DB側は `H:\...`、スキャン側は設定由来の
    `h:\...` になっており、実データで実際にズレた（1枚だけなのに `1/2` のピルが
    出た）。枚数カウントと「表示中」マークはすべてこの関数で比べる。
    """
    if not a or not b:
        return False
    return os.path.normcase(str(a)) == os.path.normcase(str(b))

# LoRA名の照合キーから不可視文字を落とすためのパターン。
# ファイル名に BOM(U+FEFF) やゼロ幅スペースが混入した実体があると、小文字化するだけでは
# 一致せず「LoRA削除済み」の誤判定になる（実測例: `﻿mai_cammysf2.safetensors`）。
LORA_NAME_NOISE_RE = re.compile("[\ufeff\u200b\u200c\u200d\u2060\u200e\u200f]")


def lora_name_key(raw: object) -> str:
    """`<lora:x>` の名前とファイル名の突合に使う正規化キーを返す。"""
    return LORA_NAME_NOISE_RE.sub("", str(raw or "")).strip().lower()

# LoRA BaseModel → サムネイル左上バッジの表示名と背景色。
# new_wildcard_dialog.LoraCardWidget._BASE_MODEL_COLORS と同系色を使う。
BASE_MODEL_BADGE_LABELS: dict[str, str] = {
    "illustrious": "ill",
    "anima": "ani",
}
BASE_MODEL_BADGE_COLORS: dict[str, str] = {
    "illustrious": "#2f6fed",
    "anima": "#c243c2",
    "mix": "#e11d48",
}
# 参照LoRAがディスクから消えたカードの表示。サムネイルをモノクロ化して区別する。
LOST_BADGE_LABEL = "LOST"
LOST_BADGE_COLOR = theme.LOST
# モノクロ化と同時にこれだけ沈める。1.0だと彩色だけ消えて亮度がそのまま残り、
# 暗いカード背景(theme.CARD_BG)の上で「死んでる」ことが分かりにくい。
LOST_CARD_DIM = 0.72


def entry_base_badge(lora_names: list[str] | tuple[str, ...] | None,
                     base_map: dict[str, str] | None) -> tuple[str, str] | None:
    """ill/aniバッジの (表示テキスト, 背景色) を返す。不要なら None。

    base_map は {lora名.lower(): BaseModel}。cm-info.json 由来。
    ill系とani系の両方を含む場合は ("mix", 赤系) を返す。
    """
    if not lora_names or not base_map:
        return None
    kinds: set[str] = set()
    for name in lora_names:
        bm = (base_map.get(lora_name_key(name)) or "").strip().lower()
        if bm in BASE_MODEL_BADGE_LABELS:
            kinds.add(bm)
    if kinds == {"illustrious"}:
        return (BASE_MODEL_BADGE_LABELS["illustrious"], BASE_MODEL_BADGE_COLORS["illustrious"])
    if kinds == {"anima"}:
        return (BASE_MODEL_BADGE_LABELS["anima"], BASE_MODEL_BADGE_COLORS["anima"])
    if kinds == {"illustrious", "anima"}:
        return ("mix", BASE_MODEL_BADGE_COLORS["mix"])
    return None


def entry_lost_loras(lora_names: list[str] | tuple[str, ...] | None,
                     lost_names: set[str] | None) -> list[str]:
    """参照LoRAのうちディスク上に痕跡が無い（削除済み）名前を元の表記のまま返す。

    lost_names は MainWindow が作る「ワイルドカードから参照されているのに、
    重み・cm-info.json・civitai.info・metadata.json のどれにも対応ファイルが
    存在しない名前」の集合（lower）。フォルダ付き参照 `<lora:sub/name>` にも
    末尾名で一致する。
    """
    if not lora_names or not lost_names:
        return []
    missing: list[str] = []
    for name in lora_names:
        raw = str(name).strip()
        key = lora_name_key(raw)
        tail = key.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if key in lost_names or tail in lost_names:
            missing.append(raw)
    return missing


def _make_monochrome_pixmap(pixmap: QPixmap, dim: float = LOST_CARD_DIM) -> QPixmap:
    """サムネイルをグレースケール化して少し沈める（LOSTカード用）。

    `QImage.Format_Grayscale8` 変換が輝度重みでのモノクロ化なので、色相を完全に
    落としつつ明るさの対比は残る。従来は暗いオーバーレイを重ねる方式だったが、
    カード背景もウィンドウ背景も暗いたま差分が見えなかった
    （ユーザー指摘「グレーアウトが弱い。モノクロにして」）。
    """
    if pixmap.isNull():
        return pixmap
    gray = pixmap.toImage().convertToFormat(QImage.Format_Grayscale8)
    result = QPixmap.fromImage(gray.convertToFormat(QImage.Format_ARGB32_Premultiplied))
    painter = QPainter(result)
    level = max(0, min(255, int(255 * dim)))
    painter.setCompositionMode(QPainter.CompositionMode_Multiply)
    painter.fillRect(result.rect(), QColor(level, level, level))
    painter.end()
    return result


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


def _fit_prefix(text: str, fm: QFontMetrics, width: int) -> int:
    """幅に収まる最長prefixの長さ。直前の区切り記号は次の行へ回す（"foo_" で切らない）。"""
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fm.horizontalAdvance(text[:mid]) <= width:
            lo = mid
        else:
            hi = mid - 1
    while lo > 1 and text[lo - 1] in " _-.·":
        lo -= 1
    return lo


def two_line_title(text: str, font: QFont, width: int) -> list[str]:
    """カード标题を最大2行に折り返し、3行目以降は2行目の末尾を省略記号にする（仕様③）。
    style の WrapText は高さを超えた行を「…」なしで切るため、折返しはここで決める。"""
    if not text or width <= 0:
        return [text] if text else []
    fm = QFontMetrics(font)
    if fm.horizontalAdvance(text) <= width:
        return [text]
    layout = QTextLayout(text, font)
    layout.beginLayout()
    spans: list[tuple[int, int]] = []
    while len(spans) < 32:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        spans.append((line.textStart(), line.textLength()))
    layout.endLayout()
    lines = [text[s:s + n] for s, n in spans if n]
    # 語で折り返せた（各行が幅に収まる）場合だけそれを信用する。
    # 区切りの無い長い名前は QTextLayout が1行のまま溢出させるので文字単位で折る。
    if lines and all(fm.horizontalAdvance(ln) <= width + 1 for ln in lines):
        if len(lines) == 1:
            return [fm.elidedText(text, Qt.ElideRight, width)]
        rest = text[spans[1][0]:].lstrip(" _-.·")
        return [lines[0], fm.elidedText(rest, Qt.ElideRight, width)]
    first = _fit_prefix(text, fm, width)
    rest = text[first:].lstrip(" _-.·")
    return [text[:first], fm.elidedText(rest, Qt.ElideRight, width)]


def render_placeholder_pixmap(thumbnail_size: int) -> QPixmap:
    width = max(80, thumbnail_size)
    height = int(width * theme.CARD_ICON_RATIO)
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(theme.APP_BG))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.PANEL_SIDE))
    painter.drawRoundedRect(4, 4, width - 8, height - 8, theme.RADIUS_CARD, theme.RADIUS_CARD)
    painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
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


class CardFlip(QObject):
    """カードを「裏返して」次の枚へ切り替える1回ぶんの進行。

    横縮みを |cos(pi t)| にすると縦軸まわり回転の横からの射影そのものになる。
    t<0.5 で旧画像、t>=0.5 で新画像を描き、中間に影を乗せて裏返った面の
    暗さを演出する。
    """

    def __init__(self, model: "ThumbnailListModel", rel_path: str,
                 outgoing: QPixmap, incoming: QImage):
        super().__init__(model)
        self.rel_path = rel_path
        self.outgoing = outgoing
        self.incoming = incoming
        self.incoming_pixmap = QPixmap.fromImage(incoming)
        self._progress = 0.0
        self._anim = QPropertyAnimation(self, b"progress", self)
        self._anim.setDuration(FLIP_DURATION_MS)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)
        self._anim.valueChanged.connect(self._on_value)
        self._anim.finished.connect(self._on_done)

    @Property(float)
    def progress(self) -> float:
        return self._progress

    @progress.setter
    def progress(self, value: float) -> None:
        self._progress = float(value)

    def start(self) -> None:
        self._anim.start()

    def _on_value(self, _value) -> None:
        model = self.parent()
        if isinstance(model, ThumbnailListModel):
            model.repaint_flip(self.rel_path)

    def _on_done(self) -> None:
        model = self.parent()
        if isinstance(model, ThumbnailListModel):
            model.finish_flip(self.rel_path)

    def pixmap_for(self, size: QSize) -> QPixmap | None:
        t = self._progress
        squeeze = max(0.06, abs(math.cos(math.pi * t)))
        source = self.outgoing if t < 0.5 else self.incoming_pixmap
        if source.isNull():
            return None
        width = max(1, int(size.width() * squeeze))
        scaled = source.scaled(width, size.height(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        frame = QPixmap(scaled.width(), scaled.height())
        frame.fill(Qt.transparent)
        painter = QPainter(frame)
        painter.drawPixmap(0, 0, scaled)
        shade = int(120 * math.sin(math.pi * t))
        if shade:
            painter.fillRect(frame.rect(), QColor(0, 0, 0, shade))
        painter.end()
        return frame


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
        # お気に入りパスのセット（MainWindow から設定される）
        self.favorite_paths: set[str] = set()
        # LoRA名(lower) → BaseModel のマップ（MainWindow から設定される）。
        # サムネイル左上の ill/ani バッジ描画に使う。
        self.lora_base_model_map: dict[str, str] = {}
        # ワイルドカードから参照されているのに実体が無いLoRA名（lower）の集合
        # （MainWindow から設定される）。含まれる名前を参照するカードは LOST 表示。
        self.lost_lora_names: set[str] = set()
        # icon_cache には「読み込みに成功したサムネイル」だけを保持する。
        # 失敗結果はここに入れない（失敗の永久キャッシュを防ぐ）。
        # 【方針B】キーは (rel_path, thumbnail_size)。rel_path は環境非依存
        # (POSIX 区切り) のため、abs_path 正規化の不一致でキャッシュミスする
        # 問題を根本から回避できる。
        self.icon_cache: OrderedDict[tuple[str, int], QIcon] = OrderedDict()
        self.placeholder_cache: dict[int, QIcon] = {}
        self.max_icon_cache = 720
        self.pending_loads: set[tuple[str, int]] = set()
        # 直近の読み込みが失敗したキーの集合と、最後に試行した時刻。
        self.failed_keys: set[tuple[str, int]] = set()
        self._last_attempt: dict[tuple[str, int], float] = {}
        # 画面の再描画を待たずに自動復旧させるためのバックグラウンド再試行回数。
        self._auto_retry_counts: dict[tuple[str, int], int] = {}
        # 【レースコンディション修正】各キーごとのロードトークン。
        # 新しいロードを開始するたびにインクリメントされ、古いタスクの結果は破棄される。
        # これにより「サムネイル生成前に起動した古いタスクが新画像を上書きする」問題を解消する。
        self._load_tokens: dict[tuple[str, int], int] = {}
        # 【保護パス】set_icon_directly でキャッシュに格納した rel_path。
        # 一定時間（3秒間）は clear_cache_for_path や force_reload_all での
        # キャッシュ削除から保護される。これにより、サムネイル生成後に遅延
        # シグナル等でキャッシュが消されるのを確実に防ぐ。
        self._protected_paths: dict[str, float] = {}  # rel_path -> expire_timestamp
        # 【方針B】row_by_path のキーは rel_path（環境非依存）。
        self.row_by_path: dict[str, int] = {}
        # L8 修正: グローバル QThreadPool を直接 setMaxThreadCount すると
        # 他コンポーネント（PreviewLoadTask 等）にも影響する。
        # 専用の QThreadPool インスタンスを持つことで分離する。
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(min(8, os.cpu_count() or 8))
        self.loader_signals = ThumbnailLoadSignals(self)
        self.loader_signals.loaded.connect(self._handle_loaded_thumbnail)
        # --- 2枚目以降（カードめくり）状態 ---
        # rel_path → 表示中の枚目（0起点）。カードの選択とは独立して保持する。
        self.variant_index: dict[str, int] = {}
        # rel_path → 実パス列（1枚目→2枚目以降）/ folder → stem索引。
        # 走査は「めくった・複数枚のカードが存在するフォルダ」だけ。
        self._gallery_cache: dict[str, list[str]] = {}
        self._folder_index: dict[str, dict[str, list[str]]] = {}
        # MainWindow から注入される（サムネルートと、フォルダ一覧の取得関数）
        self.thumbnail_root: Path | None = None
        self.gallery_listing_provider = None  # relフォルダ -> list[str]
        self.flips: dict[str, CardFlip] = {}

    def set_entries(self, entries: list[WildcardEntry]) -> None:
        # 【方針B】リスト同一性判定・row_by_path 構築ともに rel_path をキーに使う。
        # abs_path は OS・resolve() の有無で表記ゆれするため、同値判定に不適。
        if len(self.entries) == len(entries) and all(
            self.entries[i].rel_path == entries[i].rel_path for i in range(len(entries))
        ):
            changed_rows: list[int] = []
            for i, entry in enumerate(entries):
                old = self.entries[i]
                fields_changed = (old.thumbnail_path != entry.thumbnail_path
                        or old.first_line != entry.first_line
                        or old.has_thumbnail != entry.has_thumbnail
                        or old.stem != entry.stem)
                previously_failed = self._has_failed_thumbnail(entry.rel_path)
                if fields_changed or previously_failed:
                    key = (entry.rel_path, self.thumbnail_size)
                    if key not in self.icon_cache:
                        self.clear_cache_for_path(entry.rel_path)
                    self.entries[i] = entry
                    changed_rows.append(i)
            if changed_rows:
                first = self.index(changed_rows[0], 0)
                last = self.index(changed_rows[-1], 0)
                self.dataChanged.emit(first, last, [Qt.DecorationRole, Qt.DisplayRole, Qt.SizeHintRole])
            return
        self.beginResetModel()
        self.entries = list(entries)
        self.row_by_path = {entry.rel_path: index for index, entry in enumerate(self.entries)}
        self.endResetModel()

    def _has_failed_thumbnail(self, rel_path: str) -> bool:
        return any(key[0] == rel_path for key in self.failed_keys)

    def _is_protected(self, rel_path: str) -> bool:
        """指定 rel_path が保護期間内かどうかを判定し、期限切れのエントリをクリーンアップする。"""
        now = time.time()
        expire = self._protected_paths.get(rel_path)
        if expire is None:
            return False
        if now >= expire:
            del self._protected_paths[rel_path]
            return False
        return True

    def force_reload_all(self) -> None:
        """全キャッシュをクリアし、全行の dataChanged を発火して強制再描画する。"""
        # 保護パスのキャッシュは保持し、それ以外をクリア
        protected_set: set[str] = set()
        now = time.time()
        for path, expire in list(self._protected_paths.items()):
            if now < expire:
                protected_set.add(path)
            else:
                del self._protected_paths[path]

        self.icon_cache = OrderedDict(
            (key, value) for key, value in self.icon_cache.items()
            if key[0] in protected_set
        )
        self.pending_loads = {key for key in self.pending_loads if key[0] in protected_set}
        self.failed_keys = {key for key in self.failed_keys if key[0] in protected_set}
        self._last_attempt = {key: ts for key, ts in self._last_attempt.items() if key[0] in protected_set}
        self._auto_retry_counts = {key: c for key, c in self._auto_retry_counts.items() if key[0] in protected_set}
        for key in list(self._load_tokens.keys()):
            if key[0] not in protected_set:
                self._load_tokens[key] = self._load_tokens[key] + 1
        # F5 は「エクスプローラー側でサムネイルを増減した」後の再評価にも使うので、
        # 2枚目以降のフォルダ一覧も組み直す（枚ずれ防止）
        self._folder_index.clear()
        self._gallery_cache.clear()
        if self.entries:
            first = self.index(0, 0)
            last = self.index(len(self.entries) - 1, 0)
            self.dataChanged.emit(first, last, [Qt.DecorationRole])

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
        self._load_tokens.clear()
        self._protected_paths.clear()
        if self.entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.entries) - 1, 0), [Qt.DecorationRole, Qt.SizeHintRole])

    def clear_cache_for_path(self, rel_path: str) -> None:
        # 【方針B】rel_path をキーにキャッシュ削除。
        # 【保護パス】set_icon_directly で最近格納された rel_path は
        # キャッシュ削除から保護する（サムネイル生成直後の保護）
        if self._is_protected(rel_path):
            return
        self.icon_cache = OrderedDict((key, value) for key, value in self.icon_cache.items() if key[0] != rel_path)
        self.pending_loads = {key for key in self.pending_loads if key[0] != rel_path}
        self.failed_keys = {key for key in self.failed_keys if key[0] != rel_path}
        self._last_attempt = {key: ts for key, ts in self._last_attempt.items() if key[0] != rel_path}
        self._auto_retry_counts = {key: c for key, c in self._auto_retry_counts.items() if key[0] != rel_path}
        # 【レースコンディション修正】トークンをインクリメントして、
        # この rel_path に対する全ての古いタスクの結果を無効化する。
        for key in list(self._load_tokens.keys()):
            if key[0] == rel_path:
                self._load_tokens[key] = self._load_tokens[key] + 1

    def replace_entry(self, updated: WildcardEntry) -> None:
        # 【方針B】rel_path をキーに行を検索。
        row = self.row_by_path.get(updated.rel_path)
        if row is None or row >= len(self.entries):
            return
        self.entries[row] = updated
        self.row_by_path[updated.rel_path] = row
        # キャッシュをクリアして再ロードさせる（beginResetModelは選択を解除するので避ける）
        self.clear_cache_for_path(updated.rel_path)
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, [Qt.DecorationRole, Qt.DisplayRole, Qt.SizeHintRole])

    def set_icon_directly(self, rel_path: str, image: QImage) -> None:
        """生成直後のサムネイルを同期的に icon_cache へ格納し、即座に dataChanged を発火する。

        【根本修正（方針B）】サムネイル生成後に非同期 ThumbnailLoadTask 経由でカードが
        更新されない問題に対し、生成完了コールバック内でファイルを同期的に
        読み込み、このメソッドで直接キャッシュへ格納する。
        これにより非同期パイプラインの遅延や失敗に依存せず、即座にカードが
        新しいサムネイルを表示する。

        また、トークンをインクリメントすることで、進行中の古い ThumbnailLoadTask
        が完了してもその結果（旧画像）がキャッシュを上書きしないようにする。

        【保護パス】このメソッドで格納した rel_path は3秒間保護され、
        clear_cache_for_path や force_reload_all での削除から保護される。
        これにより、サムネイル生成後に遅延シグナル等でキャッシュが
        消されるのを確実に防ぐ。

        【方針B】引数は abs_path ではなく rel_path（環境非依存）。
        これにより _build_entry と _portable_abs_path の表記ゆれを気にせず
        常に正しい行へキャッシュを格納できる。
        """
        key = (rel_path, self.thumbnail_size)
        self.failed_keys.discard(key)
        self._last_attempt.pop(key, None)
        self._auto_retry_counts.pop(key, None)
        self.pending_loads.discard(key)
        # トークンをインクリメント → 古いタスクの結果は破棄される
        self._load_tokens[key] = self._load_tokens.get(key, 0) + 1
        # 保護パスに登録（3秒間）
        self._protected_paths[rel_path] = time.time() + 3.0
        icon = QIcon(QPixmap.fromImage(image))
        self.icon_cache[key] = icon
        self.icon_cache.move_to_end(key)
        while len(self.icon_cache) > self.max_icon_cache:
            self.icon_cache.popitem(last=False)
        row = self.row_by_path.get(rel_path)
        if row is not None and 0 <= row < len(self.entries):
            index = self.index(row, 0)
            self.dataChanged.emit(index, index, [Qt.DecorationRole])

    # ── 2枚目以降（カードめくり） ────────────────────────────────

    def gallery_paths(self, entry: WildcardEntry) -> list[str]:
        """このカードのサムネイル実パス列（1枚目→2枚目以降）。フォルダ単位で索引化。

        1フォルダ1回の scandir + 1回の分類で全カードに使い回すので、可視カードが
        多くても描画ごとに走査はしない。DBが解決した1枚目は必ず先頭に置く
        （F5直後に枚ずれしない為）。
        """
        cached = self._gallery_cache.get(entry.rel_path)
        if cached is not None:
            return cached
        root = self.thumbnail_root
        paths: list[str] = []
        if root is not None:
            folder = rel_folder_of(entry.rel_path)
            index = self._folder_index.get(folder)
            if index is None:
                if self.gallery_listing_provider is not None:
                    try:
                        names = self.gallery_listing_provider(folder)
                    except Exception:
                        names = []
                else:
                    names = thumbnail_folder_listing(root, entry.rel_path)
                index = build_thumbnail_gallery_index(names)
                self._folder_index[folder] = index
            base = root if not folder else root / folder
            paths = [str(base / name) for name in index.get(entry.stem, [])]
        if entry.thumbnail_path and not any(thumbnail_paths_equal(entry.thumbnail_path, p) for p in paths):
            paths.insert(0, entry.thumbnail_path)
        self._gallery_cache[entry.rel_path] = paths
        return paths

    def invalidate_gallery(self, rel_path: str) -> None:
        self._gallery_cache.pop(rel_path, None)
        folder = rel_folder_of(rel_path)
        self._folder_index.pop(folder, None)
        for cached_rel in list(self._gallery_cache.keys()):
            if rel_folder_of(cached_rel) == folder:
                self._gallery_cache.pop(cached_rel, None)

    def variant_count(self, entry: WildcardEntry) -> int:
        return len(self.gallery_paths(entry))

    def current_variant(self, entry: WildcardEntry) -> int:
        return self.variant_index.get(entry.rel_path, 0)

    def display_path_for(self, entry: WildcardEntry) -> str | None:
        """表示・ロード対象の実パス。枚目が0でDBの1枚目があるなら走査しない（高速路）。"""
        index = self.variant_index.get(entry.rel_path, 0)
        if index == 0 and entry.thumbnail_path:
            return entry.thumbnail_path
        paths = self.gallery_paths(entry)
        if not paths:
            return entry.thumbnail_path or None
        return paths[min(index, len(paths) - 1)]

    def flip_to_next(self, entry: WildcardEntry) -> int | None:
        """次の枚へめくる。成功したら 1起点 の枚目を返す（枚数が足りないなら None）。"""
        paths = self.gallery_paths(entry)
        if len(paths) < 2:
            return None
        rel_path = entry.rel_path
        current = self.variant_index.get(rel_path, 0)
        nxt = (current + 1) % len(paths)
        target = paths[nxt]
        width = max(80, self.thumbnail_size)
        height = int(width * theme.CARD_ICON_RATIO)
        incoming = decode_cover_image(target, width, height)
        if incoming.isNull():
            return None
        cached = self.icon_cache.get((rel_path, self.thumbnail_size))
        outgoing = cached.pixmap(QSize(width, height)) if cached is not None else QPixmap()
        if outgoing.isNull():
            outgoing = self._get_placeholder_icon().pixmap(QSize(width, height))
        self.variant_index[rel_path] = nxt
        flip = CardFlip(self, rel_path, outgoing, incoming)
        self.flips[rel_path] = flip
        flip.start()
        self.repaint_flip(rel_path)
        return nxt + 1

    def repaint_flip(self, rel_path: str) -> None:
        row = self.row_by_path.get(rel_path)
        if row is None or row >= len(self.entries):
            return
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, [Qt.DecorationRole])

    def finish_flip(self, rel_path: str) -> None:
        flip = self.flips.pop(rel_path, None)
        if flip is None:
            return
        self.set_icon_directly(rel_path, flip.incoming)

    def reset_variant(self, rel_path: str) -> None:
        """サムネイル削除/追加時など、枚数の前提が変わったときの初期化。"""
        self.variant_index.pop(rel_path, None)
        self.invalidate_gallery(rel_path)

    def forget_thumbnail(self, rel_path: str) -> None:
        """キャッシュ・枚数・フォルダ一覧を全部忘れて再ロードさせる。

        `clear_cache_for_path` は set_icon_directly 後の3秒間保護に弾かれるため、
        「生成直後にめくった→すぐ削除」の順でも旧画像が残る。削除では保護を
        無視して確実に忘れる必要がある。
        """
        self._protected_paths.pop(rel_path, None)
        self.reset_variant(rel_path)
        self.clear_cache_for_path(rel_path)
        row = self.row_by_path.get(rel_path)
        if row is not None and row < len(self.entries):
            index = self.index(row, 0)
            self.dataChanged.emit(index, index, [Qt.DecorationRole])

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
            height = int(max(80, self.thumbnail_size) * theme.CARD_ICON_RATIO) + CARD_TITLE_HEIGHT + CARD_FRAME_PADDING
            return QSize(width, height)
        if role == Qt.UserRole:
            return entry.abs_path
        if role == Qt.ToolTipRole:
            missing = entry_lost_loras(entry.lora_names, self.lost_lora_names)
            if missing:
                return "LoRAが見つかりません（削除済み）:\n" + "\n".join(missing)
            return None
        return None

    def entry_at(self, index: QModelIndex) -> WildcardEntry | None:
        if not index.isValid():
            return None
        row = index.row()
        if not (0 <= row < len(self.entries)):
            return None
        return self.entries[row]

    def _get_icon(self, entry: WildcardEntry) -> QIcon:
        # 【方針B】entry.rel_path をキーにキャッシュ検索。
        key = (entry.rel_path, self.thumbnail_size)
        cached = self.icon_cache.get(key)
        if cached is not None:
            self.icon_cache.move_to_end(key)
            return cached
        self._queue_thumbnail_load(entry, priority=0)
        return self._get_placeholder_icon()

    def _queue_thumbnail_load(self, entry: WildcardEntry, priority: int = 0, force: bool = False) -> None:
        # 【方針B】entry.rel_path をキーに使用。ThumbnailLoadTask にも
        # rel_path を渡す（シグナルのペイロードも rel_path になる）。
        key = (entry.rel_path, self.thumbnail_size)
        if key in self.icon_cache or key in self.pending_loads:
            return
        # 表示対象は枚目（variant）込みで解決する。1枚目だけなら DB のパスを
        # そのまま使うので、従来動作（と速さ）は変わらない。
        thumbnail_path = self.display_path_for(entry)
        if not thumbnail_path:
            return
        if not force and key in self.failed_keys:
            last = self._last_attempt.get(key, 0.0)
            if (time.monotonic() - last) < self.FAILED_RETRY_COOLDOWN_SEC:
                # 直近で失敗したばかり。リペイントの度に再試行してディスクを
                # 叩き続けないよう、クールダウン中は静かにプレースホルダーを返す。
                return
        self.pending_loads.add(key)
        self._last_attempt[key] = time.monotonic()
        # 【レースコンディション修正】トークンをインクリメントして新しいタスクに割り当てる。
        # 古いタスクが完了しても _handle_loaded_thumbnail でトークンが不一致なら破棄される。
        token = self._load_tokens.get(key, 0) + 1
        self._load_tokens[key] = token
        self.thread_pool.start(
            ThumbnailLoadTask(entry.rel_path, thumbnail_path, self.thumbnail_size, token, self.loader_signals),
            priority,
        )

    def refresh_thumbnails(self) -> None:
        """F5（再読み込み）から呼び出し、失敗していたサムネイルを
        クールダウンを無視して強制的に再評価する。"""
        if not self.entries:
            return
        targets = [entry for entry in self.entries if self._has_failed_thumbnail(entry.rel_path)]
        if not targets:
            return
        for entry in targets:
            self.clear_cache_for_path(entry.rel_path)
            self._queue_thumbnail_load(entry, priority=0, force=True)
        rows = sorted(self.row_by_path[e.rel_path] for e in targets if e.rel_path in self.row_by_path)
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
            # 【方針B】key[0] は rel_path。
            row = self.row_by_path.get(key[0])
            if row is None or not (0 <= row < len(self.entries)):
                return
            entry = self.entries[row]
            if (entry.rel_path, self.thumbnail_size) != key:
                return  # サムネイルサイズが変わった等、状況が変化していたら何もしない
            if key not in self.failed_keys:
                return  # その間に別経路で成功済みなら何もしない
            self._queue_thumbnail_load(entry, priority=0, force=True)

        QTimer.singleShot(delay_ms, _retry)

    @Slot(str, int, int, object)
    def _handle_loaded_thumbnail(self, rel_path: str, thumbnail_size: int, token: int, image: QImage | None) -> None:
        # 【方針B】シグナルの第1引数は rel_path。abs_path の表記ゆれを気にせず
        # 常に正しい行を更新できる。
        key = (rel_path, thumbnail_size)
        self.pending_loads.discard(key)
        current_token = self._load_tokens.get(key, 0)
        if token != current_token:
            return
        if image is None:
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
        row = self.row_by_path.get(rel_path)
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
            painter.setPen(QPen(QColor(theme.ACCENT), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), theme.RADIUS_CARD - 1, theme.RADIUS_CARD - 1)
        painter.restore()


class ThumbnailCardDelegate(QStyledItemDelegate):
    def _card_metrics(self, index: QModelIndex) -> tuple[int, int, int]:
        thumb_width = max(80, int(getattr(index.model(), "thumbnail_size", 260)))
        thumb_height = int(thumb_width * theme.CARD_ICON_RATIO)
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
        painter.setBrush(QColor(theme.HOVER_BG) if is_hovered else QColor(theme.CARD_BG))
        painter.drawRoundedRect(rect, theme.RADIUS_CARD, theme.RADIUS_CARD)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.state = opt.state & ~QStyle.State_Selected & ~QStyle.State_MouseOver
        opt.rect = rect.adjusted(12, 12, -12, -12)
        title_text = opt.text
        # 标题は self で2行省略描画する（style の WrapText は高さ超過行を「…」なしで切る）
        opt.text = ""
        opt.decorationPosition = QStyleOptionViewItem.Top
        opt.decorationAlignment = Qt.AlignHCenter
        opt.decorationSize = QSize(thumb_width, thumb_height)
        model = index.model()
        try:
            entry = model.entry_at(index)
        except Exception:
            entry = None
        try:
            missing = entry_lost_loras(entry.lora_names if entry is not None else None,
                                       getattr(model, "lost_lora_names", None))
        except Exception:
            missing = []
        style = option.widget.style() if option.widget is not None else QApplication.style()
        # ユーザー作業分: style.drawControl に渡す前にアイコンへ角丸を適用する。
        # drawControl 呼び出し後に painter.setClipPath() しても style 内部の
        # save()/restore() で clip が巻き戻されるため、ここで pixmap を加工する。
        if entry is not None:
            try:
                flip = model.flips.get(entry.rel_path)
            except Exception:
                flip = None
        else:
            flip = None
        if flip is not None:
            frame = flip.pixmap_for(opt.decorationSize)
            if frame is not None and not frame.isNull():
                opt.icon = QIcon(_make_rounded_pixmap(frame, 10))
        elif not opt.icon.isNull():
            pix = opt.icon.pixmap(opt.decorationSize)
            if not pix.isNull():
                if missing:
                    pix = _make_monochrome_pixmap(pix)
                opt.icon = QIcon(_make_rounded_pixmap(pix, 10))
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, option.widget)

        if title_text:
            title_font = QFont(painter.font())
            title_font.setPixelSize(theme.FONT_SIZE)
            painter.setFont(title_font)
            fm = QFontMetrics(title_font)
            line_h = fm.height() + 2
            title_rect = QRect(
                opt.rect.left(),
                opt.rect.top() + thumb_height + 2,
                opt.rect.width(),
                title_height - 4,
            )
            lines = two_line_title(title_text, title_font, title_rect.width())
            y = title_rect.top() + max(0, (title_rect.height() - line_h * len(lines)) // 2)
            painter.setPen(QColor(theme.TEXT))
            for line in lines:
                painter.drawText(
                    QRect(title_rect.left(), y, title_rect.width(), line_h),
                    Qt.AlignHCenter | Qt.AlignTop,
                    line,
                )
                y += line_h

        if is_selected:
            # 選択はアクセントの青アウトライン（仕様③）。旧来は白4pxでカード面を潰していた
            painter.setPen(QPen(QColor(theme.ACCENT), 2))
            painter.setBrush(Qt.NoBrush)
            inset = QRectF(rect.adjusted(1, 1, -1, -1))
            painter.drawRoundedRect(inset, theme.RADIUS_CARD - 1, theme.RADIUS_CARD - 1)

        # サムネイル左上の ill/ani バッジ描画
        try:
            base_map = getattr(model, "lora_base_model_map", None)
            badge = entry_base_badge(entry.lora_names if entry is not None else None, base_map)
        except Exception:
            badge = None
        if badge is not None:
            badge_text, badge_color = badge
            badge_font = QFont(painter.font())
            badge_font.setBold(True)
            badge_font.setPixelSize(max(9, min(11, rect.width() // 16)))
            painter.setFont(badge_font)
            badge_fm = QFontMetrics(badge_font)
            badge_h = 16
            badge_pad_x = 5
            badge_w = badge_fm.horizontalAdvance(badge_text) + badge_pad_x * 2
            badge_rect = QRect(rect.left() + 6, rect.top() + 6, badge_w, badge_h)
            badge_bg = QColor(badge_color)
            badge_bg.setAlpha(190)
            painter.setPen(QPen(QColor(theme.APP_BG), 1))
            painter.setBrush(badge_bg)
            painter.drawRoundedRect(badge_rect, 4, 4)
            painter.setPen(QColor(theme.TEXT))
            painter.drawText(badge_rect, Qt.AlignCenter, badge_text)
        # 2枚以上持つカードは右下に「2/3」の枚数ピル。めくれるカードが
        # どれか一目で分からないと、ダブルクリックでめくれること自体が
        # 発見できない（おまけの機能は存在が見えて初めて使われる）。
        try:
            pages = model.variant_count(entry) if entry is not None else 0
        except Exception:
            pages = 0
        if pages >= 2:
            page_font = QFont(painter.font())
            page_font.setBold(True)
            page_font.setPixelSize(max(9, min(11, rect.width() // 16)))
            painter.setFont(page_font)
            page_fm = QFontMetrics(page_font)
            shown = min(model.current_variant(entry) + 1, pages)
            page_text = f"{shown}/{pages}"
            page_w = page_fm.horizontalAdvance(page_text) + 12
            page_h = 16
            thumb_bottom = opt.rect.top() + thumb_height
            page_rect = QRect(opt.rect.right() - page_w - 6,
                              max(opt.rect.top() + 6, thumb_bottom - page_h - 6),
                              page_w, page_h)
            page_bg = QColor(theme.APP_BG)
            page_bg.setAlpha(215)
            painter.setPen(QPen(QColor(theme.BORDER), 1))
            painter.setBrush(page_bg)
            painter.drawRoundedRect(page_rect, 4, 4)
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(page_rect, Qt.AlignCenter, page_text)
        # 削除済みLoRA参照のカード: 減光は上のモノクロ化で済んでいるので、ここでは
        # サムネイル左下に LOST ピルを置く（左上=ill/aniバッジ、右上=お気に入り星と
        # 重なりにくい位置）
        if missing:
            lost_font = QFont(painter.font())
            lost_font.setBold(True)
            lost_font.setPixelSize(max(9, min(11, rect.width() // 16)))
            painter.setFont(lost_font)
            lost_fm = QFontMetrics(lost_font)
            lost_h = 16
            lost_w = lost_fm.horizontalAdvance(LOST_BADGE_LABEL) + 12
            thumb_bottom = opt.rect.top() + thumb_height
            lost_rect = QRect(opt.rect.left() + 6,
                              max(opt.rect.top() + 6, thumb_bottom - lost_h - 6),
                              lost_w, lost_h)
            lost_bg = QColor(LOST_BADGE_COLOR)
            lost_bg.setAlpha(210)
            painter.setPen(QPen(QColor(theme.APP_BG), 1))
            painter.setBrush(lost_bg)
            painter.drawRoundedRect(lost_rect, 4, 4)
            painter.setPen(QColor(theme.APP_BG))
            painter.drawText(lost_rect, Qt.AlignCenter, LOST_BADGE_LABEL)
        # お気に入り星マークの描画
        abs_path = index.data(Qt.UserRole)
        favorite_paths: set[str] | None = getattr(model, "favorite_paths", None)
        if favorite_paths is not None and abs_path in favorite_paths:
            star_size = max(14, rect.width() // 8)
            star_x = rect.right() - star_size - 6
            star_y = rect.top() + 6
            star_rect = QRect(star_x, star_y, star_size, star_size)
            # 影（可読性向上）
            painter.setPen(QPen(QColor(theme.SHADOW), 1))
            painter.setBrush(QColor(theme.SHADOW_SOFT))
            painter.drawEllipse(star_rect.adjusted(1, 1, 1, 1))
            # 星の円形バッジ
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.PANEL_MAIN))
            painter.drawEllipse(star_rect)
            # 星テキスト
            star_font = painter.font()
            star_font.setPixelSize(max(10, star_size - 3))
            painter.setFont(star_font)
            painter.setPen(QColor(theme.FAVORITE))
            painter.drawText(star_rect, Qt.AlignCenter, "★")

        painter.restore()
