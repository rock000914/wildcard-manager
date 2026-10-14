import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wildcard_manager.models import AppSettings
from wildcard_manager.repository import WildcardRepository
from wildcard_manager.api import ThumbnailApiClient
from wildcard_manager.new_wildcard_dialog import NewWildcardDialog


def main():
    app = QApplication([])
    root = Path(__file__).resolve().parent.parent
    settings = AppSettings.default(root)
    # Use a transient DB path
    db_path = root / '.tmp' / 'ui_smoke.db'
    db_path.parent.mkdir(parents=True, exist_ok=True)
    repo = WildcardRepository(db_path)
    api = ThumbnailApiClient()
    try:
        dlg = NewWildcardDialog(settings, repo, api, {})
        dlg.show()
        app.processEvents()
        dlg.close()
        print('UI smoke test: dialog created and closed successfully')
    except Exception as e:
        print('UI smoke test failed:', e)
        raise


if __name__ == '__main__':
    main()
