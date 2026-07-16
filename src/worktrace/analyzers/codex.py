from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Sequence

from ..config import RuntimeConfig
from ..errors import AnalyzerProtocolError
from ..logging_utils import log_timing
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
    SourceBackedEventDraft,
    SegmentAnalysisBatch,
    NormalizedMessage,
    ResponseSignal,
)
from ..utils.json_io import load_json_object
from ..utils.token_estimation import estimate_text_tokens
from .base import Analyzer
from .output_schemas import (
    anchor_batch_output_schema,
    batch_output_schema,
    collected_grouping_output_schema,
    collected_merge_output_schema,
    conversation_segmentation_output_schema,
    merge_output_schema,
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
    parse_segment_batch_analysis_payload,
)

logger = logging.getLogger("worktrace")


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

    def __post_init__(self) -> None:
        if self.command_runner is None:
            self.command_runner = self._run_command
        if self.cwd is None:
            self.cwd = Path.cwd()

    def analyze_batch(
        self,
        target_date: str,
        batch_input: AnalysisBatch,
    ) -> BatchAnalysisResult:
        payload = self._invoke_codex(
            self.build_batch_prompt(batch_input),
            output_schema=batch_output_schema(),
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
            output_schema=segment_batch_output_schema(),
        )
        return parse_segment_batch_analysis_payload(payload)

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
            output_schema=anchor_batch_output_schema(),
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
    ) -> object:
        estimated_tokens = estimate_text_tokens(prompt)
        if estimated_tokens > self.config.max_model_input_tokens:
            raise AnalyzerProtocolError(
                "Model input exceeds max_model_input_tokens before Codex request: "
                f"estimated_tokens={estimated_tokens} "
                f"limit={self.config.max_model_input_tokens}"
            )
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
            raise AnalyzerProtocolError(
                _format_process_failure("Codex analysis command failed.", result)
            )

        try:
            content = output_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AnalyzerProtocolError("Codex output file is missing.") from exc
        finally:
            output_path.unlink(missing_ok=True)
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            try:
                return load_json_object(content)
            except ValueError as exc:
                raise AnalyzerProtocolError("Codex did not return valid JSON.") from exc
