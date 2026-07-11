from pathlib import Path
import sys
from ctypes import windll

# If launched via python.exe, detach and hide the inherited console as early as possible.
try:
    hwnd = windll.kernel32.GetConsoleWindow()
    if hwnd:
        windll.user32.ShowWindow(hwnd, 0)
        windll.kernel32.FreeConsole()
except Exception:
    pass

from PySide6.QtGui import QFontDatabase, QIcon, QFont
from PySide6.QtWidgets import QApplication

from wildcard_manager.main_window import MainWindow

UI_FONT_FAMILIES = ["Yu Gothic UI", "Yu Gothic", "Meiryo UI", "Meiryo", "Segoe UI", "Noto Sans CJK JP", "MS Gothic"]


def _choose_ui_font_family() -> str:
    available = set(QFontDatabase.families())
    for family in UI_FONT_FAMILIES:
        if family in available:
            return family
    return QFontDatabase.systemFont(QFontDatabase.GeneralFont).family()


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    from wildcard_manager.logging_setup import setup_logging
    setup_logging(app_dir)
    import logging
    log = logging.getLogger("wildcard_manager")
    app_id = "CompassLab.WildcardManager"
    try:
        windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        log.debug("Failed to set AppUserModelID", exc_info=True)
    try:
        hwnd = windll.kernel32.GetConsoleWindow()
        if hwnd:
            windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        log.debug("Failed to hide console window", exc_info=True)
    app = QApplication(sys.argv)
    app.setApplicationName("Wildcard Manager")
    app.setDesktopFileName(app_id)
    app_font = QFont(_choose_ui_font_family())
    app_font.setPointSize(9)
    app.setFont(app_font)
    icon_path = app_dir / "assets" / "wildcard_manager_icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow(app_dir)
    if icon_path.exists():
        window.setWindowIcon(QIcon(str(icon_path)))
    if window.settings.window_maximized:
        window.showMaximized()
    else:
        window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
