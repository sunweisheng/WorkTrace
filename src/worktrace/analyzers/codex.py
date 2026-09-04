from __future__ import annotations

import json
import inspect
import logging
import os
import random
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter, sleep
from typing import Any, Callable, ClassVar, Sequence

from ..config import RuntimeConfig, load_codex_llm_settings
from ..errors import (
    AnalyzerProtocolError,
    ModelInputLimitError,
    RetryableAnalyzerProtocolError,
)
from ..logging_utils import log_timing
from ..llm_usage import LLMUsageRecorder
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
from ..utils.json_io import load_json_object
from ..utils.token_estimation import (
    estimate_model_input_tokens,
    estimate_structured_input_tokens,
)
from .base import Analyzer, is_indivisible_collected_request, oversized_input_kwargs
from .function_calls import (
    COLLECTED_GROUPING_FINAL_PARAMETER_CHECKS,
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

CODEX_GLOBAL_CONCURRENCY_LIMIT = 3
CODEX_GLOBAL_SEMAPHORE = threading.BoundedSemaphore(CODEX_GLOBAL_CONCURRENCY_LIMIT)
_CODEX_ENV_ALLOWLIST = frozenset(
    {
        "ALL_PROXY",
        "CODEX_HOME",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_COLOR",
        "NO_PROXY",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "USER",
    }
)
_CODEX_SAFE_ITEM_TYPES = frozenset({"agent_message", "reasoning"})
_CODEX_NON_RETRYABLE_FAILURE_CATEGORIES = frozenset(
    {"authentication", "permission", "configuration"}
)


def build_codex_subprocess_env(
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    return {
        key: value
        for key, value in source.items()
        if key in _CODEX_ENV_ALLOWLIST and isinstance(value, str)
    }


def validate_codex_jsonl_events(stdout: str) -> tuple[dict[str, object], ...]:
    events: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(stdout.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RetryableAnalyzerProtocolError(
                f"Codex JSON event stream is invalid at line {line_number}."
            ) from exc
        if not isinstance(event, dict):
            raise RetryableAnalyzerProtocolError(
                f"Codex JSON event stream has a non-object at line {line_number}."
            )
        item = event.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if not isinstance(item_type, str) or item_type not in _CODEX_SAFE_ITEM_TYPES:
                raise AnalyzerProtocolError(
                    "Codex protocol violation: tool or unsupported item was emitted "
                    f"(item_type={item_type or 'missing'})."
                )
        events.append(event)
    if not events:
        raise RetryableAnalyzerProtocolError("Codex JSON event stream is empty.")
    return tuple(events)


def codex_failure_category_from_process(result: object) -> str:
    combined = (
        f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}"
    ).lower()
    if any(marker in combined for marker in ("401", "unauthorized", "api key")):
        return "authentication"
    if any(marker in combined for marker in ("403", "permission denied")):
        return "permission"
    if "error loading config" in combined:
        return "configuration"
    if any(marker in combined for marker in ("429", "rate limit")):
        return "rate_limited"
    retryable_markers = (
        "timed out",
        "timeout",
        "network",
        "connection",
        "unreachable",
        "service unavailable",
        "server error",
        "status 500",
        "status 502",
        "status 503",
        "status 504",
    )
    if any(marker in combined for marker in retryable_markers):
        return "technical"
    return "command_failed"


def _codex_command_error(result: object) -> AnalyzerProtocolError:
    category = codex_failure_category_from_process(result)
    message = _format_process_failure(
        "Codex analysis command failed.",
        result,
        failure_category=category,
    )
    if category not in _CODEX_NON_RETRYABLE_FAILURE_CATEGORIES and category != "command_failed":
        return RetryableAnalyzerProtocolError(message)
    return AnalyzerProtocolError(message)


@dataclass
class CodexRequestPacer:
    """Reserve Codex start times so concurrent calls keep one shared interval."""

    min_seconds: float
    max_seconds: float
    random_uniform: Callable[[float, float], float] = random.uniform
    sleep_func: Callable[[float], None] = sleep

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._next_start_at = 0.0

    def wait_for_turn(self) -> float:
        now = perf_counter()
        with self._lock:
            wait_seconds = max(0.0, self._next_start_at - now)
            reserved_start = now + wait_seconds
            interval = self.random_uniform(self.min_seconds, self.max_seconds)
            self._next_start_at = reserved_start + interval
        if wait_seconds > 0:
            self.sleep_func(wait_seconds)
        return wait_seconds


def _format_process_failure(
    prefix: str,
    result: object,
    *,
    failure_category: str = "command_failed",
) -> str:
    returncode = getattr(result, "returncode", None)
    stderr = getattr(result, "stderr", "") or ""
    stderr_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    stderr_tail = " | ".join(stderr_lines[-3:])
    details = [f"returncode={returncode}"]
    if failure_category != "command_failed":
        details.append(f"error_category={failure_category}")
    if stderr_tail:
        details.append(f"stderr_tail={stderr_tail}")
    return f"{prefix} ({', '.join(details)})"


@dataclass
class CodexAnalyzer(Analyzer):
    manages_usage_records: ClassVar[bool] = True
    config: RuntimeConfig
    command_runner: Any | None = None
    cwd: Path | None = None
    usage_recorder: LLMUsageRecorder | None = None
    request_pacer: CodexRequestPacer | None = None

    def __post_init__(self) -> None:
        if self.command_runner is None:
            self.command_runner = self._run_command
        if self.cwd is None:
            self.cwd = Path.cwd()
        if self.usage_recorder is None:
            self.usage_recorder = LLMUsageRecorder()
        if self.request_pacer is None:
            self.request_pacer = CodexRequestPacer(
                self.config.codex_request_interval_min_seconds,
                self.config.codex_request_interval_max_seconds,
            )

    def request_function(
        self,
        prompt: str,
        *,
        function_spec: FunctionCallSpec,
        allow_oversized_input: bool = False,
    ) -> object:
        return self._invoke_codex(
            prompt,
            output_schema=function_spec.parameters,
            request_kind=function_spec.request_kind,
            allow_oversized_input=allow_oversized_input,
            function_spec=function_spec,
        )

    def request_text(self, prompt: str, *, image_path: Path | None = None) -> str:
        result = self._invoke_codex(
            prompt,
            request_kind="image_summary" if image_path is not None else "auxiliary_text",
            allow_oversized_input=True,
            image_path=image_path,
            expect_json=False,
        )
        if not isinstance(result, str) or not result.strip():
            raise RetryableAnalyzerProtocolError(
                "Codex text response did not contain text output."
            )
        return result.strip()

    def analyze_batch(
        self,
        target_date: str,
        batch_input: AnalysisBatch,
    ) -> BatchAnalysisResult:
        references = message_reference_ids(
            [message for item in batch_input.slices for message in item.messages]
        )
        payload = self.request_function(
            self.build_batch_prompt(batch_input),
            function_spec=task_function_call_spec(
                "batch_analysis",
                batch_output_schema(self.config),
                **references,
            ),
        )
        return parse_batch_analysis_payload(payload)

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
        payload = self.request_function(
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
        payload = self.request_function(
            self.build_segment_batch_prompt(batch),
            function_spec=task_function_call_spec(
                "segment_batch_analysis",
                segment_batch_output_schema(self.config),
                segment_ids=[item.segment_id for item in batch.segments],
                result_count=len(batch.segments),
                **references,
            ),
            **oversized_input_kwargs(
                batch.oversized_singleton or len(batch.segments) == 1
            ),
        )
        return parse_segment_batch_analysis_payload(payload)

    def build_retention_review_prompt(self, batch: RetentionReviewBatch) -> str:
        return build_retention_review_prompt(batch, config=self.config)

    def review_retention_candidates(
        self,
        batch: RetentionReviewBatch,
    ) -> RetentionReviewResult:
        allowed_message_ids = list(
            dict.fromkeys(
                message_id
                for item in batch.candidates
                for message_id in item.allowed_evidence_message_ids
            )
        )
        payload = self.request_function(
            self.build_retention_review_prompt(batch),
            function_spec=task_function_call_spec(
                "retention_review",
                retention_review_output_schema(self.config),
                draft_ids=[item.candidate.draft_id for item in batch.candidates],
                message_ids=allowed_message_ids,
                result_count=len(batch.candidates),
            ),
            **oversized_input_kwargs(
                batch.oversized_singleton or batch.oversized_retry
            ),
        )
        return parse_retention_review_payload(payload)

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
        payload = self.request_function(
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
        return parse_personal_fact_review_payload(payload)

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
        payload = self.request_function(
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
        payload = self.request_function(
            self.build_merge_prompt(
                target_date,
                candidates,
                validation_feedback=validation_feedback,
            ),
            function_spec=contract.function_spec,
            **oversized_input_kwargs(
                len(candidates) == 1 or bool(validation_feedback)
            ),
        )
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
        payload = self.request_function(
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
        return parse_collected_merge_payload(payload)

    def group_collected_events(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
        *,
        validation_feedback: str = "",
        previous_invalid_assignment: dict[str, object] | None = None,
    ) -> CollectedGroupingResult:
        contract = collected_grouping_call_contract(
            "collected_candidate_grouping",
            config=self.config,
            events=events,
            deterministic_groups=deterministic_groups,
            include_split_reason=False,
            final_parameter_checks=COLLECTED_GROUPING_FINAL_PARAMETER_CHECKS,
        )
        payload = self.request_function(
            build_collected_grouping_prompt(
                target_date,
                events,
                deterministic_groups,
                config=self.config,
                validation_feedback=validation_feedback,
                previous_invalid_assignment=previous_invalid_assignment,
            ),
            function_spec=contract.function_spec,
            **oversized_input_kwargs(
                is_indivisible_collected_request(events, deterministic_groups)
                or bool(validation_feedback)
                or previous_invalid_assignment is not None
            ),
        )
        result, _ = parse_collected_grouping_function_payload(
            payload,
            evidence_catalog=list(contract.evidence_catalog),
            allowed_semantic_reasons=contract.semantic_reasons,
        )
        return result

    def review_collected_group(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        candidate_group: CollectedGroupingGroup,
        *,
        review_reasons: list[str] | None = None,
        validation_feedback: str = "",
        existing_groups: list[CollectedGroupingGroup] | None = None,
        relation_reasons: list[dict[str, object]] | None = None,
        atomic_groups: list[list[str]] | None = None,
    ) -> CollectedGroupingResult:
        relation_ids = [
            str(item.get("relation_id", ""))
            for item in relation_reasons or []
            if str(item.get("relation_id", ""))
        ]
        contract = collected_grouping_call_contract(
            "collected_group_review",
            config=self.config,
            events=events,
            deterministic_groups=[list(candidate_group.draft_ids)],
            include_split_reason=True,
            relation_ids=relation_ids,
        )
        payload = self.request_function(
            build_collected_review_prompt(
                target_date,
                events,
                candidate_group,
                config=self.config,
                review_reasons=review_reasons,
                validation_feedback=validation_feedback,
                existing_groups=existing_groups,
                relation_reasons=relation_reasons,
                atomic_groups=atomic_groups,
            ),
            function_spec=contract.function_spec,
            **oversized_input_kwargs(True),
        )
        result, _ = parse_collected_grouping_function_payload(
            payload,
            evidence_catalog=list(contract.evidence_catalog),
            allowed_semantic_reasons=contract.semantic_reasons,
            allowed_relation_ids=relation_ids,
            allowed_draft_ids=[item.draft_id for item in events],
        )
        return result

    def _run_command(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: int | float | None = None,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
            check=False,
            env=env,
        )

    def _run_isolated_command(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout: int | float,
        input_text: str,
        env: dict[str, str],
    ) -> object:
        runner = self.command_runner
        assert runner is not None
        parameters = inspect.signature(runner).parameters
        accepts_env = "env" in parameters or any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in parameters.values()
        )
        kwargs: dict[str, object] = {
            "cwd": cwd,
            "timeout": timeout,
            "input_text": input_text,
        }
        if accepts_env:
            kwargs["env"] = env
        return runner(tuple(args), **kwargs)

    def _invoke_codex(
        self,
        prompt: str,
        *,
        output_schema: dict[str, object] | None = None,
        request_kind: str = "auxiliary_json",
        allow_oversized_input: bool = False,
        function_spec: FunctionCallSpec | None = None,
        image_path: Path | None = None,
        expect_json: bool = True,
    ) -> object:
        if function_spec is not None:
            estimated_tokens = estimate_structured_input_tokens(
                prompt,
                function_spec=function_spec,
                append_no_think=True,
            )["input_estimated_tokens"]
            command_prompt = function_spec.codex_prompt(prompt).rstrip()
        else:
            estimated_tokens = estimate_model_input_tokens(
                prompt,
                output_schema=output_schema,
            )
            command_prompt = prompt
        target_tokens = self.config.model_input_batch_target_tokens
        oversized_singleton = estimated_tokens > target_tokens and allow_oversized_input
        if estimated_tokens > target_tokens and not allow_oversized_input:
            raise ModelInputLimitError(
                "Model input exceeds model_input_batch_target_tokens before Codex request "
                "and was not marked as an indivisible input: "
                f"estimated_tokens={estimated_tokens} "
                f"target={target_tokens} request_kind={request_kind}"
            )
        if oversized_singleton:
            logger.warning(
                "codex.oversized_singleton request_kind=%s estimated_input_tokens=%s input_target_tokens=%s",
                request_kind,
                estimated_tokens,
                target_tokens,
            )
        input_metrics = {
            "estimated_input_tokens": estimated_tokens,
            "input_target_tokens": target_tokens,
            "oversized_singleton": oversized_singleton,
        }
        settings = load_codex_llm_settings(self.config, cwd=self.cwd)
        wait_seconds = self.request_pacer.wait_for_turn()
        started_at = perf_counter()
        content = ""
        result: object
        try:
            with tempfile.TemporaryDirectory(prefix="worktrace-codex-") as temp_dir_name:
                temp_dir = Path(temp_dir_name)
                output_path = temp_dir / "result.json"
                schema_path: Path | None = None
                if output_schema is not None:
                    schema_path = temp_dir / "parameters.schema.json"
                    schema_path.write_text(
                        json.dumps(output_schema, ensure_ascii=False),
                        encoding="utf-8",
                    )
                isolated_image_path: Path | None = None
                if image_path is not None:
                    isolated_image_path = temp_dir / f"image{image_path.suffix.lower()}"
                    isolated_image_path.write_bytes(image_path.read_bytes())
                args = [
                    "codex",
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--strict-config",
                    "--sandbox",
                    "read-only",
                    "--json",
                    "-c",
                    f"model_provider={json.dumps(settings.provider_id)}",
                    "-c",
                    (
                        "model_providers."
                        f"{settings.provider_id}.name={json.dumps(settings.provider_name)}"
                    ),
                    "-c",
                    (
                        "model_providers."
                        f"{settings.provider_id}.base_url="
                        f"{json.dumps(settings.provider_base_url)}"
                    ),
                    "-c",
                    (
                        "model_providers."
                        f"{settings.provider_id}.wire_api="
                        f"{json.dumps(settings.provider_wire_api)}"
                    ),
                    "-c",
                    (
                        "model_providers."
                        f"{settings.provider_id}.requires_openai_auth="
                        f"{str(settings.provider_requires_openai_auth).lower()}"
                    ),
                    "--model",
                    settings.model,
                    "-c",
                    f"model_reasoning_effort={json.dumps(settings.reasoning_effort)}",
                    "-c",
                    'shell_environment_policy.inherit="none"',
                    "-c",
                    'web_search="disabled"',
                    "--disable",
                    "multi_agent",
                ]
                if isolated_image_path is not None:
                    args.extend(["--image", str(isolated_image_path)])
                if schema_path is not None:
                    args.extend(["--output-schema", str(schema_path)])
                args.extend(["--output-last-message", str(output_path), "-"])
                with CODEX_GLOBAL_SEMAPHORE:
                    result = self._run_isolated_command(
                        args,
                        cwd=temp_dir,
                        timeout=settings.timeout_seconds,
                        input_text=command_prompt,
                        env=build_codex_subprocess_env(),
                    )
                if getattr(result, "returncode", 1) == 0:
                    validate_codex_jsonl_events(str(getattr(result, "stdout", "") or ""))
                    try:
                        content = output_path.read_text(encoding="utf-8").strip()
                    except OSError as exc:
                        raise RetryableAnalyzerProtocolError(
                            "Codex output file is missing."
                        ) from exc
        except AnalyzerProtocolError as exc:
            self.usage_recorder.record(
                request_kind,
                {},
                duration_ms=(perf_counter() - started_at) * 1000,
                prompt_chars=len(prompt),
                backend="codex",
                function_spec=function_spec,
                final_prompt=command_prompt,
                model=settings.model,
                reasoning_effort=settings.reasoning_effort,
                raw_result=content,
                status="failed",
                error_category=(
                    "protocol_violation"
                    if "protocol violation" in str(exc).lower()
                    else "invalid_json"
                    if "json" in str(exc).lower()
                    else "output_missing"
                    if "output" in str(exc).lower() or "empty" in str(exc).lower()
                    else "protocol_error"
                ),
                codex_wait_ms=wait_seconds * 1000,
                **input_metrics,
            )
            raise
        except subprocess.TimeoutExpired as exc:
            log_timing(
                logger,
                "codex.exec.timeout",
                started_at,
                prompt_chars=len(prompt),
                cwd="isolated_temp_dir",
                stdin_mode=True,
            )
            self.usage_recorder.record(
                request_kind,
                {},
                duration_ms=(perf_counter() - started_at) * 1000,
                prompt_chars=len(prompt),
                backend="codex",
                function_spec=function_spec,
                final_prompt=command_prompt,
                model=settings.model,
                reasoning_effort=settings.reasoning_effort,
                raw_result=content,
                status="failed",
                error_category="timeout",
                codex_wait_ms=wait_seconds * 1000,
                **input_metrics,
            )
            raise RetryableAnalyzerProtocolError("Codex analysis timed out.") from exc

        log_timing(
            logger,
            "codex.exec.completed",
            started_at,
            prompt_chars=len(prompt),
            returncode=getattr(result, "returncode", None),
            cwd="isolated_temp_dir",
            stdin_mode=True,
        )
        if getattr(result, "returncode", 1) != 0:
            error = _codex_command_error(result)
            self.usage_recorder.record(
                request_kind,
                {},
                duration_ms=(perf_counter() - started_at) * 1000,
                prompt_chars=len(prompt),
                backend="codex",
                function_spec=function_spec,
                final_prompt=command_prompt,
                model=settings.model,
                reasoning_effort=settings.reasoning_effort,
                raw_result=content,
                status="failed",
                error_category=codex_failure_category_from_process(result),
                codex_wait_ms=wait_seconds * 1000,
                **input_metrics,
            )
            raise error

        if not content:
            self.usage_recorder.record(
                request_kind,
                {},
                duration_ms=(perf_counter() - started_at) * 1000,
                prompt_chars=len(prompt),
                backend="codex",
                function_spec=function_spec,
                final_prompt=command_prompt,
                model=settings.model,
                reasoning_effort=settings.reasoning_effort,
                raw_result=content,
                status="failed",
                error_category="output_missing",
                codex_wait_ms=wait_seconds * 1000,
                **input_metrics,
            )
            raise RetryableAnalyzerProtocolError("Codex output is empty.")

        if not expect_json:
            self.usage_recorder.record(
                request_kind,
                {},
                duration_ms=(perf_counter() - started_at) * 1000,
                prompt_chars=len(prompt),
                backend="codex",
                function_spec=function_spec,
                final_prompt=command_prompt,
                model=settings.model,
                reasoning_effort=settings.reasoning_effort,
                raw_result=content,
                codex_wait_ms=wait_seconds * 1000,
                **input_metrics,
            )
            return content

        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            try:
                payload = load_json_object(content)
            except ValueError as exc:
                self.usage_recorder.record(
                    request_kind,
                    {},
                    duration_ms=(perf_counter() - started_at) * 1000,
                    prompt_chars=len(prompt),
                    backend="codex",
                    function_spec=function_spec,
                    final_prompt=command_prompt,
                    model=settings.model,
                    reasoning_effort=settings.reasoning_effort,
                    raw_result=content,
                    status="failed",
                    error_category="invalid_json",
                    codex_wait_ms=wait_seconds * 1000,
                    **input_metrics,
                )
                raise RetryableAnalyzerProtocolError(
                    "Codex did not return valid JSON."
                ) from exc
        self.usage_recorder.record(
            request_kind,
            {},
            duration_ms=(perf_counter() - started_at) * 1000,
            prompt_chars=len(prompt),
            backend="codex",
            function_spec=function_spec,
            final_prompt=command_prompt,
            model=settings.model,
            reasoning_effort=settings.reasoning_effort,
            raw_result=payload,
            codex_wait_ms=wait_seconds * 1000,
            **input_metrics,
        )
        return payload
