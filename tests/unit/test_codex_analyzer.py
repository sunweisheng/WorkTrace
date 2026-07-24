from __future__ import annotations

from dataclasses import replace
import json
import subprocess
from pathlib import Path

import pytest

from src.worktrace.analyzers.codex import CodexAnalyzer, CodexRequestPacer
from src.worktrace.analyzers.function_calls import FunctionCallSpec
from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.errors import AnalyzerProtocolError, ModelInputLimitError
from src.worktrace.models import (
    AnalysisBatch,
    ConversationSlice,
    NormalizedMessage,
    PersonalFactReviewBatch,
    PersonalFactReviewCandidate,
    RetentionReviewBatch,
    RetentionReviewCandidate,
    SourceBackedEventDraft,
)
from src.worktrace.utils.token_estimation import estimate_model_input_tokens


def sample_batch() -> AnalysisBatch:
    return AnalysisBatch(
        target_date="2026-06-23",
        batch_id="batch-001",
        retry_round=0,
        estimated_tokens=123,
        slices=[
            ConversationSlice(
                slice_id="slice-1",
                conversation_id="oc_1",
                conversation_name="项目群",
                anchor_message_ids=["om_1"],
                in_day_message_ids=["om_1"],
                messages=[
                    NormalizedMessage(
                        conversation_id="oc_1",
                        conversation_name="项目群",
                        message_id="om_1",
                        sender_open_id="ou_1",
                        sender_name="Alice",
                        send_time="2026-06-23T10:00:00+08:00",
                        message_type="text",
                        text="推进发布",
                        reply_to_message_id=None,
                        quote_message_id=None,
                    )
                ],
            )
        ],
    )


def sample_retention_review_batch() -> RetentionReviewBatch:
    message = sample_batch().slices[0].messages[0]
    candidate = SourceBackedEventDraft(
        draft_id="draft-1",
        date="2026-06-23",
        topic="临时协作复核",
        content="复核原聊天是否形成实质工作。",
        source_message_ids=[message.message_id],
        source_conversation_id=message.conversation_id,
        source_slice_id="slice-1",
        confidence=0.8,
        action_label="确认",
        object_hint="协作事项",
        retention_reason="follow_up_assigned",
        retention_detail="原聊天存在待复核的后续动作。",
    )
    return RetentionReviewBatch(
        target_date="2026-06-23",
        batch_id="retention-review-001",
        candidates=[
            RetentionReviewCandidate(
                candidate=candidate,
                messages=[message],
                allowed_evidence_message_ids=[message.message_id],
            )
        ],
    )


def sample_personal_fact_review_batch() -> PersonalFactReviewBatch:
    retention_batch = sample_retention_review_batch()
    item = retention_batch.candidates[0]
    return PersonalFactReviewBatch(
        target_date=retention_batch.target_date,
        batch_id="personal-fact-review-001",
        candidates=[
            PersonalFactReviewCandidate(
                candidate=item.candidate,
                messages=item.messages,
                allowed_evidence_message_ids=item.allowed_evidence_message_ids,
                review_reasons=["missing_or_incomplete_fact_evidence"],
            )
        ],
    )


def test_codex_analyzer_uses_retention_review_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_runtime_config_overrides(
        RuntimeConfig(data_root=tmp_path / "data", analyzer_backend="codex"),
        cwd=Path.cwd(),
    )
    analyzer = CodexAnalyzer(config=config, cwd=tmp_path)
    captured: dict[str, object] = {}

    def fake_invoke(
        prompt,
        *,
        output_schema,
        request_kind="auxiliary_json",
        **kwargs,
    ):
        captured.update(
            prompt=prompt,
            output_schema=output_schema,
            request_kind=request_kind,
            **kwargs,
        )
        return {
            "results": [
                {
                    "draft_id": "draft-1",
                    "routine_signals": [],
                    "substantive_signals": [],
                }
            ]
        }

    monkeypatch.setattr(analyzer, "_invoke_codex", fake_invoke)

    result = analyzer.review_retention_candidates(
        replace(
            sample_retention_review_batch(),
            retry_feedback="证据消息不属于当前候选。",
            oversized_retry=True,
        )
    )

    assert result.results[0].draft_id == "draft-1"
    assert "不要决定保留或删除" in str(captured["prompt"])
    assert "证据消息不属于当前候选。" in str(captured["prompt"])
    assert captured["allow_oversized_input"] is True
    assert captured["request_kind"] == "retention_review"
    function_spec = captured["function_spec"]
    assert isinstance(function_spec, FunctionCallSpec)
    assert captured["output_schema"] == function_spec.parameters


def test_codex_analyzer_uses_personal_fact_review_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_runtime_config_overrides(
        RuntimeConfig(data_root=tmp_path / "data", analyzer_backend="codex"),
        cwd=Path.cwd(),
    )
    analyzer = CodexAnalyzer(config=config, cwd=tmp_path)
    captured: dict[str, object] = {}

    def fake_invoke(
        prompt,
        *,
        output_schema,
        request_kind="auxiliary_json",
        **kwargs,
    ):
        captured.update(
            prompt=prompt,
            output_schema=output_schema,
            request_kind=request_kind,
            **kwargs,
        )
        return {
            "results": [
                {
                    "draft_id": "draft-1",
                    "supported": False,
                    "fact_items": {
                        "topic": {"text": "", "evidence_message_ids": []},
                        "content": [],
                        "action_label": {"text": "", "evidence_message_ids": []},
                        "object_hint": {"text": "", "evidence_message_ids": []},
                        "retention_detail": {"text": "", "evidence_message_ids": []},
                    },
                    "removed_claims": ["原候选没有消息证据"],
                }
            ]
        }

    monkeypatch.setattr(analyzer, "_invoke_codex", fake_invoke)

    result = analyzer.review_personal_event_facts(sample_personal_fact_review_batch())

    assert result.results[0].supported is False
    assert "messages 才是事实来源" in str(captured["prompt"])
    assert captured["request_kind"] == "personal_fact_review"
    function_spec = captured["function_spec"]
    assert isinstance(function_spec, FunctionCallSpec)
    assert captured["output_schema"] == function_spec.parameters


def test_codex_analyzer_surfaces_stderr_tail_on_failure(tmp_path: Path) -> None:
    def fake_runner(args, *, cwd=None, timeout=None, input_text=None):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "line1\nline2\nline3\nline4\n"

        return Result()

    analyzer = CodexAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            analyzer_backend="codex",
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    with pytest.raises(AnalyzerProtocolError) as exc_info:
        analyzer.analyze_batch("2026-06-23", sample_batch())

    message = str(exc_info.value)
    assert "returncode=1" in message
    assert "line2 | line3 | line4" in message


def test_codex_analyzer_rejects_oversized_prompt_before_command(tmp_path: Path) -> None:
    calls = []

    def fake_runner(args, *, cwd=None, timeout=None, input_text=None):
        calls.append(args)
        raise AssertionError("command must not run")

    analyzer = CodexAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            analyzer_backend="codex",
            model_input_batch_target_tokens=1,
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    with pytest.raises(AnalyzerProtocolError, match="model_input_batch_target_tokens"):
        analyzer.analyze_batch("2026-06-23", sample_batch())

    assert calls == []


def test_codex_analyzer_allows_marked_indivisible_input(tmp_path: Path) -> None:
    def fake_runner(args, *, cwd=None, timeout=None, input_text=None):
        output_path = Path(args[args.index("-o") + 1])
        output_path.write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    analyzer = CodexAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            analyzer_backend="codex",
            model_input_batch_target_tokens=1,
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    assert analyzer._invoke_codex(
        "oversized",
        request_kind="segment_batch_analysis",
        allow_oversized_input=True,
    ) == {}
    record = analyzer.usage_recorder.records()[0]
    assert record["oversized_singleton"] is True
    assert record["estimated_input_tokens"] > record["input_target_tokens"]


def test_codex_analyzer_counts_output_schema_before_command(tmp_path: Path) -> None:
    prompt = "short prompt"
    output_schema = {
        "type": "object",
        "description": "x" * 600,
        "additionalProperties": False,
    }
    prompt_only_tokens = estimate_model_input_tokens(prompt)
    calls = []

    def fake_runner(args, *, cwd=None, timeout=None, input_text=None):
        calls.append(args)
        raise AssertionError("command must not run")

    analyzer = CodexAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            analyzer_backend="codex",
            codex_stdin_mode=True,
            model_input_batch_target_tokens=prompt_only_tokens,
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    with pytest.raises(ModelInputLimitError, match="model_input_batch_target_tokens"):
        analyzer._invoke_codex(prompt, output_schema=output_schema)

    assert calls == []


def test_codex_request_pacer_reserves_shared_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_values = iter((100.0, 100.0))
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "src.worktrace.analyzers.codex.perf_counter",
        lambda: next(clock_values),
    )
    pacer = CodexRequestPacer(
        1,
        1,
        random_uniform=lambda minimum, maximum: 1,
        sleep_func=lambda seconds: sleep_calls.append(seconds),
    )

    assert pacer.wait_for_turn() == 0
    assert pacer.wait_for_turn() == 1
    assert sleep_calls == [1]
