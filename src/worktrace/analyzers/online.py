from __future__ import annotations

import json
import logging
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Callable

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, OpenAIError
from openai import AuthenticationError, BadRequestError, PermissionDeniedError, RateLimitError

from ..config import OnlineLLMSettings, RuntimeConfig, load_online_llm_settings
from ..errors import AnalyzerProtocolError, RetryableAnalyzerProtocolError
from ..logging_utils import log_timing
from ..llm_usage import LLMUsageRecorder, extract_usage
from ..models import (
    AnalysisBatch,
    AnchorUnit,
    AttachmentTextBlock,
    BatchAnalysisResult,
    BatchAnchorAnalysisResult,
    BatchSegmentAnalysisResult,
    CollectedGroupingGroup,
    CollectedGroupingResult,
    CollectedMergeResult,
    CollectedSourceEvent,
    ConversationSegmentationResult,
    CrossConversationGroupResult,
    PersonalFactReviewBatch,
    PersonalFactReviewResult,
    SourceBackedEventDraft,
    SegmentAnalysisBatch,
    NormalizedMessage,
    ResponseSignal,
    RetentionReviewBatch,
    RetentionReviewResult,
)
from ..utils.json_io import parse_json_value_from_text
from ..utils.token_estimation import estimate_text_tokens
from .base import Analyzer
from .output_schemas import (
    anchor_batch_output_schema,
    batch_output_schema,
    collected_grouping_output_schema,
    collected_merge_output_schema,
    conversation_segmentation_output_schema,
    merge_output_schema,
    personal_fact_review_output_schema,
    retention_review_output_schema,
    segment_batch_output_schema,
)
from .prompts import (
    build_anchor_batch_analysis_prompt,
    build_batch_analysis_prompt,
    build_collected_grouping_prompt,
    build_collected_merge_prompt,
    build_collected_review_prompt,
    build_collected_render_prompt,
    build_conversation_segmentation_prompt,
    build_merge_prompt,
    build_personal_fact_review_prompt,
    build_retention_review_prompt,
    build_segment_batch_analysis_prompt,
    restore_conversation_segmentation_references,
)
from .protocol import (
    parse_anchor_batch_analysis_payload,
    parse_batch_analysis_payload,
    parse_collected_grouping_payload,
    parse_collected_merge_payload,
    parse_conversation_segmentation_payload,
    parse_merge_payload,
    parse_personal_fact_review_payload,
    parse_retention_review_payload,
    parse_segment_batch_analysis_payload,
)

logger = logging.getLogger("worktrace")

_GLOBAL_CLIENT_FINGERPRINT: tuple[object, ...] | None = None
_GLOBAL_OPENAI_CLIENT: OpenAI | None = None
_GLOBAL_HTTPX_CLIENT: httpx.Client | None = None
_GLOBAL_CLIENT_LOCK = threading.Lock()


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


def _has_usage(payload: dict[str, object]) -> bool:
    if isinstance(payload.get("usage"), dict):
        return True
    response = payload.get("response")
    return isinstance(response, dict) and isinstance(response.get("usage"), dict)


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

    read_timeout = settings.timeout_seconds
    if settings.stream_enabled:
        read_timeout = min(
            settings.timeout_seconds,
            settings.stream_first_response_timeout_seconds,
        )
    return httpx.Client(
        verify=ssl_context if settings.tls_verify else False,
        timeout=httpx.Timeout(
            connect=min(5.0, settings.timeout_seconds),
            read=read_timeout,
            write=settings.timeout_seconds,
            pool=settings.timeout_seconds,
        ),
    )

def _build_client_fingerprint(settings: OnlineLLMSettings) -> tuple[object, ...]:
    return (
        settings.base_url,
        settings.api_key,
        settings.model,
        settings.timeout_seconds,
        settings.stream_first_response_timeout_seconds,
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

    with _GLOBAL_CLIENT_LOCK:
        return _get_or_create_global_client_locked(settings)


def _get_or_create_global_client_locked(settings: OnlineLLMSettings) -> OpenAI:
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
    usage_recorder: LLMUsageRecorder | None = None

    def __post_init__(self) -> None:
        if self.cwd is None:
            self.cwd = Path.cwd()
        if self.usage_recorder is None:
            self.usage_recorder = LLMUsageRecorder()

    def analyze_batch(
        self,
        target_date: str,
        batch_input: AnalysisBatch,
    ) -> BatchAnalysisResult:
        payload = self._invoke_online(
            self.build_batch_prompt(batch_input),
            output_schema=batch_output_schema(self.config),
            request_kind="batch_analysis",
        )
        return parse_batch_analysis_payload(payload)

    def request_json(
        self,
        prompt: str,
        *,
        output_schema: dict[str, object] | None = None,
    ) -> object:
        """Run an explicit JSON request for auxiliary, non-daily workflows."""
        return self._invoke_online(
            prompt,
            output_schema=output_schema,
            request_kind="auxiliary_json",
        )

    def build_segmentation_prompt(
        self,
        *,
        target_date: str,
        conversation_id: str,
        conversation_name: str,
        messages: list[NormalizedMessage],
        self_open_id: str,
        self_display_name: str,
        response_signals: list[ResponseSignal],
        hard_boundary_before_ids: set[str],
        attachment_texts: list[AttachmentTextBlock] | None = None,
    ) -> str:
        return build_conversation_segmentation_prompt(
            target_date=target_date,
            conversation_id=conversation_id,
            conversation_name=conversation_name,
            messages=messages,
            self_open_id=self_open_id,
            self_display_name=self_display_name,
            response_signals=response_signals,
            hard_boundary_before_ids=hard_boundary_before_ids,
            attachment_texts=attachment_texts,
            config=self.config,
        )

    def segment_conversation(
        self,
        *,
        target_date: str,
        conversation_id: str,
        conversation_name: str,
        messages: list[NormalizedMessage],
        self_open_id: str,
        self_display_name: str,
        response_signals: list[ResponseSignal],
        hard_boundary_before_ids: set[str],
        attachment_texts: list[AttachmentTextBlock] | None = None,
    ) -> ConversationSegmentationResult:
        payload = self._invoke_online(
            self.build_segmentation_prompt(
                target_date=target_date,
                conversation_id=conversation_id,
                conversation_name=conversation_name,
                messages=messages,
                self_open_id=self_open_id,
                self_display_name=self_display_name,
                response_signals=response_signals,
                hard_boundary_before_ids=hard_boundary_before_ids,
                attachment_texts=attachment_texts,
            ),
            output_schema=conversation_segmentation_output_schema(),
            request_kind="conversation_segmentation",
        )
        return restore_conversation_segmentation_references(
            parse_conversation_segmentation_payload(payload),
            messages=messages,
            response_signals=response_signals,
        )

    def build_segment_batch_prompt(self, batch: SegmentAnalysisBatch) -> str:
        return build_segment_batch_analysis_prompt(batch, config=self.config)

    def analyze_segment_batch(
        self,
        batch: SegmentAnalysisBatch,
    ) -> BatchSegmentAnalysisResult:
        payload = self._invoke_online(
            self.build_segment_batch_prompt(batch),
            output_schema=segment_batch_output_schema(self.config),
            request_kind="segment_batch_analysis",
        )
        return parse_segment_batch_analysis_payload(payload)

    def build_retention_review_prompt(self, batch: RetentionReviewBatch) -> str:
        return build_retention_review_prompt(batch, config=self.config)

    def review_retention_candidates(
        self,
        batch: RetentionReviewBatch,
    ) -> RetentionReviewResult:
        payload = self._invoke_online(
            self.build_retention_review_prompt(batch),
            output_schema=retention_review_output_schema(self.config),
            request_kind="retention_review",
        )
        try:
            return parse_retention_review_payload(payload)
        except AnalyzerProtocolError as exc:
            raise RetryableAnalyzerProtocolError(str(exc)) from exc

    def build_personal_fact_review_prompt(
        self,
        batch: PersonalFactReviewBatch,
    ) -> str:
        return build_personal_fact_review_prompt(batch, config=self.config)

    def review_personal_event_facts(
        self,
        batch: PersonalFactReviewBatch,
    ) -> PersonalFactReviewResult:
        payload = self._invoke_online(
            self.build_personal_fact_review_prompt(batch),
            output_schema=personal_fact_review_output_schema(batch),
            request_kind="personal_fact_review",
        )
        try:
            return parse_personal_fact_review_payload(payload)
        except AnalyzerProtocolError as exc:
            raise RetryableAnalyzerProtocolError(str(exc)) from exc

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
            output_schema=anchor_batch_output_schema(self.config),
            request_kind="anchor_batch_analysis",
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
            request_kind="day_candidate_merge",
        )
        self.last_merge_payload = payload
        return parse_merge_payload(payload)

    def merge_collected_events(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> CollectedMergeResult:
        payload = self._invoke_online(
            build_collected_render_prompt(
                target_date,
                events,
                deterministic_groups,
                config=self.config,
            ),
            output_schema=collected_merge_output_schema(),
            request_kind="collected_event_merge",
        )
        self.last_collected_merge_payload = payload
        try:
            return parse_collected_merge_payload(payload)
        except AnalyzerProtocolError as exc:
            raise RetryableAnalyzerProtocolError(str(exc)) from exc

    def group_collected_events(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> CollectedGroupingResult:
        payload = self._invoke_online(
            build_collected_grouping_prompt(
                target_date,
                events,
                deterministic_groups,
                config=self.config,
            ),
            output_schema=collected_grouping_output_schema(),
            request_kind="collected_candidate_grouping",
        )
        try:
            return parse_collected_grouping_payload(payload)
        except AnalyzerProtocolError as exc:
            raise RetryableAnalyzerProtocolError(str(exc)) from exc

    def review_collected_group(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        candidate_group: CollectedGroupingGroup,
        *,
        review_reasons: list[str] | None = None,
    ) -> CollectedGroupingResult:
        payload = self._invoke_online(
            build_collected_review_prompt(
                target_date,
                events,
                candidate_group,
                config=self.config,
                review_reasons=review_reasons,
            ),
            output_schema=collected_grouping_output_schema(),
            request_kind="collected_group_review",
        )
        try:
            return parse_collected_grouping_payload(payload)
        except AnalyzerProtocolError as exc:
            raise RetryableAnalyzerProtocolError(str(exc)) from exc

    def _invoke_online(
        self,
        prompt: str,
        *,
        output_schema: dict[str, object] | None = None,
        request_kind: str,
    ) -> object:
        prepared_prompt = _apply_soft_no_think(prompt)
        estimated_tokens = estimate_text_tokens(prepared_prompt)
        if estimated_tokens > self.config.max_model_input_tokens:
            raise AnalyzerProtocolError(
                "Model input exceeds max_model_input_tokens before online request: "
                f"estimated_tokens={estimated_tokens} "
                f"limit={self.config.max_model_input_tokens} "
                f"request_kind={request_kind}"
            )
        started_at = perf_counter()
        settings = self.settings_loader(self.config, cwd=self.cwd, environ=None)
        body = _build_responses_request_body(prompt, settings=settings, schema=output_schema)

        try:
            text, usage_payload = self._invoke_via_sdk(settings, body)
        except AuthenticationError as exc:
            raise AnalyzerProtocolError("HTTP 401: invalid API key or authentication failed.") from exc
        except PermissionDeniedError as exc:
            raise AnalyzerProtocolError("HTTP 403: permission denied.") from exc
        except RateLimitError as exc:
            raise RetryableAnalyzerProtocolError("HTTP 429: rate limited.") from exc
        except BadRequestError as exc:
            raise AnalyzerProtocolError(str(exc)) from exc
        except APIStatusError as exc:
            error_type = (
                RetryableAnalyzerProtocolError
                if exc.status_code == 408 or exc.status_code >= 500
                else AnalyzerProtocolError
            )
            raise error_type(f"HTTP {exc.status_code}: {exc.message}") from exc
        except APITimeoutError as exc:
            raise RetryableAnalyzerProtocolError("Request timed out.") from exc
        except APIConnectionError as exc:
            reason = str(exc)
            if "certificate verify failed" in reason.lower():
                raise AnalyzerProtocolError(f"TLS certificate verification failed: {reason}") from exc
            raise RetryableAnalyzerProtocolError(f"Network error: {reason}") from exc
        except json.JSONDecodeError as exc:
            raise RetryableAnalyzerProtocolError(
                "Online LLM stream contained invalid JSON data."
            ) from exc
        except OpenAIError as exc:
            raise AnalyzerProtocolError(str(exc)) from exc

        usage = extract_usage(usage_payload)
        duration_ms = log_timing(
            logger,
            "online_llm.request.completed",
            started_at,
            request_kind=request_kind,
            prompt_chars=len(prompt),
            stream_enabled=settings.stream_enabled,
            tls_verify=settings.tls_verify,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            total_tokens=usage["total_tokens"],
        )
        self.usage_recorder.record(
            request_kind,
            usage_payload,
            duration_ms=duration_ms,
            prompt_chars=len(prompt),
            backend="online",
        )
        if not text.strip():
            raise RetryableAnalyzerProtocolError(
                "Online LLM response did not contain text output."
            )
        try:
            return parse_json_value_from_text(text)
        except ValueError as exc:
            raise RetryableAnalyzerProtocolError(
                "Online LLM response did not contain valid JSON output."
            ) from exc

    def _invoke_via_sdk(
        self,
        settings: OnlineLLMSettings,
        body: dict[str, object],
    ) -> tuple[str, dict[str, object]]:
        client = _get_or_create_global_client(settings)
        if settings.stream_enabled:
            response_stream = client.responses.create(**body)
            chunks: list[str] = []
            usage_payload: dict[str, object] = {}
            for event in response_stream:
                event_payload = event.model_dump()
                chunk_text = _extract_text_from_responses_stream_event(event_payload)
                if chunk_text:
                    chunks.append(chunk_text)
                if _has_usage(event_payload):
                    usage_payload = event_payload
            return "".join(chunks), usage_payload
        response = client.responses.create(**body)
        payload = response.model_dump()
        return _extract_text_from_responses_payload(payload), payload
