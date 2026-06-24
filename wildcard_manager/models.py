from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class AppSettings:
    library_root: str
    thumbnail_root: str
    source_wildcard_root: str
    source_thumbnail_root: str
    api_base_url: str = "http://127.0.0.1:7860"
    api_timeout_sec: int = 120
    generation_prompt_prefix: str = ""
    generation_negative_prompt: str = ""
    generation_width: int = 512
    generation_height: int = 768
    generation_steps: int = 20
    generation_cfg_scale: float = 7.0
    generation_sampler_name: str = "Euler a"
    generation_extra_payload_json: str = "{}"
    thumbnail_random_wildcard: bool = False
    thumbnail_random_wildcard_root: str = ""
    include_lora_on_copy: bool = True
    thumbnail_size: int = 260
    random_output_root: str = ""
    random_save_mode: str = "date_folder"
    random_concept_root: str = ""
    random_concept_include_subfolders: bool = True
    random_wildcard_per_generation: bool = False
    random_character_root: str = ""
    random_character_include_subfolders: bool = True
    random_prompt: str = ""
    random_negative_prompt: str = ""
    random_width: int = 512
    random_height: int = 768
    random_steps: int = 20
    random_batch_count: int = 1
    random_batch_size: int = 1
    random_cfg_scale: float = 7.0
    random_sampler_name: str = "Euler a"
    random_seed_mode: str = "random"
    random_seed: int = -1
    random_checkpoint_name: str = ""
    generation_adetailer_enabled: bool = False
    random_adetailer_enabled: bool = False
    window_width: int = 1780
    window_height: int = 1040
    window_maximized: bool = False
    splitter_sizes: list[int] = field(default_factory=lambda: [260, 1020, 520])
    detail_splitter_sizes: list[int] = field(default_factory=lambda: [520, 420])
    last_folder: str = ""
    sort_mode: str = "name_asc"
    sort_key: str = "name"
    sort_order: str = "asc"
    lora_root: str = ""
    scroll_step: int = 30
    # 新規ワイルドカードダイアログで追加されたカスタムタブ（キャラクタータブ以外）の設定。
    # [{"name": "タブ名", "save_path": "/path/to/folder"}, ...]
    # アプリ再起動後もダイアログを開いたときに復元するため config.json に保存する。
    new_wildcard_custom_tabs: list[dict] = field(default_factory=list)

    @classmethod
    def default(cls, app_dir: Path) -> "AppSettings":
        """デフォルト設定を構築する。

        H4 修正: 以前は ``H:\\StabilityMatrix\\...`` のように開発者個人の
        Windows 環境依存パスがデフォルトとして埋め込まれており、他ユーザー
        環境では必ず存在しないパスになっていた。配布時に問題になるため、
        空文字に変更し、初回起動後にユーザーが設定ダイアログで入力する想定。
        """
        library_root = app_dir / "library" / "wildcards"
        thumbnail_root = app_dir / "library" / "thumbnails"
        return cls(
            library_root=str(library_root),
            thumbnail_root=str(thumbnail_root),
            source_wildcard_root="",
            source_thumbnail_root="",
            random_output_root=str(app_dir / "generated"),
            random_concept_root=str(library_root / "Position"),
            random_character_root=str(library_root),
        )


@dataclass(slots=True)
class WildcardEntry:
    rel_path: str
    abs_path: str
    folder: str
    name: str
    stem: str
    content: str = ""
    first_line: str = ""
    line_count: int = 0
    custom_tags: list[str] = field(default_factory=list)
    lora_names: list[str] = field(default_factory=list)
    thumbnail_path: str | None = None
    has_thumbnail: bool = False
    search_text: str = ""

    @property
    def path_obj(self) -> Path:
        """未使用だが外部互換性のために残す（L1 修正候補だったが念のため残置）。

        呼び出し元が現状無いため将来的に削除可能。
        """
        return Path(self.abs_path)
