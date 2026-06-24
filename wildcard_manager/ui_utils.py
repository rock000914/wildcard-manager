"""共有UIヘルパー。

main_window.py と new_wildcard_dialog.py の両方で複製されていた
コンテキストメニュー日本語化処理やパス操作ヘルパーを集約する。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QLineEdit, QMenu, QPlainTextEdit, QTextEdit, QWidget


def _delete_selected_text(widget) -> None:
    """テキストウィジェットの選択範囲を削除する。
    QLineEdit/QTextEdit/QPlainTextEdit いずれでも動作する。
    以前は widget.deleteLater() を呼んでしまい、ウィジェット自体を破棄していた（重大バグ）。
    """
    if hasattr(widget, "textCursor"):
        cursor = widget.textCursor()
        if cursor is not None and cursor.hasSelection():
            cursor.removeSelectedText()
            if hasattr(widget, "setTextCursor"):
                widget.setTextCursor(cursor)
    elif hasattr(widget, "del_"):  # QLineEdit 用
        widget.del_()


def setup_japanese_context_menu(widget: QWidget) -> None:
    """QLineEdit/QTextEdit/QPlainTextEdit のコンテキストメニューを日本語化する。

    以前の実装は「削除」アクションの callback に ``w.deleteLater`` を渡しており、
    右クリック→削除で入力フィールドそのものが消える重大バグがあった。
    ここでは選択テキスト削除に修正している。
    """

    def show_menu(w: QWidget, pos) -> None:
        menu = QMenu(w)
        menu.addAction("元に戻す", w.undo).setShortcut(QKeySequence("Ctrl+Z"))
        menu.addAction("やり直し", w.redo).setShortcut(QKeySequence("Ctrl+Y"))
        menu.addSeparator()
        menu.addAction("切り取り", w.cut).setShortcut(QKeySequence("Ctrl+X"))
        menu.addAction("コピー", w.copy).setShortcut(QKeySequence("Ctrl+C"))
        menu.addAction("貼り付け", w.paste).setShortcut(QKeySequence("Ctrl+V"))
        menu.addSeparator()
        menu.addAction("削除", lambda w=w: _delete_selected_text(w))
        menu.addSeparator()
        menu.addAction("すべて選択", w.selectAll).setShortcut(QKeySequence("Ctrl+A"))
        menu.exec(w.mapToGlobal(pos))

    class _ContextMenuFilter(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.ContextMenu:
                show_menu(obj, event.pos())
                return True
            return super().eventFilter(obj, event)

    filt = _ContextMenuFilter(widget)
    widget._context_menu_filter = filt
    widget.installEventFilter(filt)


def open_in_file_manager(path: Path | str, *, select: bool = False) -> bool:
    """クロスプラットフォームでファイル/フォルダをファイラで開く。

    Windows では ``explorer.exe /select,path`` 相当、それ以外では
    ``xdg-open`` (Linux) / ``open`` (macOS) を使う。失敗時は False を返す。
    """
    p = Path(path)
    if sys.platform == "win32":
        import subprocess
        try:
            if select and p.exists():
                subprocess.Popen(["explorer.exe", "/select,", str(p)])
            elif p.exists():
                subprocess.Popen(["explorer.exe", str(p)])
            else:
                return False
            return True
        except Exception:
            return False
    elif sys.platform == "darwin":
        import subprocess
        try:
            args = ["open"]
            if select and p.is_file():
                args += ["-R", str(p)]
            else:
                args.append(str(p.parent if p.is_file() else p))
            subprocess.Popen(args)
            return True
        except Exception:
            return False
    else:
        import subprocess
        try:
            target = str(p.parent if select and p.is_file() else (p if p.exists() else p.parent))
            subprocess.Popen(["xdg-open", target])
            return True
        except Exception:
            return False


def is_path_within(child: Path, parent: Path) -> bool:
    """``child`` が ``parent`` 配下（同じ場合も含む）かどうかを安全に判定する。

    ``parent in child.parents`` はシンボリックリンク解決後のパスで判定する。
    """
    try:
        child_res = child.resolve()
        parent_res = parent.resolve()
    except Exception:
        return False
    return child_res == parent_res or parent_res in child_res.parents
