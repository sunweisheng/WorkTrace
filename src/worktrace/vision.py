from __future__ import annotations

import base64
import json
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from openai import OpenAI

from .config import RuntimeConfig, load_online_llm_settings
from .errors import AnalyzerProtocolError
from .logging_utils import log_timing
from .llm_usage import LLMUsageRecorder, extract_usage

logger = logging.getLogger("worktrace")


@dataclass(frozen=True)
class ImageSummarySettings:
    enabled: bool
    prompt: str
    max_images_per_run: int
    max_image_bytes: int

    @classmethod
    def load(cls, config: RuntimeConfig, *, cwd: Path | None = None) -> "ImageSummarySettings":
        path = (cwd or Path.cwd()) / "config" / "image_summary.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(False, "", 0, 0)
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid image summary config: {path} must contain an object.")
        enabled = payload.get("enabled", False)
        prompt = payload.get("prompt", "")
        max_images = payload.get("max_images_per_run", 0)
        max_bytes = payload.get("max_image_bytes", 0)
        if not isinstance(enabled, bool) or not isinstance(prompt, str):
            raise ValueError(f"Invalid image summary config: {path} has invalid fields.")
        if not isinstance(max_images, int) or max_images < 0:
            raise ValueError(f"Invalid image summary config: max_images_per_run must be non-negative.")
        if not isinstance(max_bytes, int) or max_bytes < 0:
            raise ValueError(f"Invalid image summary config: max_image_bytes must be non-negative.")
        return cls(enabled, prompt.strip(), max_images, max_bytes)


class OnlineImageSummarizer:
    def __init__(
        self,
        *,
        config: RuntimeConfig,
        settings: ImageSummarySettings | None = None,
        client: OpenAI | None = None,
        usage_recorder: LLMUsageRecorder | None = None,
    ) -> None:
        self.config = config
        self.settings = settings or ImageSummarySettings.load(config)
        self._client = client
        self._count = 0
        self.usage_recorder = usage_recorder or LLMUsageRecorder()

    def summarize(self, image_path: Path, *, required: bool = False) -> str:
        if not self.settings.enabled or (
            not required and self._count >= self.settings.max_images_per_run
        ):
            return ""
        if not image_path.is_file() or image_path.stat().st_size > self.settings.max_image_bytes:
            return ""

        online = load_online_llm_settings(self.config)
        mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        content: list[dict[str, object]] = [
            {"type": "input_text", "text": f"{self.settings.prompt}\n/no_think"},
            {
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{encoded}",
                "detail": "low",
            },
        ]
        body: dict[str, object] = {
            "model": online.model,
            "input": [{"role": "user", "content": content}],
            "stream": False,
        }
        if online.reasoning_effort == "none":
            body["reasoning"] = {"effort": "none"}
        client = self._client or OpenAI(
            base_url=online.base_url,
            api_key=online.api_key,
            timeout=online.timeout_seconds,
        )
        started_at = perf_counter()
        try:
            response = client.responses.create(**body)
        except Exception as exc:
            log_timing(
                logger,
                "online_llm.request.failed",
                started_at,
                request_kind="image_summary",
                required=required,
                prompt_chars=len(self.settings.prompt),
                image_bytes=image_path.stat().st_size,
                stream_enabled=False,
            )
            raise AnalyzerProtocolError(f"Image summary request failed: {exc}") from exc
        payload = response.model_dump() if hasattr(response, "model_dump") else {}
        usage = extract_usage(payload)
        duration_ms = log_timing(
            logger,
            "online_llm.request.completed",
            started_at,
            request_kind="image_summary",
            required=required,
            prompt_chars=len(self.settings.prompt),
            image_bytes=image_path.stat().st_size,
            stream_enabled=False,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            total_tokens=usage["total_tokens"],
        )
        self.usage_recorder.record(
            "image_summary",
            payload,
            duration_ms=duration_ms,
            prompt_chars=len(self.settings.prompt),
        )
        if not required:
            self._count += 1
        text = str(getattr(response, "output_text", "")).strip()
        if not text:
            raise AnalyzerProtocolError("Image summary response did not contain text output.")
        return text
