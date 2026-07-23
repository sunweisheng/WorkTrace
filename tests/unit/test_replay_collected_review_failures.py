from __future__ import annotations

from scripts.replay_collected_review_failures import (
    evaluate_review_result,
    evaluate_split_reason_compatibility,
)
from src.worktrace.config import RuntimeConfig
from src.worktrace.models import (
    CollectedGroupingGroup,
    CollectedGroupingResult,
    CollectedSourceEvent,
    WorkEvent,
)


def _source(draft_id: str, file_key: str) -> CollectedSourceEvent:
    return CollectedSourceEvent(
        draft_id=draft_id,
        person_name=draft_id,
        source_file=f"{draft_id}.md",
        event=WorkEvent(
            event_id=draft_id,
            date="2026-07-20",
            title=f"{draft_id} 工作记录",
            content=f"提交 {draft_id} 工作记录。",
            object_hint="同批工作记录",
            conversation_fingerprints=["conversation-1"],
            file_keys=[file_key],
        ),
    )


def test_offline_review_accepts_one_legacy_split_reason() -> None:
    events = [_source("d1", "file-1"), _source("d2", "file-2")]
    result = CollectedGroupingResult(
        groups=[
            CollectedGroupingGroup(
                "g1",
                ["d1"],
                split_reason="两组处理不同业务对象。",
            ),
            CollectedGroupingGroup("g2", ["d2"]),
        ]
    )

    evaluation = evaluate_review_result(
        result=result,
        source_events=events,
        candidate_group=CollectedGroupingGroup("candidate", ["d1", "d2"]),
        review_reasons=["same_conversation_only"],
        config=RuntimeConfig(),
    )

    assert evaluation["valid"] is True
    assert evaluation["split_reason"] == "两组处理不同业务对象。"

    compatibility = evaluate_split_reason_compatibility(
        result=result,
        source_events=events,
        candidate_group=CollectedGroupingGroup("candidate", ["d1", "d2"]),
        review_reasons=["same_conversation_only"],
        config=RuntimeConfig(),
    )
    assert compatibility == {
        "tested": True,
        "top_level": {"accepted": True, "source": "top_level"},
        "legacy_group": {"accepted": True, "source": "legacy_group"},
    }


def test_offline_review_rejects_false_shared_file_and_accepts_batch_reason() -> None:
    events = [_source("d1", "file-1"), _source("d2", "file-2")]
    candidate = CollectedGroupingGroup("candidate", ["d1", "d2"])
    common = {
        "source_events": events,
        "candidate_group": candidate,
        "review_reasons": ["same_conversation_only"],
        "config": RuntimeConfig(),
    }

    invalid = evaluate_review_result(
        result=CollectedGroupingResult(
            groups=[
                CollectedGroupingGroup(
                    "reports",
                    ["d1", "d2"],
                    summary_title="同批工作记录",
                    summary_content="两人分别提交工作记录。",
                    summary_object_hint="同批工作记录",
                    group_reason=["shared_file"],
                )
            ]
        ),
        **common,
    )
    valid = evaluate_review_result(
        result=CollectedGroupingResult(
            groups=[
                CollectedGroupingGroup(
                    "reports",
                    ["d1", "d2"],
                    summary_title="同批工作记录",
                    summary_content="两人分别提交工作记录。",
                    summary_object_hint="同批工作记录",
                    group_reason=["same_deliverable_batch"],
                )
            ]
        ),
        **common,
    )

    assert invalid["valid"] is False
    assert "shared_file" in invalid["errors"][0]
    assert valid["valid"] is True
