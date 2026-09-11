from __future__ import annotations

import base64
import json
import logging
import mimetypes
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from openai import OpenAI

from .config import RuntimeConfig, load_online_llm_settings
from .errors import (
    AnalyzerProtocolError,
    CodexProtocolViolationError,
    RetryableAnalyzerProtocolError,
)
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
    unrecognized_response_markers: tuple[str, ...] = ()

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
        unrecognized_markers = payload.get("unrecognized_response_markers", [])
        if not isinstance(enabled, bool) or not isinstance(prompt, str):
            raise ValueError(f"Invalid image summary config: {path} has invalid fields.")
        if not isinstance(max_images, int) or max_images < 0:
            raise ValueError(f"Invalid image summary config: max_images_per_run must be non-negative.")
        if not isinstance(max_bytes, int) or max_bytes < 0:
            raise ValueError(f"Invalid image summary config: max_image_bytes must be non-negative.")
        if not isinstance(unrecognized_markers, list) or any(
            not isinstance(marker, str) or not marker.strip()
            for marker in unrecognized_markers
        ):
            raise ValueError(
                "Invalid image summary config: unrecognized_response_markers "
                "must contain non-empty strings."
            )
        return cls(
            enabled,
            prompt.strip(),
            max_images,
            max_bytes,
            tuple(marker.strip() for marker in unrecognized_markers),
        )


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
            duration_ms = log_timing(
                logger,
                "online_llm.request.failed",
                started_at,
                request_kind="image_summary",
                required=required,
                prompt_chars=len(self.settings.prompt),
                image_bytes=image_path.stat().st_size,
                stream_enabled=online.stream_enabled,
            )
            self.usage_recorder.record(
                "image_summary",
                {},
                duration_ms=duration_ms,
                prompt_chars=len(self.settings.prompt),
                backend="online",
                status="failed",
                error_category="request_failed",
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
        if not text:
            self.usage_recorder.record(
                "image_summary",
                payload,
                duration_ms=duration_ms,
                prompt_chars=len(self.settings.prompt),
                backend="online",
                status="failed",
                error_category="empty_response",
            )
            raise AnalyzerProtocolError("Image summary response did not contain text output.")
        self.usage_recorder.record(
            "image_summary",
            payload,
            duration_ms=duration_ms,
            prompt_chars=len(self.settings.prompt),
            backend="online",
        )
        if not required:
            self._count += 1
        return text


@dataclass
class CodexFirstImageSummarizer:
    config: RuntimeConfig
    settings: ImageSummarySettings
    codex: object
    online_fallback: OnlineImageSummarizer | None = None
    usage_recorder: LLMUsageRecorder | None = None

    def __post_init__(self) -> None:
        self._count = 0
        if self.usage_recorder is None:
            recorder = getattr(self.codex, "usage_recorder", None)
            if isinstance(recorder, LLMUsageRecorder):
                self.usage_recorder = recorder

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

        request_context = (
            self.usage_recorder.request_context(f"image-summary-{uuid4().hex}")
            if self.usage_recorder is not None
            else nullcontext()
        )
        with request_context:
            return self._summarize_current_image(image_path, required=required)

    def _summarize_current_image(self, image_path: Path, *, required: bool) -> str:
        request_text = getattr(self.codex, "request_text")
        last_error: Exception | None = None
        fallback_error_category: str | None = None
        for attempt_index in range(self.config.primary_request_retry_limit + 1):
            try:
                text = str(
                    request_text(self.settings.prompt, image_path=image_path)
                ).strip()
                if not text:
                    raise RetryableAnalyzerProtocolError(
                        "Codex image summary response is empty."
                    )
                if self._is_unrecognized_response(text):
                    last_error = AnalyzerProtocolError(
                        "Codex image summary reported that the image was unavailable."
                    )
                    fallback_error_category = "image_unrecognized"
                    self._mark_current_validation(valid=False, error=last_error)
                    break
                self._mark_current_validation(valid=True)
                if not required:
                    self._count += 1
                return text
            except RetryableAnalyzerProtocolError as exc:
                last_error = exc
                self._mark_current_validation(valid=False, error=exc)
                if attempt_index < self.config.primary_request_retry_limit:
                    continue
                break
            except CodexProtocolViolationError as exc:
                last_error = exc
                fallback_error_category = "protocol_violation"
                self._mark_current_validation(valid=False, error=exc)
                break

        if self.online_fallback is None:
            assert last_error is not None
            raise last_error
        if self.usage_recorder is not None:
            self.usage_recorder.mark_request_fallback(
                self.usage_recorder.current_request_context_id(),
                fallback_from="codex",
                fallback_to="online",
                error_category=fallback_error_category,
            )
        try:
            text = self.online_fallback.summarize(image_path, required=True)
        except Exception as exc:
            self._mark_current_validation(valid=False, error=exc)
            raise
        if not text:
            raise AnalyzerProtocolError("Online image fallback returned empty output.")
        self._mark_current_validation(valid=True)
        if not required:
            self._count += 1
        return text

    def _is_unrecognized_response(self, text: str) -> bool:
        normalized = " ".join(text.casefold().split())
        return any(
            " ".join(marker.casefold().split()) in normalized
            for marker in self.settings.unrecognized_response_markers
        )

    def _mark_current_validation(
        self,
        *,
        valid: bool,
        error: Exception | None = None,
    ) -> None:
        if self.usage_recorder is None:
            return
        self.usage_recorder.mark_request_validation(
            self.usage_recorder.current_request_context_id(),
            valid=valid,
            errors=() if error is None else (str(error),),
        )
