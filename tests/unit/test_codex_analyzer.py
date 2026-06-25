from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.worktrace.analyzers.codex import CodexAnalyzer
from src.worktrace.config import RuntimeConfig
from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.models import (
    AnalysisBatch,
    BucketMergedDraft,
    ConversationSlice,
    MergedEventDraft,
    NormalizedMessage,
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


def test_codex_analyzer_parses_cross_merge_bucket_result(tmp_path: Path) -> None:
    def fake_runner(args, *, cwd=None, timeout=None, input_text=None):
        output_index = args.index("-o") + 1
        output_path = Path(args[output_index])
        output_path.write_text(
            json.dumps(
                {
                    "buckets": [
                        {
                            "bucket_id": "b1",
                            "draft_ids": ["d1"],
                            "reason": "same event",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    analyzer = CodexAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            analyzer_backend="codex",
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    result = analyzer.bucket_cross_merge_candidates(
        "2026-06-23",
        [
            SourceBackedEventDraft(
                draft_id="d1",
                date="2026-06-23",
                topic="发布推进",
                content="同步",
                result="",
                source_message_ids=["om_1"],
                source_conversation_id="oc_1",
                source_slice_id="slice-1",
                confidence=0.9,
            )
        ],
    )

    assert [bucket.bucket_id for bucket in result.buckets] == ["b1"]


def test_codex_analyzer_parses_cross_bucket_merge_result(tmp_path: Path) -> None:
    def fake_runner(args, *, cwd=None, timeout=None, input_text=None):
        output_index = args.index("-o") + 1
        output_path = Path(args[output_index])
        output_path.write_text(
            json.dumps(
                {
                    "merge_decisions": [
                        {
                            "left_bucket_id": "b1",
                            "right_bucket_id": "b2",
                            "should_merge": True,
                            "reason": "same thread",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    analyzer = CodexAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            analyzer_backend="codex",
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    result = analyzer.decide_cross_bucket_merges(
        "2026-06-23",
        [
            BucketMergedDraft(
                bucket_id="b1",
                draft=MergedEventDraft(
                    date="2026-06-23",
                    topic="主题1",
                    content="内容1",
                    result="",
                    source_message_ids=["m1"],
                    source_conversation_ids=["c1"],
                ),
                upstream_draft_ids=["d1"],
            ),
            BucketMergedDraft(
                bucket_id="b2",
                draft=MergedEventDraft(
                    date="2026-06-23",
                    topic="主题2",
                    content="内容2",
                    result="",
                    source_message_ids=["m2"],
                    source_conversation_ids=["c2"],
                ),
                upstream_draft_ids=["d2"],
            ),
        ],
        [("b1", "b2")],
    )

    assert result.merge_decisions[0].should_merge is True
