from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from .models import AppSettings, WildcardEntry
from .repository import ensure_thumbnail_destination


class ThumbnailApiClient:
    def test_connection(self, settings: AppSettings) -> None:
        response = requests.get(
            f"{settings.api_base_url.rstrip('/')}/sdapi/v1/options",
            timeout=settings.api_timeout_sec,
        )
        response.raise_for_status()

    def list_checkpoints(self, settings: AppSettings) -> list[str]:
        response = requests.get(
            f"{settings.api_base_url.rstrip('/')}/sdapi/v1/sd-models",
            timeout=settings.api_timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []

        titles: list[str] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            model_name = item.get("model_name")
            hash_value = item.get("hash")
            if isinstance(title, str) and title.strip():
                titles.append(title.strip())
            elif isinstance(model_name, str) and model_name.strip():
                if isinstance(hash_value, str) and hash_value.strip():
                    titles.append(f"{model_name.strip()} [{hash_value.strip()}]")
                else:
                    titles.append(model_name.strip())
        return sorted(dict.fromkeys(titles))

    def generate_thumbnail(self, settings: AppSettings, entry: WildcardEntry) -> Path:
        prompt = self._compose_prompt(settings.generation_prompt_prefix, self._pick_prompt(entry.content))
        if not prompt:
            raise ValueError("サムネイル生成に使うプロンプトがありません。")

        payload: dict[str, Any] = {
            "prompt": prompt,
            "negative_prompt": settings.generation_negative_prompt,
            "steps": settings.generation_steps,
            "cfg_scale": settings.generation_cfg_scale,
            "width": settings.generation_width,
            "height": settings.generation_height,
            "sampler_name": settings.generation_sampler_name,
            "batch_size": 1,
            "n_iter": 1,
        }

        override_settings: dict[str, Any] = {}
        if settings.generation_checkpoint_name.strip():
            override_settings["sd_model_checkpoint"] = settings.generation_checkpoint_name.strip()
        if override_settings:
            payload["override_settings"] = override_settings

        extra_payload = json.loads(settings.generation_extra_payload_json or "{}")
        if not isinstance(extra_payload, dict):
            raise ValueError("追加 API payload は JSON object で入力してください。")
        payload.update(extra_payload)

        response = requests.post(
            f"{settings.api_base_url.rstrip('/')}/sdapi/v1/txt2img",
            json=payload,
            timeout=settings.api_timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        images = data.get("images") or []
        if not images:
            raise ValueError("API から画像が返りませんでした。")

        image_bytes = base64.b64decode(images[0].split(",", 1)[-1])
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        destination = ensure_thumbnail_destination(Path(settings.thumbnail_root), entry.rel_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, "WEBP", quality=82, method=6)
        return destination

    @staticmethod
    def _compose_prompt(prefix: str, body: str) -> str:
        prefix = prefix.strip()
        body = body.strip()
        if prefix and body:
            return f"{prefix}, {body}"
        return prefix or body

    @staticmethod
    def _pick_prompt(content: str) -> str:
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                return line
        return ""
