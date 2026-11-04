"""コピー時 LoRA 実在性チェック（MainWindow._missing_copy_lora_names）の回帰テスト。

GUI を起動せず、MainWindow の該当メソッドだけを検証用スタブへ束縛して実行する。
LoRAフォルダは一時ディレクトリに作り、H:\\StabilityMatrix へ依存しない。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wildcard_manager.main_window import MainWindow  # noqa: E402


class FakeView:
    def viewport(self):
        return self

    def update(self):
        pass


class FakeEntry:
    def __init__(self, lora_names):
        self.lora_names = lora_names


class Harness:
    """MainWindow._missing_copy_lora_names / _refresh_lora_weight_index が触る属性だけを持つ。"""

    _lora_tail_name = staticmethod(MainWindow._lora_tail_name)
    _walk_lora_files = staticmethod(MainWindow._walk_lora_files)
    _lora_name_present = classmethod(MainWindow._lora_name_present.__func__)
    _compute_lost_lora_names = classmethod(MainWindow._compute_lost_lora_names.__func__)
    _refresh_lora_weight_index = MainWindow._refresh_lora_weight_index
    _missing_copy_lora_names = MainWindow._missing_copy_lora_names

    def __init__(self, roots, entries=()):
        self.roots = roots
        self.entries = list(entries)
        self.card_view = FakeView()
        self._lost_lora_names = set()

    def _lora_search_roots(self):
        return list(self.roots)


def make_root(base: Path, names) -> Path:
    root = base / "Lora"
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / f"{name}.safetensors").write_bytes(b"")
    return root


def test_present_names_pass():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(Path(tmp), ["aria_ilxl", "car_sex"])
        h = Harness([root])
        got = h._missing_copy_lora_names("<lora:aria_ilxl:1>, <lora:car_sex:0.6>, 1girl")
        assert got == [], f"実在する名前をLOSTと報告した: {got}"
        print("PASS 実在名はそのまま通る")


def test_missing_name_reported_in_original_spelling():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(Path(tmp), ["aria_ilxl"])
        h = Harness([root])
        got = h._missing_copy_lora_names("<lora:AgaWa-000019:1>, 1girl")
        assert got == ["AgaWa-000019"], f"元の表記で報告しない: {got}"
        print("PASS 削除済みLoRAは元の表記のまま報告")


def test_folder_prefixed_reference_matches_by_tail():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(Path(tmp), ["aria_ilxl"])
        h = Harness([root])
        assert h._missing_copy_lora_names("<lora:sub/aria_ilxl:1>") == []
        assert h._missing_copy_lora_names("<lora:sub/nope_ilxl:1>") == ["sub/nope_ilxl"]
        print("PASS `<lora:sub/name>` は末尾名で実在判定")


def test_invisible_characters_in_reference():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(Path(tmp), ["mai_cammysf2"])
        h = Harness([root])
        got = h._missing_copy_lora_names("<lora:﻿mai_cammysf2:1>")
        assert got == [], f"BOM付き参照が誤LOSTになった: {got}"
        print("PASS 参照側のBOM/ゼロ幅文字は正規化して照合")


def test_no_lora_text_skips_walk():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(Path(tmp), ["aria_ilxl"])
        h = Harness([root])
        assert h._missing_copy_lora_names("1girl, solo, cat ears") == []
        assert h._lost_lora_names == set(), "LoRAタグ無しでwalkまで走った"
        print("PASS LoRAタグ無しテキストはディスクを見ない")


def test_lora_root_unset_is_not_warned():
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness([Path(tmp) / "NotThere"])
        assert h._missing_copy_lora_names("<lora:anything:1>") == []
        assert h._lost_lora_names == set(), "存在しないフォルダをLOST集合に混ぜた"
        print("PASS LoRAフォルダ未設定では警告しない（判定不能）")


def test_deleted_file_detected_at_copy_time():
    """コピー時は索引を信じず必ずwalkし直す、の核心回帰。

    QFileSystemWatcher はワイルドカードライブラリ側しか見ないので、
    起動後に消えたLoRAは索引では古いまま。それを拾えることを確認する。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(Path(tmp), ["aria_ilxl", "doomed_ilxl"])
        entry = FakeEntry(["aria_ilxl", "doomed_ilxl"])
        h = Harness([root], entries=[entry])
        h._refresh_lora_weight_index()
        assert h._lost_lora_names == set(), f"削除前はLOSTなしのはず: {h._lost_lora_names}"
        (root / "doomed_ilxl.safetensors").unlink()
        got = h._missing_copy_lora_names("<lora:doomed_ilxl:1>")
        assert got == ["doomed_ilxl"], f"削除後の検出に失敗: {got}"
        assert h._lost_lora_names == {"doomed_ilxl"}, "カードのLOST集合が追従していない"
        print("PASS 起動後に削除されたLoRAをコピー時に検出し、LOST集合も更新")


def test_restored_file_no_longer_reported():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(Path(tmp), ["aria_ilxl"])
        entry = FakeEntry(["back_ilxl"])
        h = Harness([root], entries=[entry])
        assert h._missing_copy_lora_names("<lora:back_ilxl:1>") == ["back_ilxl"]
        (root / "back_ilxl.safetensors").write_bytes(b"")
        assert h._missing_copy_lora_names("<lora:back_ilxl:1>") == []
        assert h._lost_lora_names == set(), "復元後もLOSTに残ったまま"
        print("PASS 戻来たLoRAは次回コピーから警告しなくなる")


def test_lost_set_is_mutated_in_place():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(Path(tmp), ["aria_ilxl"])
        h = Harness([root], entries=[FakeEntry(["gone_ilxl"])])
        shared = h._lost_lora_names
        h._refresh_lora_weight_index()
        assert shared is h._lost_lora_names, "setを差し替えて共有参照を切った"
        assert shared == {"gone_ilxl"}
        print("PASS _lost_lora_names は中身その入れ替え（list_model との共有が壊れない）")


if __name__ == "__main__":
    for fn in [
        test_present_names_pass,
        test_missing_name_reported_in_original_spelling,
        test_folder_prefixed_reference_matches_by_tail,
        test_invisible_characters_in_reference,
        test_no_lora_text_skips_walk,
        test_lora_root_unset_is_not_warned,
        test_deleted_file_detected_at_copy_time,
        test_restored_file_no_longer_reported,
        test_lost_set_is_mutated_in_place,
    ]:
        fn()
    print("\nall copy-lora checks passed")
