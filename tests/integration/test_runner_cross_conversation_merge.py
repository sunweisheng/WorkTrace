from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from time import sleep

import pytest

import src.worktrace.runner as runner_module
from src.worktrace.config import RuntimeConfig
from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.errors import PersonalGroupingValidationError
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import (
    CrossConversationGroup,
    CrossConversationGroupResult,
    DayGroupingSummary,
    NormalizedMessage,
    SourceBackedEventDraft,
)
from src.worktrace.pipeline.cross_conversation_merge import (
    materialize_grouped_merged_drafts,
)
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore
from tests.helpers import NullDelivery


def _draft(
    draft_id: str,
    message_id: str,
    *,
    slice_id: str | None = None,
) -> SourceBackedEventDraft:
    return SourceBackedEventDraft(
        draft_id=draft_id,
        date="2026-07-22",
        topic=f"事项 {draft_id}",
        content=f"处理事项 {draft_id}。",
        source_message_ids=[message_id],
        source_conversation_id=f"oc_{draft_id}",
        source_slice_id=slice_id or f"slice-{draft_id}",
        confidence=0.9,
        action_label="确认",
        object_hint=f"对象 {draft_id}",
        retention_reason="decision_made",
        retention_detail=f"形成事项 {draft_id} 的结论。",
    )


def _message(message_id: str) -> NormalizedMessage:
    return NormalizedMessage(
        conversation_id="oc_shared",
        conversation_name="项目群",
        message_id=message_id,
        sender_open_id="ou_self",
        sender_name="本人",
        send_time="2026-07-22T10:00:00+08:00",
        message_type="text",
        text=message_id,
        reply_to_message_id=None,
        quote_message_id=None,
    )


def _singletons(candidates: list[SourceBackedEventDraft]) -> CrossConversationGroupResult:
    return CrossConversationGroupResult(
        groups=[
            CrossConversationGroup(
                group_id=f"model-{index}",
                draft_ids=[candidate.draft_id],
                primary_draft_id=candidate.draft_id,
                merge_reason="单条保留",
            )
            for index, candidate in enumerate(candidates, start=1)
        ]
    )


def _runner(tmp_path: Path, analyzer: object, **config_values: object) -> DailyTraceRunner:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        **config_values,
    )
    return DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=object(),
            content_resolver=object(),
            analyzer=analyzer,
            delivery_channel=NullDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )


class QualityRetryAnalyzer:
    def __init__(
        self,
        online_results: list[CrossConversationGroupResult],
        codex_result: CrossConversationGroupResult | Exception,
    ) -> None:
        self.online_results = list(online_results)
        self.codex_result = codex_result
        self.validation_feedback: list[str] = []
        self.fallback_calls = 0

    def merge_day_candidates(
        self,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
        *,
        validation_feedback: str = "",
    ) -> CrossConversationGroupResult:
        self.validation_feedback.append(validation_feedback)
        return self.online_results.pop(0)

    def last_request_used_fallback(self) -> bool:
        return False

    def fallback_current_request(self, method_name: str, *args, **kwargs):
        self.fallback_calls += 1
        if isinstance(self.codex_result, Exception):
            raise self.codex_result
        return self.codex_result


class ParsingRetryAnalyzer(QualityRetryAnalyzer):
    def __init__(self, valid_result: CrossConversationGroupResult) -> None:
        super().__init__([], valid_result)
        self.calls = 0

    def merge_day_candidates(
        self,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
        *,
        validation_feedback: str = "",
    ) -> CrossConversationGroupResult:
        self.validation_feedback.append(validation_feedback)
        self.calls += 1
        if self.calls == 1:
            raise PersonalGroupingValidationError("missing_member_connection")
        return self.codex_result  # type: ignore[return-value]


def test_day_grouping_retries_personal_contract_parse_error(tmp_path: Path) -> None:
    candidates = [_draft("d1", "m1"), _draft("d2", "m2")]
    analyzer = ParsingRetryAnalyzer(_singletons(candidates))
    runner = _runner(tmp_path, analyzer)

    result, warnings, attempts, retry_count, codex_count, repair_count = (
        runner._request_valid_day_groups(
            "2026-07-22",
            candidates,
            request_label="full-day",
        )
    )

    assert [group.draft_ids for group in result.groups] == [["d1"], ["d2"]]
    assert analyzer.validation_feedback == ["", "missing_member_connection"]
    assert [item["status"] for item in attempts] == ["invalid", "success"]
    assert warnings == []
    assert (retry_count, codex_count, repair_count) == (1, 0, 0)


def test_day_grouping_retries_online_quality_once_then_uses_codex(
    tmp_path: Path,
) -> None:
    candidates = [_draft("d1", "m1"), _draft("d2", "m2")]
    analyzer = QualityRetryAnalyzer(
        [CrossConversationGroupResult(), CrossConversationGroupResult()],
        _singletons(candidates),
    )
    runner = _runner(tmp_path, analyzer)

    result, warnings, attempts, retry_count, codex_count, repair_count = (
        runner._request_valid_day_groups(
            "2026-07-22",
            candidates,
            request_label="full-day",
        )
    )

    assert [group.draft_ids for group in result.groups] == [["d1"], ["d2"]]
    assert analyzer.validation_feedback[0] == ""
    assert "missing=['d1', 'd2']" in analyzer.validation_feedback[1]
    assert analyzer.fallback_calls == 1
    assert [item["backend"] for item in attempts] == ["online", "online", "codex"]
    assert warnings == []
    assert (retry_count, codex_count, repair_count) == (1, 1, 0)


def test_invalid_codex_result_keeps_legal_groups_and_repairs_rest_as_singletons(
    tmp_path: Path,
) -> None:
    candidates = [_draft("d1", "m1"), _draft("d2", "m2"), _draft("d3", "m3")]
    legal_partial_group = CrossConversationGroupResult(
        groups=[
            CrossConversationGroup(
                group_id="ignored",
                draft_ids=["d1", "d2"],
                primary_draft_id="d1",
                merge_reason="同一事项的方案与执行反馈。",
                evidence_message_ids=["m1", "m2"],
            )
        ]
    )
    analyzer = QualityRetryAnalyzer(
        [CrossConversationGroupResult(), CrossConversationGroupResult()],
        legal_partial_group,
    )
    runner = _runner(tmp_path, analyzer)

    result, warnings, attempts, retry_count, codex_count, repair_count = (
        runner._request_valid_day_groups(
            "2026-07-22",
            candidates,
            request_label="full-day",
        )
    )

    assert [group.draft_ids for group in result.groups] == [["d1", "d2"], ["d3"]]
    assert attempts[-1]["backend"] == "python"
    assert attempts[-1]["status"] == "repaired"
    assert warnings and "singleton_candidates=['d3']" in warnings[0]
    assert (retry_count, codex_count, repair_count) == (1, 1, 1)


def test_codex_technical_failure_stops_day_grouping(tmp_path: Path) -> None:
    candidates = [_draft("d1", "m1"), _draft("d2", "m2")]
    analyzer = QualityRetryAnalyzer(
        [CrossConversationGroupResult(), CrossConversationGroupResult()],
        AnalyzerProtocolError("Codex unavailable"),
    )
    runner = _runner(tmp_path, analyzer)

    with pytest.raises(AnalyzerProtocolError, match="Codex unavailable"):
        runner._request_valid_day_groups(
            "2026-07-22",
            candidates,
            request_label="full-day",
        )


class ConcurrentReviewAnalyzer:
    def __init__(self, *, split_existing_groups: bool = False) -> None:
        self.split_existing_groups = split_existing_groups
        self.lock = Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    def request_function(self, prompt: str, *, function_spec, allow_oversized_input=False):
        payload = json.loads(prompt)
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls += 1
        sleep(0.03)
        with self.lock:
            self.active -= 1
        candidates = payload["candidates"]
        if self.split_existing_groups:
            return {
                "groups": [
                    {
                        "draft_ids": [item["draft_id"]],
                        "primary_draft_id": item["draft_id"],
                        "merge_reason": "单条保留",
                        "evidence_message_ids": [],
                    }
                    for item in candidates
                ]
            }
        return {
            "groups": [
                {
                    "draft_ids": list(group["draft_ids"]),
                    "primary_draft_id": group["primary_draft_id"],
                    "merge_reason": group["merge_reason"],
                    "evidence_message_ids": list(group["evidence_message_ids"]),
                }
                for group in payload["existing_groups"]
            ]
        }

    def last_request_used_fallback(self) -> bool:
        return False


def test_independent_day_group_reviews_run_with_configured_parallel_limit(
    tmp_path: Path,
) -> None:
    candidates = [
        _draft("d1", "m1", slice_id="slice-a"),
        _draft("d2", "m2", slice_id="slice-a"),
        _draft("d3", "m3", slice_id="slice-b"),
        _draft("d4", "m4", slice_id="slice-b"),
    ]
    groups = _singletons(candidates).groups
    analyzer = ConcurrentReviewAnalyzer()
    runner = _runner(
        tmp_path,
        analyzer,
        max_concurrent_day_group_review_requests=2,
    )

    result = runner._review_strongly_related_day_groups(
        target_date="2026-07-22",
        groups=groups,
        candidates=candidates,
        messages=[_message(f"m{index}") for index in range(1, 5)],
    )

    reviewed, warnings, attempts, component_count, request_count, retries, codex = result
    assert [group.draft_ids for group in reviewed] == [["d1"], ["d2"], ["d3"], ["d4"]]
    assert analyzer.max_active == 2
    assert (component_count, request_count, retries, codex) == (2, 2, 0, 0)
    assert warnings == []
    assert len(attempts) == 2


def test_invalid_local_review_keeps_previous_groups_with_warning(tmp_path: Path) -> None:
    candidates = [
        _draft("d1", "m1", slice_id="slice-a"),
        _draft("d2", "m2", slice_id="slice-a"),
        _draft("d3", "m3", slice_id="slice-a"),
    ]
    groups = [
        CrossConversationGroup(
            group_id="group-001",
            draft_ids=["d1", "d2"],
            primary_draft_id="d1",
            merge_reason="同一事项的连续动作。",
            evidence_message_ids=["m1"],
        ),
        CrossConversationGroup(
            group_id="group-002",
            draft_ids=["d3"],
            primary_draft_id="d3",
            merge_reason="单条保留",
        ),
    ]
    analyzer = ConcurrentReviewAnalyzer(split_existing_groups=True)
    runner = _runner(tmp_path, analyzer, day_group_validation_retry_limit=1)

    reviewed, warnings, attempts, components, requests, retries, codex = (
        runner._review_strongly_related_day_groups(
            target_date="2026-07-22",
            groups=groups,
            candidates=candidates,
            messages=[_message("m1"), _message("m2"), _message("m3")],
        )
    )

    assert [group.draft_ids for group in reviewed] == [["d1", "d2"], ["d3"]]
    assert warnings and "Kept existing day groups" in warnings[0]
    assert (components, requests, retries, codex) == (1, 2, 1, 0)
    assert all(item["status"] == "failed" for item in attempts)


def test_materialization_uses_primary_candidate_without_workstream_metadata() -> None:
    candidates = [_draft("d1", "m1"), _draft("d2", "m2")]
    groups = [
        CrossConversationGroup(
            group_id="group-001",
            draft_ids=["d1", "d2"],
            primary_draft_id="d2",
            merge_reason="同一事项的连续动作。",
            evidence_message_ids=["m1", "m2"],
        )
    ]

    drafts = materialize_grouped_merged_drafts(
        candidates,
        groups,
        target_date="2026-07-22",
        message_order=["m1", "m2"],
    )

    assert drafts[0].topic == "事项 d2"
    assert "workstream" not in drafts[0].to_dict()


def test_cross_batch_summary_keeps_existing_multi_event_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [_draft("d1", "m1"), _draft("d2", "m2")]
    source_group = CrossConversationGroup(
        group_id="local-001",
        draft_ids=["d1", "d2"],
        primary_draft_id="d1",
        merge_reason="同一事项的方案和执行反馈。",
        evidence_message_ids=["m1", "m2"],
    )
    analyzer = QualityRetryAnalyzer(
        [CrossConversationGroupResult(groups=[source_group])],
        AnalyzerProtocolError("Codex should not be called"),
    )
    runner = _runner(tmp_path, analyzer, model_input_batch_target_tokens=1)
    monkeypatch.setattr(
        runner_module,
        "_estimate_day_merge_input_tokens",
        lambda *args, **kwargs: 2,
    )
    monkeypatch.setattr(
        runner_module,
        "_pack_day_merge_candidates",
        lambda **kwargs: [kwargs["candidates"]],
    )

    result, warnings, attempts, retries, codex, repairs = (
        runner._merge_day_candidates_with_batching("2026-07-22", candidates)
    )

    assert [group.draft_ids for group in result.groups] == [["d1", "d2"]]
    assert result.groups[0].merge_reason == source_group.merge_reason
    assert result.groups[0].evidence_message_ids == ["m1", "m2"]
    assert warnings == []
    assert len(attempts) == 1
    assert (retries, codex, repairs) == (0, 0, 0)


class DebugAnalyzer:
    def build_merge_prompt(self, target_date, candidates):
        return json.dumps(
            {"target_date": target_date, "draft_ids": [item.draft_id for item in candidates]},
            ensure_ascii=False,
        )


def test_new_day_grouping_trace_contains_only_new_artifacts(tmp_path: Path) -> None:
    candidate = _draft("d1", "m1")
    runner = _runner(
        tmp_path,
        DebugAnalyzer(),
        conversation_debug_root=tmp_path / "debug",
    )
    summary = DayGroupingSummary(candidate_count=1, initial_group_count=1, final_group_count=1)

    runner._dump_merge_debug_artifacts(
        target_date="2026-07-22",
        candidates=[candidate],
        grouping_attempts=[],
        review_attempts=[],
        groups=_singletons([candidate]).groups,
        warnings=[],
        summary=summary,
    )

    directory = tmp_path / "debug" / "2026-07-22" / "_merge_day_candidates"
    assert {path.name for path in directory.iterdir()} == {
        "input.json",
        "prompt.txt",
        "grouping_attempts.json",
        "day_group_review.json",
        "resolved_groups.json",
    }
    assert "workstream" not in "\n".join(
        path.read_text(encoding="utf-8") for path in directory.iterdir()
    ).lower()
