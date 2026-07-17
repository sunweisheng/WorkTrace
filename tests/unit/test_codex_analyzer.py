from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.worktrace.analyzers.codex import CodexAnalyzer
from src.worktrace.analyzers.output_schemas import (
    personal_fact_review_output_schema,
    retention_review_output_schema,
)
from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.errors import AnalyzerProtocolError
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

    def fake_invoke(prompt, *, output_schema):
        captured.update(prompt=prompt, output_schema=output_schema)
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

    result = analyzer.review_retention_candidates(sample_retention_review_batch())

    assert result.results[0].draft_id == "draft-1"
    assert "不要决定保留或删除" in str(captured["prompt"])
    assert captured["output_schema"] == retention_review_output_schema(config)


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

    def fake_invoke(prompt, *, output_schema):
        captured.update(prompt=prompt, output_schema=output_schema)
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
                        "workstream_key": {"text": "", "evidence_message_ids": []},
                    },
                    "removed_claims": ["原候选没有消息证据"],
                }
            ]
        }

    monkeypatch.setattr(analyzer, "_invoke_codex", fake_invoke)

    result = analyzer.review_personal_event_facts(sample_personal_fact_review_batch())

    assert result.results[0].supported is False
    assert "messages 才是事实来源" in str(captured["prompt"])
    assert captured["output_schema"] == personal_fact_review_output_schema(
        sample_personal_fact_review_batch()
    )


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
            max_model_input_tokens=1,
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    with pytest.raises(AnalyzerProtocolError, match="max_model_input_tokens"):
        analyzer.analyze_batch("2026-06-23", sample_batch())

    assert calls == []
