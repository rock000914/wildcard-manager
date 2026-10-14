import json
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

def test_favorites_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        fav_file = Path(tmpdir) / "favorites.json"
        
        fav_paths = {"path/to/card1.txt", "path/to/card2.txt"}
        with fav_file.open("w", encoding="utf-8") as f:
            json.dump(sorted(fav_paths), f, ensure_ascii=False, indent=2)
            
        with fav_file.open("r", encoding="utf-8") as f:
            loaded = set(json.load(f))
            
        assert loaded == fav_paths, f"Expected {fav_paths}, got {loaded}"
        print("favorites.json test passed successfully!")

if __name__ == "__main__":
    test_favorites_json()
