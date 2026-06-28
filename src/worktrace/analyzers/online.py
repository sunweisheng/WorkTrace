from __future__ import annotations

import json
import logging
import random
import ssl
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter, sleep
from typing import Callable

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, OpenAIError
from openai import AuthenticationError, BadRequestError, PermissionDeniedError, RateLimitError

from ..config import OnlineLLMSettings, RuntimeConfig, load_online_llm_settings
from ..errors import AnalyzerProtocolError
from ..logging_utils import log_timing
from ..models import (
    AnalysisBatch,
    AnchorUnit,
    BatchAnalysisResult,
    BatchAnchorAnalysisResult,
    CrossConversationGroupResult,
    SourceBackedEventDraft,
)
from ..utils.json_io import parse_json_value_from_text
from .base import Analyzer
from .output_schemas import anchor_batch_output_schema, batch_output_schema, merge_output_schema
from .prompts import (
    build_anchor_batch_analysis_prompt,
    build_batch_analysis_prompt,
    build_merge_prompt,
)
from .protocol import (
    parse_anchor_batch_analysis_payload,
    parse_batch_analysis_payload,
    parse_merge_payload,
)

logger = logging.getLogger("worktrace")

_GLOBAL_CLIENT_FINGERPRINT: tuple[object, ...] | None = None
_GLOBAL_OPENAI_CLIENT: OpenAI | None = None
_GLOBAL_HTTPX_CLIENT: httpx.Client | None = None


def _apply_soft_no_think(prompt: str) -> str:
    stripped = prompt.rstrip()
    if stripped.endswith("/no_think"):
        return stripped
    return f"{stripped}\n/no_think"


def _extract_text_from_chat_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""

    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def _responses_output_to_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"output_text", "text"}:
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)
    if isinstance(value, dict):
        for key in ("text", "content", "output_text"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
            if isinstance(candidate, (dict, list)):
                nested = _responses_output_to_text(candidate)
                if nested:
                    return nested
    return ""


def _extract_text_from_responses_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            text = _responses_output_to_text(content)
            if text.strip():
                return text
    return ""


def _extract_text_from_chat_stream_event(event: object) -> str:
    if not isinstance(event, dict):
        return ""
    choices = event.get("choices")
    if not isinstance(choices, list):
        return ""

    chunks: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            chunks.append(content)
    return "".join(chunks)


def _extract_text_from_responses_stream_event(event: object) -> str:
    if not isinstance(event, dict):
        return ""
    event_type = event.get("type")
    if event_type in {"response.output_text.delta", "response.output_text"}:
        delta = event.get("delta")
        if isinstance(delta, str):
            return delta
        text = event.get("text")
        if isinstance(text, str):
            return text
    return ""


def _build_responses_request_body(
    prompt: str,
    *,
    settings: OnlineLLMSettings,
    schema: dict[str, object] | None,
) -> dict[str, object]:
    prompt = _apply_soft_no_think(prompt)
    body: dict[str, object] = {
        "model": settings.model,
        "input": prompt,
        "stream": settings.stream_enabled,
        "stream_options": {"include_usage": True},
    }
    if settings.reasoning_effort == "none":
        body["reasoning"] = {"effort": "none"}
    if schema is not None:
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": "worktrace_output",
                "schema": schema,
                "strict": True,
            }
        }
    return body


def _build_http_client(settings: OnlineLLMSettings) -> httpx.Client:
    ssl_context = ssl.create_default_context()
    if not settings.tls_verify:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    return httpx.Client(
        verify=ssl_context if settings.tls_verify else False,
        timeout=httpx.Timeout(settings.timeout_seconds, connect=min(5.0, settings.timeout_seconds)),
    )

def _build_client_fingerprint(settings: OnlineLLMSettings) -> tuple[object, ...]:
    return (
        settings.base_url,
        settings.api_key,
        settings.model,
        settings.timeout_seconds,
        settings.tls_verify,
        settings.stream_enabled,
        settings.reasoning_effort,
    )


def _close_global_client() -> None:
    global _GLOBAL_OPENAI_CLIENT, _GLOBAL_HTTPX_CLIENT
    if _GLOBAL_OPENAI_CLIENT is not None and hasattr(_GLOBAL_OPENAI_CLIENT, "close"):
        _GLOBAL_OPENAI_CLIENT.close()
    elif _GLOBAL_HTTPX_CLIENT is not None and hasattr(_GLOBAL_HTTPX_CLIENT, "close"):
        _GLOBAL_HTTPX_CLIENT.close()
    _GLOBAL_OPENAI_CLIENT = None
    _GLOBAL_HTTPX_CLIENT = None


def _get_or_create_global_client(settings: OnlineLLMSettings) -> OpenAI:
    global _GLOBAL_CLIENT_FINGERPRINT, _GLOBAL_OPENAI_CLIENT, _GLOBAL_HTTPX_CLIENT

    fingerprint = _build_client_fingerprint(settings)
    if _GLOBAL_OPENAI_CLIENT is not None and _GLOBAL_CLIENT_FINGERPRINT == fingerprint:
        logger.info(
            "online_llm.client_singleton state=%s base_url=%s model=%s stream_enabled=%s tls_verify=%s reasoning_effort=%s",
            "reused",
            settings.base_url,
            settings.model,
            settings.stream_enabled,
            settings.tls_verify,
            settings.reasoning_effort,
        )
        return _GLOBAL_OPENAI_CLIENT

    if _GLOBAL_OPENAI_CLIENT is not None:
        _close_global_client()

    http_client = _build_http_client(settings)
    client = OpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url.strip(),
        http_client=http_client,
        max_retries=0,
    )
    _GLOBAL_HTTPX_CLIENT = http_client
    _GLOBAL_OPENAI_CLIENT = client
    _GLOBAL_CLIENT_FINGERPRINT = fingerprint
    logger.info(
        "online_llm.client_singleton state=%s base_url=%s model=%s stream_enabled=%s tls_verify=%s reasoning_effort=%s",
        "created",
        settings.base_url,
        settings.model,
        settings.stream_enabled,
        settings.tls_verify,
        settings.reasoning_effort,
    )
    return client


@dataclass
class OnlineLLMAnalyzer(Analyzer):
    config: RuntimeConfig
    cwd: Path | None = None
    settings_loader: Callable[..., OnlineLLMSettings] = load_online_llm_settings
    random_uniform: Callable[[float, float], float] = random.uniform
    sleep_func: Callable[[float], None] = sleep

    def __post_init__(self) -> None:
        if self.cwd is None:
            self.cwd = Path.cwd()
        self._request_count = 0

    def analyze_batch(
        self,
        target_date: str,
        batch_input: AnalysisBatch,
    ) -> BatchAnalysisResult:
        payload = self._invoke_online(
            self.build_batch_prompt(batch_input),
            output_schema=batch_output_schema(),
        )
        return parse_batch_analysis_payload(payload)

    def build_batch_prompt(self, batch_input: AnalysisBatch) -> str:
        return build_batch_analysis_prompt(batch_input, config=self.config)

    def build_merge_prompt(
        self,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
    ) -> str:
        return build_merge_prompt(target_date, candidates)

    def analyze_anchor_batch(
        self,
        target_date: str,
        anchor_units: list[AnchorUnit],
    ) -> BatchAnchorAnalysisResult:
        payload = self._invoke_online(
            build_anchor_batch_analysis_prompt(target_date, anchor_units, config=self.config),
            output_schema=anchor_batch_output_schema(),
        )
        return parse_anchor_batch_analysis_payload(payload)

    def merge_day_candidates(
        self,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
    ) -> CrossConversationGroupResult:
        payload = self._invoke_online(
            build_merge_prompt(target_date, candidates),
            output_schema=merge_output_schema(),
        )
        self.last_merge_payload = payload
        return parse_merge_payload(payload)

    def _invoke_online(
        self,
        prompt: str,
        *,
        output_schema: dict[str, object] | None = None,
    ) -> object:
        started_at = perf_counter()
        settings = self.settings_loader(self.config, cwd=self.cwd, environ=None)
        self._maybe_sleep_between_requests(settings)
        body = _build_responses_request_body(prompt, settings=settings, schema=output_schema)

        try:
            text = self._invoke_via_sdk(settings, body)
        except AuthenticationError as exc:
            raise AnalyzerProtocolError("HTTP 401: invalid API key or authentication failed.") from exc
        except PermissionDeniedError as exc:
            raise AnalyzerProtocolError("HTTP 403: permission denied.") from exc
        except RateLimitError as exc:
            raise AnalyzerProtocolError("HTTP 429: rate limited.") from exc
        except BadRequestError as exc:
            raise AnalyzerProtocolError(str(exc)) from exc
        except APIStatusError as exc:
            raise AnalyzerProtocolError(f"HTTP {exc.status_code}: {exc.message}") from exc
        except APITimeoutError as exc:
            raise AnalyzerProtocolError("Request timed out.") from exc
        except APIConnectionError as exc:
            reason = str(exc)
            if "certificate verify failed" in reason.lower():
                raise AnalyzerProtocolError(f"TLS certificate verification failed: {reason}") from exc
            raise AnalyzerProtocolError(f"Network error: {reason}") from exc
        except OpenAIError as exc:
            raise AnalyzerProtocolError(str(exc)) from exc

        self._request_count += 1
        log_timing(
            logger,
            "online_llm.request.completed",
            started_at,
            prompt_chars=len(prompt),
            stream_enabled=settings.stream_enabled,
            tls_verify=settings.tls_verify,
        )
        if not text.strip():
            raise AnalyzerProtocolError("Online LLM response did not contain text output.")
        try:
            return parse_json_value_from_text(text)
        except ValueError as exc:
            raise AnalyzerProtocolError("Online LLM response did not contain valid JSON output.") from exc

    def _invoke_via_sdk(self, settings: OnlineLLMSettings, body: dict[str, object]) -> str:
        client = _get_or_create_global_client(settings)
        if settings.stream_enabled:
            response_stream = client.responses.create(**body)
            chunks: list[str] = []
            for event in response_stream:
                chunk_text = _extract_text_from_responses_stream_event(event.model_dump())
                if chunk_text:
                    chunks.append(chunk_text)
            return "".join(chunks)
        response = client.responses.create(**body)
        return _extract_text_from_responses_payload(response.model_dump())

    def _maybe_sleep_between_requests(self, settings: OnlineLLMSettings) -> None:
        if self._request_count == 0:
            return
        delay_seconds = self.random_uniform(
            settings.sleep_min_seconds,
            settings.sleep_max_seconds,
        )
        sleep_started_at = perf_counter()
        self.sleep_func(delay_seconds)
        log_timing(
            logger,
            "online_llm.request.delay",
            sleep_started_at,
            delay_seconds=round(delay_seconds, 3),
        )
