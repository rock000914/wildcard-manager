from __future__ import annotations

import base64
import io
import json
from pathlib import Path

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

    def generate_thumbnail(self, settings: AppSettings, entry: WildcardEntry) -> Path:
        prompt = self._compose_prompt(settings.generation_prompt_prefix, self._pick_prompt(entry.content))
        if not prompt:
            raise ValueError("サムネイル生成に使える行がありません。")

        payload = {
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

        extra_payload = json.loads(settings.generation_extra_payload_json or "{}")
        if not isinstance(extra_payload, dict):
            raise ValueError("追加 API payload は JSON object である必要があります。")
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
