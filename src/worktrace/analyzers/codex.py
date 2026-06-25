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
    BatchAnalysisResult,
    BatchAnchorAnalysisResult,
    BucketMergedDraft,
    CrossConversationGroupResult,
    CrossBucketMergeResult,
    CrossMergeBucketResult,
    SourceBackedEventDraft,
)
from ..utils.json_io import load_json_object
from .base import Analyzer
from .output_schemas import (
    anchor_batch_output_schema,
    batch_output_schema,
    bucket_output_schema,
    cross_bucket_merge_output_schema,
    merge_output_schema,
)
from .prompts import (
    build_batch_analysis_prompt,
    build_anchor_batch_analysis_prompt,
    build_cross_bucket_merge_prompt,
    build_cross_merge_bucket_prompt,
    build_merge_prompt,
)
from .protocol import (
    parse_anchor_batch_analysis_payload,
    parse_batch_analysis_payload,
    parse_merge_payload,
    parse_cross_bucket_merge_payload,
    parse_cross_merge_bucket_payload,
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
            build_batch_analysis_prompt(batch_input, config=self.config),
            output_schema=batch_output_schema(),
        )
        return parse_batch_analysis_payload(payload)

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

    def bucket_cross_merge_candidates(
        self,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
    ) -> CrossMergeBucketResult:
        payload = self._invoke_codex(
            build_cross_merge_bucket_prompt(target_date, candidates),
            output_schema=bucket_output_schema(),
        )
        return parse_cross_merge_bucket_payload(payload)

    def decide_cross_bucket_merges(
        self,
        target_date: str,
        merged_buckets: list[BucketMergedDraft],
        candidate_pairs: list[tuple[str, str]],
    ) -> CrossBucketMergeResult:
        payload = self._invoke_codex(
            build_cross_bucket_merge_prompt(target_date, merged_buckets, candidate_pairs),
            output_schema=cross_bucket_merge_output_schema(),
        )
        return parse_cross_bucket_merge_payload(payload)

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
