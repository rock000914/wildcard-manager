from __future__ import annotations

import base64
import io
import json
import random
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from .models import AppSettings, WildcardEntry
from .repository import ensure_thumbnail_destination


class ThumbnailApiClient:
    def test_connection(self, settings: AppSettings) -> None:
        """API が応答するか確認する。

        L12 修正: 以前は ``raise_for_status()`` だけだったため、プロキシの
        HTML 応答（200 OK だが中身は認証ページ等）でも成功扱いになっていた。
        Content-Type が JSON であることを追加検証する。
        """
        response = requests.get(
            f"{settings.api_base_url.rstrip('/')}/sdapi/v1/options",
            timeout=settings.api_timeout_sec,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            raise ValueError(
                f"API 応答が JSON ではありません (Content-Type: {content_type or 'unknown'})。"
                " API ベースURL が sd-webui のものか確認してください。"
            )
        # 念のため JSON パースできるか検証
        try:
            data = response.json()
        except Exception as exc:
            raise ValueError(f"API 応答を JSON としてパースできません: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("API 応答が JSON object ではありません。")

    def check_progress(self, settings: AppSettings) -> dict[str, Any]:
        response = requests.get(
            f"{settings.api_base_url.rstrip('/')}/sdapi/v1/progress",
            timeout=min(settings.api_timeout_sec, 5),
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def is_generating(self, settings: AppSettings) -> bool:
        try:
            data = self.check_progress(settings)
            state = data.get("state", {})
            return bool(state.get("job_count", 0) > 0)
        except Exception:
            return False

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

    def request_txt2img(self, settings: AppSettings, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(
            f"{settings.api_base_url.rstrip('/')}/sdapi/v1/txt2img",
            json=payload,
            timeout=settings.api_timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("API response is not a JSON object.")
        return data

    def generate_thumbnail(self, settings: AppSettings, entry: WildcardEntry) -> Path:
        body = self._pick_prompt(entry.content)

        if settings.thumbnail_random_wildcard and settings.thumbnail_random_wildcard_root:
            random_prompt = self._pick_random_wildcard(settings.thumbnail_random_wildcard_root, entry.abs_path)
            if random_prompt:
                body = f"{random_prompt}, {body}"

        prompt = self._compose_prompt(settings.generation_prompt_prefix, body)
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

        extra_payload = json.loads(settings.generation_extra_payload_json or "{}")
        if not isinstance(extra_payload, dict):
            raise ValueError("追加 API payload は JSON object で入力してください。")
        payload.update(extra_payload)

        if settings.generation_adetailer_enabled:
            payload.setdefault("alwayson_scripts", {})
            payload["alwayson_scripts"]["ADetailer"] = {
                "args": [{"ad_model": "face_yolov8n.pt"}]
            }

        data = self.request_txt2img(settings, payload)
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

    @staticmethod
    def _pick_random_wildcard(folder: str, exclude_path: str) -> str:
        """指定フォルダからランダムに wildcard を1つ選び、先頭プロンプト行を返す。

        M6 修正: 以前は ``encoding="utf-8"`` 固定で cp932 ファイルで落ちた。
        Repository 側のフォールバックリーダと同じ挙動にするため、
        3エンコーディングを順に試す。
        M7 修正: 除外パスの比較を resolve() 済みで行う。
        【WinError 6 対策】resolve() は OSError を投げうるので try/except で保護。
        """
        root = Path(folder)
        if not root.is_dir():
            return ""
        try:
            try:
                exclude_resolved = Path(exclude_path).resolve(strict=False)
            except (OSError, ValueError):
                exclude_resolved = Path(exclude_path).absolute()
        except Exception:
            exclude_resolved = Path(exclude_path)
        candidates: list[Path] = []
        for p in root.glob("*.txt"):
            try:
                try:
                    p_resolved = p.resolve(strict=False)
                except (OSError, ValueError):
                    p_resolved = p.absolute()
                if p_resolved != exclude_resolved:
                    candidates.append(p)
            except Exception:
                if str(p) != exclude_path:
                    candidates.append(p)
        if not candidates:
            return ""
        chosen = random.choice(candidates)
        # M6 修正: repository._read_text_file と同じエンコーディングフォールバック
        content = ""
        for encoding in ("utf-8", "utf-8-sig", "cp932"):
            try:
                content = chosen.read_text(encoding=encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            content = chosen.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
        return ""
