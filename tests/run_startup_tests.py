"""Startup tests for repository.

Checks performed:
- Detect function-local (indented) imports (tools/check_local_imports.py)
- Compile all .py files to catch SyntaxError/IndentationError
- Try importing key modules: `wildcard_manager.main_window`, `wildcard_manager.new_wildcard_dialog`

Exit code 0 on success, non-zero on failure.
"""
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_FILES = list(ROOT.rglob('*.py'))

def run_check_local_imports():
    p = ROOT / 'tools' / 'check_local_imports.py'
    if not p.exists():
        print('tools/check_local_imports.py not found, skipping.')
        return True
    print('Running check_local_imports.py...')
    r = subprocess.run([sys.executable, str(p)], cwd=ROOT)
    return r.returncode == 0

def compile_all():
    print('Compiling python files...')
    try:
        import py_compile
        for f in PY_FILES:
            # skip tests themselves to avoid recursive complexity
            py_compile.compile(str(f), doraise=True)
    except Exception as e:
        print('Compilation error:', e)
        return False
    return True

def try_imports():
    print('Trying to import critical modules...')
    sys.path.insert(0, str(ROOT))
    try:
        import wildcard_manager.main_window as mw
        import wildcard_manager.new_wildcard_dialog as nd
    except Exception as e:
        print('Import failed:')
        import traceback
        traceback.print_exc()
        return False
    return True


def main():
    ok = True
    ok = ok and run_check_local_imports()
    ok = ok and compile_all()
    ok = ok and try_imports()
    if not ok:
        print('\nStartup tests FAILED')
        sys.exit(1)
    print('\nStartup tests PASSED')
    sys.exit(0)

if __name__ == '__main__':
    main()
