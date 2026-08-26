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
from .errors import AnalyzerProtocolError, RetryableAnalyzerProtocolError
from .logging_utils import log_timing
from .llm_usage import LLMUsageRecorder, extract_usage
from .analyzers.online import (
    _build_http_client,
    _extract_text_from_responses_stream_event,
    _has_usage,
)

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
        cwd: Path | None = None,
    ) -> None:
        self.config = config
        self.settings = settings or ImageSummarySettings.load(config)
        self._client = client
        self.cwd = cwd or Path.cwd()
        self._count = 0
        self.usage_recorder = usage_recorder or LLMUsageRecorder()

    def summarize(self, image_path: Path, *, required: bool = False) -> str:
        if not self.settings.enabled or (
            not required and self._count >= self.settings.max_images_per_run
        ):
            return ""
        if not image_path.is_file() or image_path.stat().st_size > self.settings.max_image_bytes:
            return ""

        online = load_online_llm_settings(self.config, cwd=self.cwd)
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
            "stream": online.stream_enabled,
        }
        if online.reasoning_effort == "none":
            body["reasoning"] = {"effort": "none"}
        http_client = None
        client = self._client
        if client is None:
            http_client = _build_http_client(online)
            client = OpenAI(
                base_url=online.base_url,
                api_key=online.api_key,
                http_client=http_client,
                max_retries=0,
            )
        started_at = perf_counter()
        try:
            response = client.responses.create(**body)
            if online.stream_enabled:
                chunks: list[str] = []
                payload: dict[str, object] = {}
                try:
                    for event in response:
                        event_payload = event.model_dump()
                        chunks.append(_extract_text_from_responses_stream_event(event_payload))
                        if _has_usage(event_payload):
                            payload = event_payload
                finally:
                    close_stream = getattr(response, "close", None)
                    if callable(close_stream):
                        close_stream()
                text = "".join(chunks).strip()
            else:
                payload = response.model_dump() if hasattr(response, "model_dump") else {}
                text = str(getattr(response, "output_text", "")).strip()
        except Exception as exc:
            log_timing(
                logger,
                "online_llm.request.failed",
                started_at,
                request_kind="image_summary",
                required=required,
                prompt_chars=len(self.settings.prompt),
                image_bytes=image_path.stat().st_size,
                stream_enabled=online.stream_enabled,
            )
            raise AnalyzerProtocolError(f"Image summary request failed: {exc}") from exc
        finally:
            if self._client is None:
                close_client = getattr(client, "close", None)
                if callable(close_client):
                    close_client()
                elif http_client is not None:
                    http_client.close()
        usage = extract_usage(payload)
        duration_ms = log_timing(
            logger,
            "online_llm.request.completed",
            started_at,
            request_kind="image_summary",
            required=required,
            prompt_chars=len(self.settings.prompt),
            image_bytes=image_path.stat().st_size,
            stream_enabled=online.stream_enabled,
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
        if not text:
            raise AnalyzerProtocolError("Image summary response did not contain text output.")
        return text


@dataclass
class CodexFirstImageSummarizer:
    config: RuntimeConfig
    settings: ImageSummarySettings
    codex: object
    online_fallback: OnlineImageSummarizer | None = None

    def __post_init__(self) -> None:
        self._count = 0

    def summarize(self, image_path: Path, *, required: bool = False) -> str:
        if not self.settings.enabled or (
            not required and self._count >= self.settings.max_images_per_run
        ):
            return ""
        if (
            not image_path.is_file()
            or image_path.stat().st_size > self.settings.max_image_bytes
        ):
            return ""

        request_text = getattr(self.codex, "request_text")
        last_error: Exception | None = None
        for attempt_index in range(self.config.primary_request_retry_limit + 1):
            try:
                text = str(
                    request_text(self.settings.prompt, image_path=image_path)
                ).strip()
                if not text:
                    raise RetryableAnalyzerProtocolError(
                        "Codex image summary response is empty."
                    )
                if not required:
                    self._count += 1
                return text
            except RetryableAnalyzerProtocolError as exc:
                last_error = exc
                if attempt_index < self.config.primary_request_retry_limit:
                    continue
                break

        if self.online_fallback is None:
            assert last_error is not None
            raise last_error
        text = self.online_fallback.summarize(image_path, required=True)
        if not text:
            raise AnalyzerProtocolError("Online image fallback returned empty output.")
        if not required:
            self._count += 1
        return text
