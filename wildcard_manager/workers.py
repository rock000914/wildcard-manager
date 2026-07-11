"""Background worker classes extracted from main_window.py."""

from __future__ import annotations

import base64
import copy as _copy
import io
import json
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .api import ThumbnailApiClient
from .models import AppSettings, WildcardEntry
from .repository import (
    OperationCancelledError,
    WildcardRepository,
    _safe_resolve_path,
)


def collect_txt_files(root: Path, include_subfolders: bool) -> list[Path]:
    if not root.exists():
        return []
    files = root.rglob("*.txt") if include_subfolders else root.glob("*.txt")
    return sorted((path for path in files if path.is_file()), key=lambda path: path.as_posix().lower())


def extract_prompt_line(text: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            return line
    return ""

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RandomGenerationRequest:
    prompt: str
    negative_prompt: str
    output_root: str
    save_mode: str
    wildcard_root: str
    wildcard_include_subfolders: bool
    wildcard_per_generation: bool
    character_text: str
    width: int
    height: int
    steps: int
    batch_count: int
    batch_size: int
    cfg_scale: float
    sampler_name: str
    seed_mode: str
    seed: int
    checkpoint_name: str
    adetailer_enabled: bool


class RandomGenerationWorker(QObject):
    finished = Signal(list, str)
    failed = Signal(str)

    def __init__(self, settings: AppSettings, api: ThumbnailApiClient, repo: WildcardRepository, request: RandomGenerationRequest):
        super().__init__()
        self.settings = settings
        self.api = api
        self.repo = repo
        self.request = request

    def _pick_random_prompt(self, root_text: str, include_subfolders: bool) -> tuple[str, str]:
        root = Path(root_text)
        candidates = collect_txt_files(root, include_subfolders)
        if not candidates:
            raise FileNotFoundError(f"ワイルドカード候補が見つかりません: {root}")
        path = random.choice(candidates)
        content = self.repo.load_entry_content(path)
        prompt = extract_prompt_line(content)
        if not prompt:
            raise ValueError(f"プロンプトとして使える行がありません: {path}")
        return prompt, str(path)

    def _build_prompt(self, wildcard_text: str) -> str:
        prompt_parts = [self.request.prompt.strip(), self.request.character_text.strip(), wildcard_text]
        prompt_parts = [part for part in prompt_parts if part]
        prompt = ThumbnailApiClient._compose_prompt("", ", ".join(prompt_parts))
        if not prompt:
            raise ValueError("生成用プロンプトが空です。")
        return prompt

    def _build_payload(self, prompt: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "prompt": prompt,
            "negative_prompt": self.request.negative_prompt.strip(),
            "steps": self.request.steps,
            "cfg_scale": self.request.cfg_scale,
            "width": self.request.width,
            "height": self.request.height,
            "sampler_name": self.request.sampler_name,
            "batch_size": self.request.batch_size,
            "n_iter": self.request.batch_count,
            "seed": self.request.seed if self.request.seed_mode == "fixed" else -1,
        }

        if self.request.checkpoint_name.strip():
            payload["override_settings"] = {
                "sd_model_checkpoint": self.request.checkpoint_name.strip(),
            }

        if self.request.adetailer_enabled:
            payload["alwayson_scripts"] = {
                "ADetailer": {
                    "args": [
                        {
                            "ad_model": "face_yolov8n.pt",
                        }
                    ]
                }
            }

        return payload

    def _generate_and_save_batch(
        self,
        output_root: Path,
        stamp: str,
        batch_size: int,
        batch_count: int,
        generation_index: int | None = None,
    ) -> tuple[list[str], str]:
        wildcard_text, wildcard_source = self._pick_random_prompt(
            self.request.wildcard_root,
            self.request.wildcard_include_subfolders,
        )
        prompt = self._build_prompt(wildcard_text)
        payload = self._build_payload(prompt)
        payload["batch_size"] = batch_size
        payload["n_iter"] = batch_count
        data = self.api.request_txt2img(self.settings, payload)
        images = data.get("images") or []
        if not images:
            raise ValueError("API から画像が返りませんでした。")
        parameters_text, extra_metadata = self._build_png_metadata(
            prompt=prompt,
            negative_prompt=self.request.negative_prompt.strip(),
            wildcard_text=wildcard_text,
            wildcard_source=wildcard_source,
            payload=payload,
            api_data=data,
        )
        saved_paths: list[str] = []
        for image_index, image_data in enumerate(images, start=1):
            image_bytes = base64.b64decode(str(image_data).split(",", 1)[-1])
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            if generation_index is not None:
                filename = f"random_{stamp}_{uuid.uuid4().hex[:8]}_{generation_index + 1:03d}_{image_index:02d}.png"
            else:
                filename = f"random_{stamp}_{uuid.uuid4().hex[:8]}_{image_index:02d}.png"
            destination = output_root / filename
            pnginfo = PngInfo()
            pnginfo.add_text("parameters", parameters_text)
            for key, value in extra_metadata.items():
                pnginfo.add_text(key, value)
            image.save(destination, "PNG", pnginfo=pnginfo)
            saved_paths.append(str(destination))
        return saved_paths, prompt

    @Slot()
    def run(self) -> None:
        try:
            output_root = _safe_resolve_path(Path(self.request.output_root or self.settings.library_root))
            if self.request.save_mode == "date_folder":
                output_root = output_root / datetime.now().strftime("%Y-%m-%d")
            output_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            saved_paths: list[str] = []
            if self.request.wildcard_per_generation:
                total = max(1, self.request.batch_count * self.request.batch_size)
                for generation_index in range(total):
                    batch_paths, prompt = self._generate_and_save_batch(
                        output_root, stamp, batch_size=1, batch_count=1, generation_index=generation_index
                    )
                    saved_paths.extend(batch_paths)
            else:
                saved_paths, prompt = self._generate_and_save_batch(
                    output_root, stamp, batch_size=self.request.batch_size, batch_count=self.request.batch_count
                )
            self.finished.emit(saved_paths, prompt)
        except Exception as exc:
            self.failed.emit(str(exc))

    @staticmethod
    def _build_png_metadata(
        *,
        prompt: str,
        negative_prompt: str,
        wildcard_text: str,
        wildcard_source: str,
        payload: dict[str, object],
        api_data: dict[str, object],
    ) -> tuple[str, dict[str, str]]:
        if isinstance(api_data.get("parameters"), str) and api_data["parameters"].strip():
            parameters_text = str(api_data["parameters"]).strip()
        else:
            parameters_text = "\n".join(
                [
                    prompt,
                    f"Negative prompt: {negative_prompt}",
                    (
                        "Steps: {steps}, Sampler: {sampler}, CFG scale: {cfg}, Seed: {seed}, "
                        "Size: {width}x{height}, Batch size: {batch_size}, Batch count: {batch_count}"
                    ).format(
                        steps=payload.get("steps", ""),
                        sampler=payload.get("sampler_name", ""),
                        cfg=payload.get("cfg_scale", ""),
                        seed=payload.get("seed", ""),
                        width=payload.get("width", ""),
                        height=payload.get("height", ""),
                        batch_size=payload.get("batch_size", ""),
                        batch_count=payload.get("n_iter", ""),
                    ),
                ]
            )

        extra_metadata = {
            "random_wildcard": wildcard_text,
            "random_wildcard_source": wildcard_source,
        }
        if isinstance(api_data.get("info"), str) and api_data["info"].strip():
            extra_metadata["random_generation_info"] = str(api_data["info"]).strip()
        return parameters_text, extra_metadata


class CachedEntriesLoader(QObject):
    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, db_path: Path, settings: AppSettings):
        super().__init__()
        self.db_path = db_path
        self.settings = settings

    @Slot()
    def run(self) -> None:
        try:
            repo = WildcardRepository(self.db_path)
            entries = repo.load_entries_summary(self.settings)
            self.finished.emit(entries)
        except Exception as exc:
            self.failed.emit(str(exc))


class RescanWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(self, db_path: Path, settings):
        super().__init__()
        self.db_path = db_path
        self.settings = settings
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        try:
            repo = WildcardRepository(self.db_path)

            db_count = repo.db_entry_count()
            if db_count > 0:
                disk_count = repo.count_txt_files_on_disk(self.settings)
                if disk_count == db_count:
                    self.finished.emit({"scanned": db_count, "updated": 0, "deleted": 0, "quick": True})
                    return

            _PROGRESS_INTERVAL = 50
            _last_emitted: list[int] = [-1]

            def _progress(i: int, total: int, name: str) -> None:
                if self._cancelled:
                    raise OperationCancelledError()
                if (i == 1 or i == total or (i - _last_emitted[0]) >= _PROGRESS_INTERVAL):
                    _last_emitted[0] = i
                    self.progress.emit(i, total, name)

            stats = repo.scan_library(
                self.settings,
                progress=_progress,
            )
            self.finished.emit(stats)
        except OperationCancelledError:
            self.finished.emit({"scanned": 0, "updated": 0, "deleted": 0, "cancelled": True})
        except Exception as exc:
            self.failed.emit(str(exc))


class UnmadeWildcardScanWorker(QObject):
    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, lora_root: str, haystack: str):
        super().__init__()
        self.lora_root = lora_root
        self.haystack = haystack

    @staticmethod
    def _find_lora_preview(directory: Path, stem: str) -> Path | None:
        for ext in [".preview.png", ".preview.jpg", ".preview.jpeg", ".preview.webp", ".png", ".jpg", ".jpeg", ".webp"]:
            candidate = directory / f"{stem}{ext}"
            if candidate.exists():
                return candidate
        return None

    @Slot()
    def run(self) -> None:
        try:
            lora_root = Path(self.lora_root)
            if not lora_root.exists():
                self.finished.emit([])
                return

            results = []
            for safetensors in lora_root.rglob("*.safetensors"):
                stem = safetensors.stem
                cm_info = safetensors.parent / f"{stem}.cm-info.json"
                if not cm_info.exists():
                    continue

                if stem.lower() in self.haystack:
                    continue

                preview = self._find_lora_preview(safetensors.parent, stem)

                try:
                    with open(cm_info, "r", encoding="utf-8") as f:
                        cm_data = json.load(f)
                except Exception:
                    log.debug("Failed to load cm-info: %s", cm_info, exc_info=True)
                    continue

                results.append({
                    "path": str(safetensors),
                    "name": stem,
                    "preview": str(preview) if preview else None,
                    "trained_words": cm_data.get("TrainedWords", []),
                    "model_name": cm_data.get("ModelName", ""),
                })

            self.finished.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))


class ApiMonitorWorker(QThread):
    state_changed = Signal(str)
    POLL_INTERVAL_MS = 5000

    def __init__(self, api: ThumbnailApiClient, get_settings, parent=None):
        super().__init__(parent)
        self.api = api
        self._get_settings = get_settings
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        while self._running:
            settings = self._get_settings()
            try:
                self.api.test_connection(settings)
                try:
                    generating = self.api.is_generating(settings)
                except Exception:
                    log.debug("Failed to check generation state", exc_info=True)
                    generating = False
                self.state_changed.emit("generating" if generating else "connected")
            except Exception:
                log.debug("API connection check failed", exc_info=True)
                self.state_changed.emit("off")
            slept = 0
            while slept < self.POLL_INTERVAL_MS and self._running:
                self.msleep(100)
                slept += 100


class MissingThumbnailBatchWorker(QObject):
    progress = Signal(int, int, str)
    item_finished = Signal(object)
    item_failed = Signal(str, str)
    finished = Signal(int, int)
    cancelled = Signal()

    def __init__(self, db_path: Path, settings: AppSettings,
                 api: ThumbnailApiClient,
                 entries: list[WildcardEntry],
                 use_random_wildcard: bool):
        super().__init__()
        self.db_path = db_path
        self.settings = settings
        self.api = api
        self.entries = list(entries)
        self.use_random_wildcard = use_random_wildcard
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        total = len(self.entries)
        done = 0
        worker_settings = _copy.copy(self.settings)
        worker_settings.thumbnail_random_wildcard = self.use_random_wildcard
        for index, entry in enumerate(self.entries, start=1):
            if self._cancelled:
                break
            self.progress.emit(index, total, entry.name)
            try:
                self.api.generate_thumbnail(worker_settings, entry)
                repo = WildcardRepository(self.db_path)
                refreshed = repo.refresh_entry(self.settings, Path(entry.abs_path))
                done += 1
                self.item_finished.emit(refreshed)
            except Exception as exc:
                self.item_failed.emit(entry.abs_path, str(exc))
        if self._cancelled:
            self.cancelled.emit()
        else:
            self.finished.emit(done, total)
