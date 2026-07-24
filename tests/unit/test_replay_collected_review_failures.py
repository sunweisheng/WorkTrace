from __future__ import annotations

import json
from pathlib import Path

from scripts.replay_collected_review_failures import (
    build_markdown,
    build_summary,
    evaluate_review_result,
    evaluate_split_reason_compatibility,
    replay_trace_payload,
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


def test_trace_replay_separates_legacy_and_current_protocol(tmp_path: Path) -> None:
    events = [_source("d1", "file-1"), _source("d2", "file-2")]
    trace_path = tmp_path / "step-001.json"
    trace_path.write_text(
        json.dumps(
            {
                "step_index": 1,
                "stage": "candidate_grouping_batch_1",
                "input_events": [item.to_dict() for item in events],
                "deterministic_groups": [],
                "raw_result": {
                    "groups": [
                        {
                            "group_id": "g1",
                            "draft_ids": ["d1", "d2"],
                            "semantic_reasons": ["same_object"],
                            "group_reason": ["same_object"],
                            "reason_detail": "两条记录处理同一对象。",
                            "evidence_relation_ids": ["MSG-999"],
                        }
                    ],
                    "validation_errors": [],
                },
                "python_validation": {"valid": True, "errors": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    legacy, _prompt, _function = replay_trace_payload(
        case_id="1",
        trace_path=trace_path,
        target_date="2026-07-20",
        failure_types=[],
        config=RuntimeConfig(),
        result_dir=None,
    )

    assert legacy["mode"] == "legacy_audit"
    assert legacy["model_declared_evidence_relation_ids"] == [
        {"group_id": "g1", "relation_ids": ["MSG-999"]}
    ]
    assert legacy["issue_counts"]["member_connection_error"] == 1

    result_dir = tmp_path / "results"
    result_dir.mkdir()
    (result_dir / "step-001.json").write_text(
        json.dumps(
            {
                "merged_groups": [
                    {
                        "group_id": "g1",
                        "draft_ids": ["d1", "d2"],
                        "summary_title": "同一事项",
                        "summary_content": "两条记录处理同一事项。",
                        "summary_object_hint": "同一事项",
                        "semantic_reasons": ["same_object"],
                        "reason_detail": "两条记录处理同一对象。",
                        "member_connections": [
                            {
                                "draft_id": "d1",
                                "connection_detail": "处理该对象。",
                            }
                        ],
                        "risk_flags": [],
                    }
                ],
                "singleton_draft_ids": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    current, _prompt, _function = replay_trace_payload(
        case_id="1",
        trace_path=trace_path,
        target_date="2026-07-20",
        failure_types=[],
        config=RuntimeConfig(),
        result_dir=result_dir,
    )

    assert current["mode"] == "current"
    assert current["valid"] is False
    assert any(
        error.startswith("missing_member_connection")
        for error in current["errors"]
    )
    assert current["model_declared_evidence_relation_ids"] == []


def test_replay_summary_counts_issues_by_stage() -> None:
    results = [
        {
            "id": "13",
            "stage": "candidate_grouping_batch_11",
            "stage_group": "candidate_grouping",
            "mode": "legacy_audit",
            "valid": False,
            "errors": ["duplicate_draft_id draft_ids=['d1']"],
            "original_errors": ["duplicate_draft_id draft_ids=['d1']"],
            "group_count": 1,
            "issue_counts": {"duplicate_draft_id": 1},
            "review_trigger_reasons": ["broad_object"],
            "new_rule_handling": "拒绝重复编号",
            "needs_model_review": True,
        },
        {
            "id": "34",
            "stage": "high_risk_review",
            "stage_group": "high_risk_review",
            "mode": "legacy_audit",
            "valid": False,
            "errors": ["merged_group_too_small"],
            "original_errors": ["merged_group_too_small"],
            "group_count": 1,
            "issue_counts": {"single_member_merge": 1},
            "review_trigger_reasons": [],
            "new_rule_handling": "拒绝单成员合并组",
            "needs_model_review": True,
        },
    ]

    summary = build_summary(results)
    markdown = build_markdown(summary)

    assert summary["model_call_count"] == 0
    assert summary["by_stage"]["candidate_grouping"]["issue_counts"] == {
        "duplicate_draft_id": 1
    }
    assert summary["by_stage"]["high_risk_review"]["issue_counts"] == {
        "single_member_merge": 1
    }
    assert "旧结果问题" in markdown
    assert "新规则处理" in markdown
    assert "是否仍需模型复核" in markdown
