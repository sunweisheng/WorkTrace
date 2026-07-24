from __future__ import annotations

import json
import logging
import ssl
import threading
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from queue import Empty, Queue
from time import perf_counter
from typing import Callable, Iterator

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, OpenAIError
from openai import AuthenticationError, BadRequestError, PermissionDeniedError, RateLimitError

from ..config import OnlineLLMSettings, RuntimeConfig, load_online_llm_settings
from ..errors import (
    AnalyzerProtocolError,
    ModelInputLimitError,
    ModelInputRejectedError,
    RetryableAnalyzerProtocolError,
)
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
from ..utils.token_estimation import estimate_structured_input_tokens, prepare_model_prompt
from .base import Analyzer, is_indivisible_collected_request, oversized_input_kwargs
from .function_calls import (
    FunctionCallSpec,
    collected_grouping_call_contract,
    function_call_spec,
    message_reference_ids,
    personal_grouping_call_contract,
    task_function_call_spec,
)
from .output_schemas import (
    anchor_batch_output_schema,
    batch_output_schema,
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
    build_collected_review_prompt,
    build_collected_render_prompt,
    build_conversation_segmentation_message_refs,
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
    parse_collected_grouping_function_payload,
    parse_collected_merge_payload,
    parse_conversation_segmentation_payload,
    parse_merge_payload,
    parse_personal_grouping_function_payload,
    parse_personal_fact_review_payload,
    parse_retention_review_payload,
    parse_segment_batch_analysis_payload,
)

logger = logging.getLogger("worktrace")

class _FirstStreamEventTimeoutError(TimeoutError):
    pass


@dataclass(frozen=True)
class _FirstStreamEventResult:
    stream: object
    iterator: Iterator[object]
    event: object | None


def _apply_soft_no_think(prompt: str) -> str:
    return prepare_model_prompt(prompt, append_no_think=True)


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


def _parse_function_arguments(value: object) -> object:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        raise RetryableAnalyzerProtocolError(
            "Online LLM Function call did not contain arguments."
        )
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise RetryableAnalyzerProtocolError(
            "Online LLM Function arguments were not valid JSON."
        ) from exc


def _extract_function_arguments_from_responses_payload(
    payload: object,
    *,
    expected_name: str,
) -> object:
    if not isinstance(payload, dict):
        raise RetryableAnalyzerProtocolError(
            "Online LLM response did not contain a Function call."
        )
    output = payload.get("output")
    if not isinstance(output, list):
        response = payload.get("response")
        output = response.get("output") if isinstance(response, dict) else None
    calls = [
        item
        for item in output or []
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]
    if len(calls) != 1:
        raise RetryableAnalyzerProtocolError(
            "Online LLM response must contain exactly one Function call."
        )
    call = calls[0]
    actual_name = str(call.get("name", ""))
    if actual_name != expected_name:
        raise RetryableAnalyzerProtocolError(
            "Online LLM called an unexpected Function: "
            f"expected={expected_name} actual={actual_name}"
        )
    return _parse_function_arguments(call.get("arguments"))


def _extract_stream_function_arguments(
    event_payloads: list[dict[str, object]],
    *,
    expected_name: str,
) -> object:
    chunks_by_call: dict[str, list[str]] = {}
    names_by_call: dict[str, str] = {}
    completed_payload: dict[str, object] | None = None
    for event in event_payloads:
        event_type = event.get("type")
        if event_type == "response.completed":
            completed_payload = event
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            call_key = str(
                item.get("id")
                or item.get("call_id")
                or event.get("item_id")
                or event.get("output_index")
                or "0"
            )
            name = item.get("name")
            if isinstance(name, str) and name:
                names_by_call[call_key] = name
            arguments = item.get("arguments")
            if isinstance(arguments, str) and arguments:
                chunks_by_call[call_key] = [arguments]
        if event_type == "response.function_call_arguments.delta":
            call_key = str(
                event.get("item_id")
                or event.get("call_id")
                or event.get("output_index")
                or "0"
            )
            delta = event.get("delta")
            if isinstance(delta, str):
                chunks_by_call.setdefault(call_key, []).append(delta)
            name = event.get("name")
            if isinstance(name, str) and name:
                names_by_call[call_key] = name

    call_keys = set(chunks_by_call) | set(names_by_call)
    if len(call_keys) > 1:
        raise RetryableAnalyzerProtocolError(
            "Online LLM stream must contain exactly one Function call."
        )
    if call_keys:
        call_key = next(iter(call_keys))
        actual_name = names_by_call.get(call_key, "")
        if actual_name and actual_name != expected_name:
            raise RetryableAnalyzerProtocolError(
                "Online LLM stream called an unexpected Function: "
                f"expected={expected_name} actual={actual_name}"
            )

    if completed_payload is not None:
        try:
            return _extract_function_arguments_from_responses_payload(
                completed_payload,
                expected_name=expected_name,
            )
        except RetryableAnalyzerProtocolError:
            if not chunks_by_call:
                raise
    if len(call_keys) != 1:
        raise RetryableAnalyzerProtocolError(
            "Online LLM stream must contain exactly one Function call."
        )
    call_key = next(iter(call_keys))
    actual_name = names_by_call.get(call_key, "")
    if actual_name != expected_name:
        raise RetryableAnalyzerProtocolError(
            "Online LLM stream called an unexpected Function: "
            f"expected={expected_name} actual={actual_name or 'missing'}"
        )
    return _parse_function_arguments("".join(chunks_by_call.get(call_key, [])))


def _has_usage(payload: dict[str, object]) -> bool:
    if isinstance(payload.get("usage"), dict):
        return True
    response = payload.get("response")
    return isinstance(response, dict) and isinstance(response.get("usage"), dict)


def _build_responses_request_body(
    prompt: str,
    *,
    settings: OnlineLLMSettings,
    function_spec: FunctionCallSpec,
) -> dict[str, object]:
    prompt = _apply_soft_no_think(function_spec.prompt_with_example(prompt))
    body: dict[str, object] = {
        "model": settings.model,
        "input": prompt,
        "stream": settings.stream_enabled,
        "tools": [function_spec.tool()],
        "tool_choice": function_spec.tool_choice(),
        "parallel_tool_calls": False,
    }
    if settings.stream_enabled:
        body["stream_options"] = {"include_usage": True}
    if settings.reasoning_effort == "none":
        body["reasoning"] = {"effort": "none"}
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


def _set_stream_body_read_timeout(stream: object, timeout_seconds: float) -> None:
    response = getattr(stream, "response", None)
    request = getattr(response, "request", None)
    extensions = getattr(request, "extensions", None)
    if not isinstance(extensions, dict):
        return
    timeout = extensions.get("timeout")
    if isinstance(timeout, dict):
        # HTTPX passes this same mapping to the unread response body.
        timeout["read"] = timeout_seconds


def _close_stream(stream: object) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        close()


def _read_first_stream_event(
    client: OpenAI,
    body: dict[str, object],
    *,
    first_response_timeout_seconds: float,
    subsequent_read_timeout_seconds: float,
) -> _FirstStreamEventResult:
    result_queue: Queue[_FirstStreamEventResult | BaseException] = Queue(maxsize=1)
    state_lock = threading.Lock()
    cancelled = threading.Event()
    state: dict[str, object] = {}

    def read_first_event() -> None:
        try:
            stream = client.responses.create(**body)
            _set_stream_body_read_timeout(stream, subsequent_read_timeout_seconds)
            with state_lock:
                state["stream"] = stream
                should_cancel = cancelled.is_set()
            if should_cancel:
                _close_stream(stream)
                return

            iterator = iter(stream)
            try:
                event = next(iterator)
            except StopIteration:
                event = None

            with state_lock:
                if cancelled.is_set():
                    return
                result_queue.put_nowait(
                    _FirstStreamEventResult(
                        stream=stream,
                        iterator=iterator,
                        event=event,
                    )
                )
        except BaseException as exc:
            with state_lock:
                if not cancelled.is_set():
                    result_queue.put_nowait(exc)

    worker = threading.Thread(
        target=read_first_event,
        name="worktrace-first-stream-event",
        daemon=True,
    )
    worker.start()
    timeout_seconds = min(
        first_response_timeout_seconds,
        subsequent_read_timeout_seconds,
    )
    try:
        result = result_queue.get(timeout=timeout_seconds)
    except Empty as exc:
        with state_lock:
            cancelled.set()
            stream = state.get("stream")
        if stream is not None:
            _close_stream(stream)
        raise _FirstStreamEventTimeoutError(
            "Online LLM did not return its first stream event before the configured timeout."
        ) from exc

    if isinstance(result, BaseException):
        raise result
    return result


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
        references = message_reference_ids(
            [message for item in batch_input.slices for message in item.messages]
        )
        payload = self._invoke_online(
            self.build_batch_prompt(batch_input),
            function_spec=task_function_call_spec(
                "batch_analysis",
                batch_output_schema(self.config),
                **references,
            ),
        )
        return parse_batch_analysis_payload(payload)

    def request_function(
        self,
        prompt: str,
        *,
        function_spec: FunctionCallSpec,
        allow_oversized_input: bool = False,
    ) -> object:
        return self._invoke_online(
            prompt,
            function_spec=function_spec,
            allow_oversized_input=allow_oversized_input,
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
        allow_oversized_input: bool = False,
    ) -> ConversationSegmentationResult:
        message_refs = build_conversation_segmentation_message_refs(messages)
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
            function_spec=task_function_call_spec(
                "conversation_segmentation",
                conversation_segmentation_output_schema(),
                enum_values={
                    "segment_start_message_ids": list(message_refs.values())
                },
            ),
            **oversized_input_kwargs(allow_oversized_input),
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
        references = message_reference_ids(
            [message for item in batch.segments for message in item.messages]
        )
        payload = self._invoke_online(
            self.build_segment_batch_prompt(batch),
            function_spec=task_function_call_spec(
                "segment_batch_analysis",
                segment_batch_output_schema(self.config),
                segment_ids=[item.segment_id for item in batch.segments],
                result_count=len(batch.segments),
                **references,
            ),
            **oversized_input_kwargs(batch.oversized_singleton),
        )
        return parse_segment_batch_analysis_payload(payload)

    def build_retention_review_prompt(self, batch: RetentionReviewBatch) -> str:
        return build_retention_review_prompt(batch, config=self.config)

    def review_retention_candidates(
        self,
        batch: RetentionReviewBatch,
    ) -> RetentionReviewResult:
        references = message_reference_ids(
            [message for item in batch.candidates for message in item.messages]
        )
        payload = self._invoke_online(
            self.build_retention_review_prompt(batch),
            function_spec=task_function_call_spec(
                "retention_review",
                retention_review_output_schema(self.config),
                draft_ids=[item.candidate.draft_id for item in batch.candidates],
                result_count=len(batch.candidates),
                **references,
            ),
            **oversized_input_kwargs(
                batch.oversized_singleton or batch.oversized_retry
            ),
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
        references = message_reference_ids(
            [message for item in batch.candidates for message in item.messages]
        )
        payload = self._invoke_online(
            self.build_personal_fact_review_prompt(batch),
            function_spec=task_function_call_spec(
                "personal_fact_review",
                personal_fact_review_output_schema(batch),
                draft_ids=[item.candidate.draft_id for item in batch.candidates],
                result_count=len(batch.candidates),
                **references,
            ),
            **oversized_input_kwargs(batch.oversized_singleton),
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
        *,
        validation_feedback: str = "",
    ) -> str:
        return build_merge_prompt(
            target_date,
            candidates,
            config=self.config,
            validation_feedback=validation_feedback,
        )

    def analyze_anchor_batch(
        self,
        target_date: str,
        anchor_units: list[AnchorUnit],
    ) -> BatchAnchorAnalysisResult:
        references = message_reference_ids(
            [message for item in anchor_units for message in item.messages]
        )
        payload = self._invoke_online(
            build_anchor_batch_analysis_prompt(target_date, anchor_units, config=self.config),
            function_spec=task_function_call_spec(
                "anchor_batch_analysis",
                anchor_batch_output_schema(self.config),
                anchor_unit_ids=[item.anchor_unit_id for item in anchor_units],
                result_count=len(anchor_units),
                **references,
            ),
            **oversized_input_kwargs(
                len(anchor_units) == 1 and anchor_units[0].oversized_singleton
            ),
        )
        return parse_anchor_batch_analysis_payload(payload)

    def merge_day_candidates(
        self,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
        *,
        validation_feedback: str = "",
    ) -> CrossConversationGroupResult:
        contract = personal_grouping_call_contract(
            config=self.config,
            candidates=candidates,
        )
        payload = self._invoke_online(
            self.build_merge_prompt(
                target_date,
                candidates,
                validation_feedback=validation_feedback,
            ),
            function_spec=contract.function_spec,
            **oversized_input_kwargs(len(candidates) == 1),
        )
        self.last_merge_payload = payload
        return parse_personal_grouping_function_payload(
            payload,
            candidates=candidates,
            allowed_semantic_reasons=contract.semantic_reasons,
        )

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
            function_spec=task_function_call_spec(
                "collected_event_merge",
                collected_merge_output_schema(),
                draft_ids=[item.draft_id for item in events],
                exact_array_lengths={"groups": len(deterministic_groups)},
            ),
            **oversized_input_kwargs(
                is_indivisible_collected_request(events, deterministic_groups)
            ),
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
        *,
        validation_feedback: str = "",
    ) -> CollectedGroupingResult:
        contract = collected_grouping_call_contract(
            "collected_candidate_grouping",
            config=self.config,
            events=events,
            deterministic_groups=deterministic_groups,
            include_split_reason=False,
        )
        payload = self._invoke_online(
            build_collected_grouping_prompt(
                target_date,
                events,
                deterministic_groups,
                config=self.config,
                validation_feedback=validation_feedback,
            ),
            function_spec=contract.function_spec,
            **oversized_input_kwargs(
                is_indivisible_collected_request(events, deterministic_groups)
                or bool(validation_feedback)
            ),
        )
        try:
            result, _ = parse_collected_grouping_function_payload(
                payload,
                evidence_catalog=list(contract.evidence_catalog),
                allowed_semantic_reasons=contract.semantic_reasons,
            )
            return result
        except AnalyzerProtocolError as exc:
            raise RetryableAnalyzerProtocolError(str(exc)) from exc

    def review_collected_group(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        candidate_group: CollectedGroupingGroup,
        *,
        review_reasons: list[str] | None = None,
        validation_feedback: str = "",
    ) -> CollectedGroupingResult:
        contract = collected_grouping_call_contract(
            "collected_group_review",
            config=self.config,
            events=events,
            deterministic_groups=[list(candidate_group.draft_ids)],
            include_split_reason=True,
        )
        payload = self._invoke_online(
            build_collected_review_prompt(
                target_date,
                events,
                candidate_group,
                config=self.config,
                review_reasons=review_reasons,
                validation_feedback=validation_feedback,
            ),
            function_spec=contract.function_spec,
            **oversized_input_kwargs(True),
        )
        try:
            result, _ = parse_collected_grouping_function_payload(
                payload,
                evidence_catalog=list(contract.evidence_catalog),
                allowed_semantic_reasons=contract.semantic_reasons,
            )
            return result
        except AnalyzerProtocolError as exc:
            raise RetryableAnalyzerProtocolError(str(exc)) from exc

    def _invoke_online(
        self,
        prompt: str,
        *,
        function_spec: FunctionCallSpec,
        allow_oversized_input: bool = False,
    ) -> object:
        estimates = estimate_structured_input_tokens(
            prompt,
            function_spec=function_spec,
            append_no_think=True,
        )
        estimated_tokens = estimates["input_estimated_tokens"]
        request_kind = function_spec.request_kind
        target_tokens = self.config.model_input_batch_target_tokens
        oversized_singleton = estimated_tokens > target_tokens and allow_oversized_input
        if estimated_tokens > target_tokens and not allow_oversized_input:
            raise ModelInputLimitError(
                "Model input exceeds model_input_batch_target_tokens before online request "
                "and was not marked as an indivisible input: "
                f"estimated_tokens={estimated_tokens} "
                f"target={target_tokens} "
                f"request_kind={request_kind}"
            )
        if oversized_singleton:
            logger.warning(
                "online_llm.oversized_singleton request_kind=%s estimated_input_tokens=%s input_target_tokens=%s",
                request_kind,
                estimated_tokens,
                target_tokens,
            )
        try:
            return self._invoke_online_prepared(
                prompt,
                function_spec=function_spec,
                estimated_input_tokens=estimated_tokens,
                input_target_tokens=target_tokens,
                oversized_singleton=oversized_singleton,
            )
        except AnalyzerProtocolError as exc:
            exc.estimated_input_tokens = estimated_tokens
            exc.input_target_tokens = target_tokens
            exc.oversized_singleton = oversized_singleton
            raise

    def _invoke_online_prepared(
        self,
        prompt: str,
        *,
        function_spec: FunctionCallSpec,
        estimated_input_tokens: int,
        input_target_tokens: int,
        oversized_singleton: bool,
    ) -> object:
        started_at = perf_counter()
        settings = self.settings_loader(self.config, cwd=self.cwd, environ=None)
        body = _build_responses_request_body(
            prompt,
            settings=settings,
            function_spec=function_spec,
        )
        request_kind = function_spec.request_kind

        try:
            payload, usage_payload = self._invoke_via_sdk(
                settings,
                body,
                function_spec=function_spec,
            )
        except AuthenticationError as exc:
            raise AnalyzerProtocolError("HTTP 401: invalid API key or authentication failed.") from exc
        except PermissionDeniedError as exc:
            raise AnalyzerProtocolError("HTTP 403: permission denied.") from exc
        except RateLimitError as exc:
            raise RetryableAnalyzerProtocolError("HTTP 429: rate limited.") from exc
        except BadRequestError as exc:
            raise ModelInputRejectedError(str(exc)) from exc
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
        except _FirstStreamEventTimeoutError as exc:
            raise RetryableAnalyzerProtocolError(
                "Request timed out before the first stream event."
            ) from exc
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
            estimated_input_tokens=estimated_input_tokens,
            input_target_tokens=input_target_tokens,
            oversized_singleton=oversized_singleton,
        )
        return payload

    def _invoke_via_sdk(
        self,
        settings: OnlineLLMSettings,
        body: dict[str, object],
        *,
        function_spec: FunctionCallSpec,
    ) -> tuple[object, dict[str, object]]:
        http_client = _build_http_client(settings)
        client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url.strip(),
            http_client=http_client,
            max_retries=0,
        )
        try:
            if settings.stream_enabled:
                first_event = _read_first_stream_event(
                    client,
                    body,
                    first_response_timeout_seconds=(
                        settings.stream_first_response_timeout_seconds
                    ),
                    subsequent_read_timeout_seconds=settings.timeout_seconds,
                )
                event_payloads: list[dict[str, object]] = []
                usage_payload: dict[str, object] = {}
                events: Iterator[object]
                if first_event.event is None:
                    events = first_event.iterator
                else:
                    events = chain((first_event.event,), first_event.iterator)
                try:
                    for event in events:
                        event_payload = event.model_dump()
                        event_payloads.append(event_payload)
                        if _has_usage(event_payload):
                            usage_payload = event_payload
                finally:
                    _close_stream(first_event.stream)
                arguments = _extract_stream_function_arguments(
                    event_payloads,
                    expected_name=function_spec.name,
                )
                return arguments, usage_payload or (event_payloads[-1] if event_payloads else {})
            response = client.responses.create(**body)
            response_payload = response.model_dump()
            return (
                _extract_function_arguments_from_responses_payload(
                    response_payload,
                    expected_name=function_spec.name,
                ),
                response_payload,
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
            else:
                http_client.close()
