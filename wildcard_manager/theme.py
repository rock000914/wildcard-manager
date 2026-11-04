"""UI外観の単一ソース（Game Asset Browser 仕様）。

色・角丸・余白・寸法・部品スタイルをここに集約する。各所へばらまいていた
ハードコード色（旧パレット）は LEGACY_MAP で対応付け只管する。
※機能・イベント・データ構造はここでは扱わない（Appearance Only）。
"""
from __future__ import annotations

# ---------------------------------------------------------------- パレット
APP_BG = "#111722"          # アプリ背景        (旧 #0f1216 / #101419)
PANEL_SIDE = "#171f2b"      # サイドパネル      (旧 #131820)
PANEL_MAIN = "#1b2533"      # メインパネル      (旧 #161b22)
CARD_BG = "#202c3b"         # カード背景        (旧 #171b21 / #1a1d22)
HOVER_BG = "#29384a"        # ホバー背景        (旧 #1f2731 / #2a2d32)
BORDER = "#303d4e"          # 境界線            (旧 #2a3440 / #202833 / #2d3640)
TEXT = "#e5eaf2"            # メイン文字        (旧 #edf2f7 / #eef2f7)
TEXT_MUTED = "#929fb1"      # 補助文字          (旧 #aeb8c2 / #708090)
ACCENT = "#4388e8"          # アクセントブルー  (旧 #2e5fb8 / #3a71c6 / #2b6cb0)
SELECTED = "#254c7c"        # 選択背景          (旧 #2f5274 / #1e4a9a)

# 意味を持つ色はパレット外で維持（情報として色を使うのはここだけ）
DANGER = "#b84040"
DANGER_BG = "#2a1b1c"
DANGER_PRESSED = "#7a2f2f"
DANGER_TEXT = "#ef8d8d"    # 暗背景の状態テキスト（エラー文）
OK = "#7fd48e"             # 暗背景の状態テキスト（取得成功）
OK_BG = "#1b2a22"          # OK系の面（LoRAタグチップなど）
OK_BORDER = "#33604a"
WARNING = "#e7b65c"
FAVORITE = "#f2c14e"
LOST = "#8b95a5"

# アクセントの派生（Primaryボタンの状態など）
ACCENT_HOVER = "#5b9bee"
ACCENT_PRESSED = "#2f6cc0"
ACCENT_TEXT = "#f2f7ff"
GHOST_HOVER = "#22303f"
GHOST_PRESSED = "#1a2634"
SURFACE_RAISED = "#243240"   # ホバー時の面（入力欄フォーカス線など）

# 線画アイコンの描画色（TEXT より一段淡い主線／面を示す帯／補助線）
ICON_STROKE = "#cfd8e3"
ICON_BAND = "#48566a"
ICON_MUTED = "#7c8899"

# 影（パレット外の無彩色アルファ。明るい画像の上でも読めるようにするための地色）
SHADOW = "#00000088"
SHADOW_SOFT = "#00000044"

# ---------------------------------------------------------------- 寸法
RADIUS_PANEL = 8
RADIUS_CARD = 8
RADIUS_CONTROL = 7
RADIUS_CHIP = 6
BORDER_WIDTH = 1

# カード面の縦横比（高さ/幅）。生成・保存されるサムネイルは 2:3（`THUMB_SOURCE_RATIO`）
# なので、これより小さい値＝カードを太くするほど画像の上下が切り捨てられる
# （引き伸ばしはしない。クロップ方針は `thumbnail_workers.cover_crop`）。
CARD_ICON_RATIO = 1.35
THUMB_SOURCE_RATIO = 1.5

SPACE = 8
SPACE_SM = 4
SPACE_LG = 16
SPACE_XL = 24

# ボタン間の2段組。ペア（同じ役割の隣同士の操作）と、クラスタ（役割の違う塊）の
# 間隔を分けると、ツールバー上のかたまりが目で読める。
GAP_PAIR = 6
GAP_CLUSTER = 12

BUTTON_HEIGHT = 36          # 34〜38
ICON_BUTTON_SIZE = 36       # 32〜36四方。ツールバーの四角ボタン＝ボタン高と揃える
INPUT_HEIGHT = 36
CHECKBOX_SIZE = 16
CHIP_HEIGHT = 24            # 22〜26
ICON_SIZE = 16
# アイコン+テキストボタンの内側すき間。Qt はテキストをアイコン矩形の右+4px
# （リテラル）に置く。この 4px は QSS でも styleHint でも変えられない
# （PySide6 は SH_Button_TextIconPadding を公開しておらず、iconSize を広げても
# pixmap が中央寄せされるだけで間隔は不変）。よってアイコン側の透明帯で稼ぐ。
ICON_TEXT_PAD = 4
LIST_ROW_HEIGHT = 28        # ツリー／フィルタリストの行高

FONT_SIZE = 13              # 13〜14
HEADING_SIZE = 14
SMALL_SIZE = 11
BUTTON_PADDING_X = 12
TOOLBAR_HEIGHT = 44

FONT_STACK = "'Segoe UI', 'Yu Gothic UI', 'Meiryo UI', sans-serif"

# 旧→新の対応（置忘れ監査用。キー=旧リテラルの小文字）
LEGACY_MAP = {
    "#0f1216": APP_BG, "#101419": APP_BG, "#0d1014": APP_BG, "#10141a": APP_BG,
    "#131820": PANEL_SIDE, "#121a27": PANEL_SIDE, "#151a21": PANEL_SIDE,
    "#161b22": PANEL_MAIN, "#171b21": CARD_BG, "#1a1d22": CARD_BG, "#1e2430": BORDER,
    "#2a3440": BORDER, "#202833": BORDER, "#2d3640": BORDER, "#3a4450": BORDER,
    "#edf2f7": TEXT, "#eef2f7": TEXT, "#ffffff": TEXT,
    "#aeb8c2": TEXT_MUTED, "#708090": TEXT_MUTED,
    "#2e5fb8": ACCENT, "#3a71c6": ACCENT, "#2b6cb0": ACCENT, "#4f7cc0": ACCENT,
    "#1e4a9a": SELECTED, "#2f5274": SELECTED, "#24384f": SELECTED,
}


def map_legacy(color: str) -> str:
    return LEGACY_MAP.get(str(color).lower(), color)


# ---------------------------------------------------------------- スタイリング
def button(kind: str = "secondary", content_height: int | None = None,
           font_size: int | None = None) -> str:
    """Primary=主要操作 / Secondary=通常の操作 / Ghost=表示切替・設定。

    content_height は styleSheet 上の min/max/height（=外側寸法 - border 1px*2）。
    Qt は styleSheet の min/max-height を setFixedHeight より優先して使うため、
    枠に収める側でこの値を揃えて渡す必要がある。
    """
    kind = kind.lower()
    if kind == "primary":
        base = f"""
            background: {ACCENT};
            border: 1px solid {ACCENT};
            color: {ACCENT_TEXT};
        """
        hover = f"background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER};"
        pressed = f"background: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED};"
        disabled = f"background: {SURFACE_RAISED}; border-color: {BORDER}; color: {TEXT_MUTED};"
    elif kind == "ghost":
        base = f"""
            background: transparent;
            border: 1px solid transparent;
            color: {TEXT_MUTED};
        """
        hover = f"background: {GHOST_HOVER}; border-color: {BORDER}; color: {TEXT};"
        pressed = f"background: {GHOST_PRESSED}; border-color: {BORDER}; color: {TEXT};"
        disabled = f"background: transparent; border-color: transparent; color: {BORDER};"
    else:  # secondary
        base = f"""
            background: {SURFACE_RAISED};
            border: 1px solid {BORDER};
            color: {TEXT};
        """
        hover = f"background: {HOVER_BG}; border-color: {SURFACE_RAISED};"
        pressed = f"background: {PANEL_MAIN}; border-color: {BORDER};"
        disabled = f"background: {PANEL_SIDE}; border-color: {BORDER}; color: {TEXT_MUTED};"
    height = BUTTON_HEIGHT - 2 if content_height is None else content_height
    size = FONT_SIZE if font_size is None else font_size
    return f"""
        QPushButton {{
            {base}
            border-radius: {RADIUS_CONTROL}px;
            font-size: {size}px;
            padding: 0px {BUTTON_PADDING_X}px;
            min-height: {height}px;
            max-height: {height}px;
            height: {height}px;
        }}
        QPushButton:hover {{ {hover} }}
        QPushButton:pressed {{ {pressed} }}
        QPushButton:disabled {{ {disabled} }}
        QPushButton:checked {{
            background: {SELECTED};
            border: 1px solid {ACCENT};
            color: {TEXT};
        }}
    """


def icon_button(content_height: int | None = None) -> str:
    """32〜36px四方のトグル系ボタン（余白なし・角丸統一）。"""
    height = ICON_BUTTON_SIZE - 2 if content_height is None else content_height
    return f"""
        QPushButton {{
            background: transparent;
            border: 1px solid {BORDER};
            border-radius: {RADIUS_CONTROL}px;
            padding: 0px;
            min-height: {height}px;
            max-height: {height}px;
            height: {height}px;
        }}
        QPushButton:hover {{ background: {GHOST_HOVER}; border-color: {SURFACE_RAISED}; }}
        QPushButton:pressed {{ background: {GHOST_PRESSED}; }}
        QPushButton:checked {{ background: {SELECTED}; border-color: {ACCENT}; }}
    """


def danger_button(content_height: int | None = None) -> str:
    """削除系。パレット外の意味色を確実に残すため専用のままここに出す。"""
    height = BUTTON_HEIGHT - 2 if content_height is None else content_height
    return f"""
        QPushButton {{
            background: {DANGER_BG};
            border: 1px solid {DANGER};
            border-radius: {RADIUS_CONTROL}px;
            color: {DANGER_TEXT};
            font-size: {SMALL_SIZE + 1}px;
            padding: 0px {SPACE}px;
            min-height: {height}px;
            max-height: {height}px;
        }}
        QPushButton:hover {{ background: {DANGER}; color: {ACCENT_TEXT}; }}
        QPushButton:pressed {{ background: {DANGER_PRESSED}; border-color: {DANGER}; }}
        QPushButton:disabled {{
            background: {PANEL_SIDE};
            border-color: {BORDER};
            color: {TEXT_MUTED};
        }}
    """


def input_field(object_name: str | None = None, content_height: int | None = None) -> str:
    """入力欄。content_height は border 2px を引いた styleSheet 上の値（ツールバー用の低め指定用）。"""
    kinds = ["QLineEdit", "QTextEdit", "QPlainTextEdit", "QAbstractSpinBox"]
    if object_name:
        kinds = [f"{k}#{object_name}" for k in kinds]
    selector = ", ".join(kinds)
    # Qt QSS は `A, B:focus` の擬似クラスを B にだけ付ける（CSS と同じ）。
    # まとめて書くと A には状態なし＝常時適用になるので、:focus は各項目へ書く。
    focus_selector = ", ".join(f"{k}:focus" for k in kinds)
    height = INPUT_HEIGHT - 2 if content_height is None else content_height
    # 複数行エディタは伸びないので max-height は「1行控件と高さを揃えたいとき」だけ使う
    clamp = f"\n            max-height: {height}px;" if content_height is not None else ""
    return f"""
        {selector} {{
            background: {PANEL_MAIN};
            border: 1px solid {BORDER};
            border-radius: {RADIUS_CONTROL}px;
            color: {TEXT};
            font-size: {FONT_SIZE}px;
            padding: 0px {SPACE}px;
            min-height: {height}px;{clamp}
            selection-background-color: {SELECTED};
            selection-color: {TEXT};
        }}
        {focus_selector} {{ border: 1px solid {ACCENT}; }}
        QTextEdit::viewport, QPlainTextEdit::viewport, QLineEdit::viewport {{
            background: {PANEL_MAIN};
        }}
    """


def panel(object_name: str, bg: str = PANEL_SIDE, title: bool = False) -> str:
    return f"""
        QWidget#{object_name}, QFrame#{object_name} {{
            background: {bg};
            border: 1px solid {BORDER};
            border-radius: {RADIUS_PANEL}px;
        }}
    """


def heading(extra: str = "") -> str:
    return f"""
        QLabel {{
            color: {TEXT_MUTED};
            font-size: {HEADING_SIZE}px;
            font-weight: 600;
            {extra}
        }}
    """


def scrollbar() -> str:
    """細く控えめなスクロールバー（常時表示・太さ8px）。"""
    return f"""
        QScrollBar:vertical {{
            background: transparent;
            width: 10px;
            margin: 2px 2px 2px 0px;
        }}
        QScrollBar::handle:vertical {{
            background: {SURFACE_RAISED};
            border-radius: 4px;
            min-height: 28px;
        }}
        QScrollBar::handle:vertical:hover {{ background: {HOVER_BG}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        QScrollBar:horizontal {{
            background: transparent;
            height: 10px;
            margin: 0px 2px 2px 2px;
        }}
        QScrollBar::handle:horizontal {{
            background: {SURFACE_RAISED};
            border-radius: 4px;
            min-width: 28px;
        }}
        QScrollBar::handle:horizontal:hover {{ background: {HOVER_BG}; }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
    """


def checkbox() -> str:
    return f"""
        QCheckBox {{
            color: {TEXT};
            font-size: {FONT_SIZE}px;
            spacing: {SPACE_SM + 2}px;
            background: transparent;
        }}
        QCheckBox::indicator {{
            width: {CHECKBOX_SIZE}px;
            height: {CHECKBOX_SIZE}px;
            border: 1px solid {BORDER};
            border-radius: 4px;
            background: {PANEL_MAIN};
        }}
        QCheckBox::indicator:hover {{ border: 1px solid {ACCENT}; }}
        QCheckBox::indicator:checked {{
            background: {ACCENT};
            border: 1px solid {ACCENT};
            image: none;
        }}
    """


def scroll_area() -> str:
    """スクロールエリア自体は透明にして、中身の面板の色を通す。"""
    return "QScrollArea { background: transparent; border: none; }"


def note(color: str | None = None) -> str:
    """補助説明文（本文より一段小さく、 MUTED または意味色）。"""
    return f"color: {TEXT_MUTED if color is None else color}; font-size: {SMALL_SIZE + 1}px;"


def chip(bg: str | None = None, fg: str | None = None,
         height: int | None = None, radius: int | None = None) -> str:
    """小型ピル（px表示・件数・タグなど 22〜26px）。"""
    return f"""
        background: {PANEL_MAIN if bg is None else bg};
        color: {TEXT if fg is None else fg};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_CHIP if radius is None else radius}px;
        padding: 0px {SPACE_SM + 2}px;
        min-height: {CHIP_HEIGHT - 2 if height is None else height}px;
        max-height: {CHIP_HEIGHT - 2 if height is None else height}px;
        font-size: {SMALL_SIZE + 1}px;
    """


def slider() -> str:
    return f"""
        QSlider::groove:horizontal {{
            height: 4px;
            background: {BORDER};
            border-radius: 2px;
        }}
        QSlider::sub-page:horizontal {{
            background: {ACCENT};
            border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            width: 12px;
            margin: -5px 0px;
            border-radius: 6px;
            background: {TEXT};
            border: 1px solid {BORDER};
        }}
        QSlider::handle:horizontal:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT}; }}
        QSlider::handle:horizontal:pressed {{ background: {ACCENT}; }}
    """


def item_indicator() -> str:
    """QListWidget/QTreeWidget の item チェック印。
    ネイティブ印はダーク面の上で白く潰えて見えるため、面（fill）で状態を出す。"""
    return f"""
        QListView::indicator, QTreeView::indicator,
        QListWidget::indicator, QTreeWidget::indicator {{
            width: {CHECKBOX_SIZE}px;
            height: {CHECKBOX_SIZE}px;
            border: 1px solid {BORDER};
            border-radius: {RADIUS_CHIP}px;
            background: {PANEL_MAIN};
        }}
        QListView::indicator:hover, QTreeView::indicator:hover,
        QListWidget::indicator:hover, QTreeWidget::indicator:hover {{
            border: 1px solid {ACCENT};
        }}
        QListView::indicator:checked, QTreeView::indicator:checked,
        QListWidget::indicator:checked, QTreeWidget::indicator:checked {{
            background: {ACCENT};
            border: 1px solid {ACCENT};
            image: none;
        }}
        QListView::indicator:unchecked, QTreeView::indicator:unchecked,
        QListWidget::indicator:unchecked, QTreeWidget::indicator:unchecked {{
            image: none;
        }}
    """


def app_style() -> str:
    """QApplication に一度当てる基底スタイル。
    各ウィジェット個別の setStyleSheet は「上書きしたい所だけ」残す。"""
    return f"""
        QWidget {{
            background: {APP_BG};
            color: {TEXT};
            font-size: {FONT_SIZE}px;
        }}
        /* QLabel は常に透過。面板（PANEL_SIDE 等）に載ったときに背景の板だけ黒く
           塗れるのを避けるため。個別に塗りたいラベルは widget 側で上書きする。 */
        QLabel {{
            background: transparent;
            color: {TEXT};
        }}
        QMainWindow, QDialog {{ background: {APP_BG}; }}
        QToolTip {{
            background: {PANEL_MAIN};
            color: {TEXT};
            border: 1px solid {BORDER};
            border-radius: {RADIUS_CHIP}px;
            padding: 6px 8px;
            font-size: {SMALL_SIZE + 1}px;
        }}
        QMenu {{
            background: {PANEL_MAIN};
            border: 1px solid {BORDER};
            border-radius: {RADIUS_CONTROL}px;
            padding: {SPACE_SM}px;
        }}
        QMenu::item {{
            padding: 6px 22px 6px 12px;
            border-radius: {RADIUS_CHIP}px;
            color: {TEXT};
        }}
        QMenu::item:selected {{ background: {SELECTED}; color: {TEXT}; }}
        QMenu::item:disabled {{ color: {TEXT_MUTED}; }}
        QMenu::separator {{ height: 1px; background: {BORDER}; margin: {SPACE_SM}px 6px; }}
        QMenuBar {{ background: {APP_BG}; border: none; color: {TEXT}; }}
        QMenuBar::item {{ padding: 6px 10px; border-radius: {RADIUS_CHIP}px; background: transparent; }}
        QMenuBar::item:selected {{ background: {GHOST_HOVER}; }}
        QStatusBar {{
            background: {APP_BG};
            border-top: 1px solid {BORDER};
            color: {TEXT_MUTED};
            font-size: {SMALL_SIZE + 1}px;
        }}
        QStatusBar::item {{ border: none; }}
        QSplitter::handle {{ background: {BORDER}; }}
        QSplitter::handle:horizontal {{ width: 1px; }}
        QSplitter::handle:vertical {{ height: 1px; }}
        QListView, QTreeView, QListWidget, QTreeWidget {{
            background: {PANEL_MAIN};
            border: 1px solid {BORDER};
            border-radius: {RADIUS_PANEL}px;
            outline: 0;
            color: {TEXT};
        }}
        /* view 側の background は viewport（実際に item が並ぶ面）へは伝わらない。
           実測: viewport を名指ししないと行の下が palette Base（白）で塗られた。 */
        QListView::viewport, QTreeView::viewport, QListWidget::viewport, QTreeWidget::viewport {{
            background: {PANEL_MAIN};
        }}
        QListView::item, QTreeView::item {{ border-radius: {RADIUS_CHIP}px; }}
        QListView::item:selected, QTreeView::item:selected {{
            background: {SELECTED};
            color: {TEXT};
        }}
        QListView::item:hover:!selected, QTreeView::item:hover:!selected {{
            background: {HOVER_BG};
        }}
        QHeaderView::section {{
            background: {PANEL_SIDE};
            color: {TEXT_MUTED};
            border: none;
            border-bottom: 1px solid {BORDER};
            padding: 6px;
        }}
        QGroupBox {{
            background: {PANEL_SIDE};
            border: 1px solid {BORDER};
            border-radius: {RADIUS_PANEL}px;
            margin-top: {SPACE}px;
            padding-top: {SPACE}px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: {SPACE}px;
            color: {TEXT_MUTED};
            font-size: {HEADING_SIZE}px;
            font-weight: 600;
        }}
        QComboBox {{
            background: {PANEL_MAIN};
            border: 1px solid {BORDER};
            border-radius: {RADIUS_CONTROL}px;
            padding: 0px {SPACE}px;
            min-height: {INPUT_HEIGHT - 2}px;
            color: {TEXT};
        }}
        QComboBox:hover {{ border: 1px solid {SURFACE_RAISED}; }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox QAbstractItemView {{
            background: {PANEL_MAIN};
            border: 1px solid {BORDER};
            border-radius: {RADIUS_CONTROL}px;
            selection-background-color: {SELECTED};
            selection-color: {TEXT};
            outline: 0;
        }}
        {button("secondary")}
        {input_field()}
        {checkbox()}
        {item_indicator()}
        {scrollbar()}
    """
