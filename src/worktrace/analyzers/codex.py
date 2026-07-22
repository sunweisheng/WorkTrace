from __future__ import annotations

import json
import logging
import random
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter, sleep
from typing import Any, Callable, Sequence

from ..config import RuntimeConfig
from ..errors import AnalyzerProtocolError, ModelInputLimitError
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
from ..utils.token_estimation import estimate_model_input_tokens
from .base import Analyzer, is_indivisible_collected_request, oversized_input_kwargs
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


def _format_process_failure(prefix: str, result: object) -> str:
    returncode = getattr(result, "returncode", None)
    stderr = getattr(result, "stderr", "") or ""
    stderr_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    stderr_tail = " | ".join(stderr_lines[-3:])
    if stderr_tail:
        return f"{prefix} (returncode={returncode}, stderr_tail={stderr_tail})"
    return f"{prefix} (returncode={returncode})"


@dataclass
class CodexAnalyzer(Analyzer):
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

    def analyze_batch(
        self,
        target_date: str,
        batch_input: AnalysisBatch,
    ) -> BatchAnalysisResult:
        payload = self._invoke_codex(
            self.build_batch_prompt(batch_input),
            output_schema=batch_output_schema(self.config),
            request_kind="batch_analysis",
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
        payload = self._invoke_codex(
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
        payload = self._invoke_codex(
            self.build_segment_batch_prompt(batch),
            output_schema=segment_batch_output_schema(self.config),
            request_kind="segment_batch_analysis",
            **oversized_input_kwargs(batch.oversized_singleton),
        )
        return parse_segment_batch_analysis_payload(payload)

    def build_retention_review_prompt(self, batch: RetentionReviewBatch) -> str:
        return build_retention_review_prompt(batch, config=self.config)

    def review_retention_candidates(
        self,
        batch: RetentionReviewBatch,
    ) -> RetentionReviewResult:
        payload = self._invoke_codex(
            self.build_retention_review_prompt(batch),
            output_schema=retention_review_output_schema(self.config),
            request_kind="retention_review",
            **oversized_input_kwargs(batch.oversized_singleton),
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
        payload = self._invoke_codex(
            self.build_personal_fact_review_prompt(batch),
            output_schema=personal_fact_review_output_schema(batch),
            request_kind="personal_fact_review",
            **oversized_input_kwargs(batch.oversized_singleton),
        )
        return parse_personal_fact_review_payload(payload)

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
        payload = self._invoke_codex(
            build_anchor_batch_analysis_prompt(target_date, anchor_units, config=self.config),
            output_schema=anchor_batch_output_schema(self.config),
            request_kind="anchor_batch_analysis",
            **oversized_input_kwargs(
                len(anchor_units) == 1 and anchor_units[0].oversized_singleton
            ),
        )
        return parse_anchor_batch_analysis_payload(payload)

    def merge_day_candidates(
        self,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
    ) -> CrossConversationGroupResult:
        payload = self._invoke_codex(
            build_merge_prompt(target_date, candidates),
            output_schema=merge_output_schema(),
            request_kind="day_candidate_merge",
            **oversized_input_kwargs(len(candidates) == 1),
        )
        return parse_merge_payload(payload)

    def merge_collected_events(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        deterministic_groups: list[list[str]],
    ) -> CollectedMergeResult:
        payload = self._invoke_codex(
            build_collected_render_prompt(
                target_date,
                events,
                deterministic_groups,
                config=self.config,
            ),
            output_schema=collected_merge_output_schema(),
            request_kind="collected_event_merge",
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
    ) -> CollectedGroupingResult:
        payload = self._invoke_codex(
            build_collected_grouping_prompt(
                target_date,
                events,
                deterministic_groups,
                config=self.config,
            ),
            output_schema=collected_grouping_output_schema(),
            request_kind="collected_candidate_grouping",
            **oversized_input_kwargs(
                is_indivisible_collected_request(events, deterministic_groups)
            ),
        )
        return parse_collected_grouping_payload(payload)

    def review_collected_group(
        self,
        target_date: str,
        events: list[CollectedSourceEvent],
        candidate_group: CollectedGroupingGroup,
        *,
        review_reasons: list[str] | None = None,
    ) -> CollectedGroupingResult:
        payload = self._invoke_codex(
            build_collected_review_prompt(
                target_date,
                events,
                candidate_group,
                config=self.config,
                review_reasons=review_reasons,
            ),
            output_schema=collected_grouping_output_schema(),
            request_kind="collected_group_review",
            **oversized_input_kwargs(True),
        )
        return parse_collected_grouping_payload(payload)

    def _run_command(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: int | float | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
            check=False,
        )

    def _invoke_codex(
        self,
        prompt: str,
        *,
        output_schema: dict[str, object] | None = None,
        request_kind: str = "auxiliary_json",
        allow_oversized_input: bool = False,
    ) -> object:
        request_schema = output_schema if self.config.codex_stdin_mode else None
        estimated_tokens = estimate_model_input_tokens(
            prompt,
            output_schema=request_schema,
        )
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
        with tempfile.NamedTemporaryFile(
            prefix="worktrace-codex-",
            suffix=".json",
            dir=str(self.cwd),
            delete=False,
        ) as handle:
            output_path = Path(handle.name)
        schema_path: Path | None = None
        schema_handle = None
        if self.config.codex_stdin_mode and output_schema is not None:
            schema_handle = tempfile.NamedTemporaryFile(
                prefix="worktrace-codex-schema-",
                suffix=".json",
                dir=str(self.cwd),
                delete=False,
            )
            schema_path = Path(schema_handle.name)
            schema_handle.write(
                json.dumps(output_schema, ensure_ascii=False).encode("utf-8")
            )
            schema_handle.flush()
            schema_handle.close()

        wait_seconds = self.request_pacer.wait_for_turn()
        started_at = perf_counter()
        try:
            if self.config.codex_stdin_mode:
                args = [
                    "codex",
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--color",
                    "never",
                    "-s",
                    "read-only",
                    "-o",
                    str(output_path),
                ]
                if schema_path is not None:
                    args.extend(["--output-schema", str(schema_path)])
                args.append("-")
                result = self.command_runner(
                    tuple(args),
                    cwd=self.cwd,
                    timeout=self.config.analyzer_timeout_seconds,
                    input_text=prompt,
                )
            else:
                result = self.command_runner(
                    (
                        "codex",
                        "exec",
                        "--skip-git-repo-check",
                        "--ephemeral",
                        "--color",
                        "never",
                        "-s",
                        "read-only",
                        "-o",
                        str(output_path),
                        prompt,
                    ),
                    cwd=self.cwd,
                    timeout=self.config.analyzer_timeout_seconds,
                )
        except subprocess.TimeoutExpired as exc:
            output_path.unlink(missing_ok=True)
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)
            log_timing(
                logger,
                "codex.exec.timeout",
                started_at,
                prompt_chars=len(prompt),
                cwd=str(self.cwd),
                stdin_mode=self.config.codex_stdin_mode,
            )
            self.usage_recorder.record(
                request_kind,
                {},
                duration_ms=(perf_counter() - started_at) * 1000,
                prompt_chars=len(prompt),
                backend="codex",
                status="failed",
                error_category="timeout",
                codex_wait_ms=wait_seconds * 1000,
                **input_metrics,
            )
            raise AnalyzerProtocolError("Codex analysis timed out.") from exc

        log_timing(
            logger,
            "codex.exec.completed",
            started_at,
            prompt_chars=len(prompt),
            returncode=getattr(result, "returncode", None),
            cwd=str(self.cwd),
            stdin_mode=self.config.codex_stdin_mode,
        )
        if getattr(result, "returncode", 1) != 0:
            output_path.unlink(missing_ok=True)
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)
            error = AnalyzerProtocolError(
                _format_process_failure("Codex analysis command failed.", result)
            )
            self.usage_recorder.record(
                request_kind,
                {},
                duration_ms=(perf_counter() - started_at) * 1000,
                prompt_chars=len(prompt),
                backend="codex",
                status="failed",
                error_category="command_failed",
                codex_wait_ms=wait_seconds * 1000,
                **input_metrics,
            )
            raise error

        try:
            content = output_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            self.usage_recorder.record(
                request_kind,
                {},
                duration_ms=(perf_counter() - started_at) * 1000,
                prompt_chars=len(prompt),
                backend="codex",
                status="failed",
                error_category="output_missing",
                codex_wait_ms=wait_seconds * 1000,
                **input_metrics,
            )
            raise AnalyzerProtocolError("Codex output file is missing.") from exc
        finally:
            output_path.unlink(missing_ok=True)
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)

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
                    status="failed",
                    error_category="invalid_json",
                    codex_wait_ms=wait_seconds * 1000,
                    **input_metrics,
                )
                raise AnalyzerProtocolError("Codex did not return valid JSON.") from exc
        self.usage_recorder.record(
            request_kind,
            {},
            duration_ms=(perf_counter() - started_at) * 1000,
            prompt_chars=len(prompt),
            backend="codex",
            codex_wait_ms=wait_seconds * 1000,
            **input_metrics,
        )
        return payload
