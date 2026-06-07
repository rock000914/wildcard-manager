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
    generation_checkpoint_name: str = ""
    generation_extra_payload_json: str = "{}"
    include_lora_on_copy: bool = True
    thumbnail_size: int = 260
    window_width: int = 1780
    window_height: int = 1040
    window_maximized: bool = False
    splitter_sizes: list[int] = field(default_factory=lambda: [260, 1020, 520])
    detail_splitter_sizes: list[int] = field(default_factory=lambda: [520, 420])
    last_folder: str = ""
    sort_mode: str = "name_asc"

    @classmethod
    def default(cls, app_dir: Path) -> "AppSettings":
        library_root = app_dir / "library" / "wildcards"
        thumbnail_root = app_dir / "library" / "thumbnails"
        return cls(
            library_root=str(library_root),
            thumbnail_root=str(thumbnail_root),
            source_wildcard_root=r"H:\StabilityMatrix\Packages\Neo\extensions\sd-dynamic-prompts\wildcards",
            source_thumbnail_root=r"H:\StabilityMatrix\Packages\Neo\extensions\wildcard-gallery\cards",
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
        return Path(self.abs_path)
