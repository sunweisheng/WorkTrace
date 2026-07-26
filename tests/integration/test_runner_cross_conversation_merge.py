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
        groups = (
            [[item["draft_id"]] for item in candidates]
            if self.split_existing_groups
            else [list(group["draft_ids"]) for group in payload["existing_groups"]]
        )
        return _review_payload(payload, groups=groups)

    def last_request_used_fallback(self) -> bool:
        return False


class ReviewValidationFallbackAnalyzer:
    def __init__(self) -> None:
        self.prompts: list[dict[str, object]] = []
        self.fallback_prompts: list[dict[str, object]] = []

    def request_function(self, prompt: str, *, function_spec, allow_oversized_input=False):
        payload = json.loads(prompt)
        self.prompts.append(payload)
        return {
            "merged_groups": [],
            "singleton_draft_ids": [
                item["draft_id"] for item in payload["candidates"]
            ],
            "relation_resolutions": [],
        }

    def fallback_current_request(self, method_name: str, prompt: str, **kwargs):
        assert method_name == "request_function"
        payload = json.loads(prompt)
        self.fallback_prompts.append(payload)
        return _review_payload(
            payload,
            groups=[
                list(group["draft_ids"])
                for group in payload["existing_groups"]
            ],
        )

    def last_request_used_fallback(self) -> bool:
        return False


class ReviewTechnicalFailureAnalyzer:
    def __init__(self) -> None:
        self.calls = 0

    def request_function(self, prompt: str, *, function_spec, allow_oversized_input=False):
        self.calls += 1
        raise AnalyzerProtocolError("all request routes failed")

    def last_request_used_fallback(self) -> bool:
        return True


def _review_payload(
    prompt_payload: dict[str, object],
    *,
    groups: list[list[str]],
) -> dict[str, object]:
    candidate_by_id = {
        str(item["draft_id"]): item
        for item in prompt_payload["candidates"]  # type: ignore[index]
    }
    returned_group_by_draft = {
        draft_id: index
        for index, draft_ids in enumerate(groups)
        for draft_id in draft_ids
    }
    merged_groups = []
    singleton_draft_ids = []
    for draft_ids in groups:
        if len(draft_ids) == 1:
            singleton_draft_ids.extend(draft_ids)
            continue
        merged_groups.append(
            {
                "draft_ids": draft_ids,
                "primary_draft_id": draft_ids[0],
                "common_object": "共同事项",
                "semantic_reasons": ["continuous_action"],
                "reason_detail": "成员直接参与同一事项的连续过程。",
                "member_connections": [
                    {
                        "draft_id": draft_id,
                        "connection_detail": "直接参与共同过程。",
                        "evidence_message_ids": list(
                            candidate_by_id[draft_id]["source_message_ids"]
                        ),
                    }
                    for draft_id in draft_ids
                ],
            }
        )
    relation_resolutions = []
    for relation in prompt_payload["strong_relations"]:  # type: ignore[index]
        structural_ids = [
            str(relation.get(field, ""))
            for field in ("left_draft_id", "right_draft_id")
            if str(relation.get(field, ""))
        ]
        if structural_ids:
            joined = len(
                {returned_group_by_draft[draft_id] for draft_id in structural_ids}
            ) == 1
            connected_ids = structural_ids if joined else []
            evidence_draft_ids = structural_ids
        else:
            original_groups = {
                str(group["group_id"]): list(group["draft_ids"])
                for group in prompt_payload["existing_groups"]  # type: ignore[index]
            }
            relation_group_ids = [str(value) for value in relation.get("group_ids", [])]
            connected_ids = []
            for left_index, left_group_id in enumerate(relation_group_ids):
                for right_group_id in relation_group_ids[left_index + 1 :]:
                    pair = next(
                        (
                            [left_draft_id, right_draft_id]
                            for left_draft_id in original_groups[left_group_id]
                            for right_draft_id in original_groups[right_group_id]
                            if returned_group_by_draft[left_draft_id]
                            == returned_group_by_draft[right_draft_id]
                        ),
                        [],
                    )
                    if pair:
                        connected_ids = pair
                        break
                if connected_ids:
                    break
            joined = bool(connected_ids)
            evidence_draft_ids = (
                connected_ids
                if joined
                else [
                    original_groups[group_id][0]
                    for group_id in relation_group_ids
                ]
            )
        relation_resolutions.append(
            {
                "relation_id": relation["relation_id"],
                "decision": "merged" if joined else "separate",
                "connected_draft_ids": connected_ids,
                "reason": (
                    "相关成员进入同一最终组。"
                    if joined
                    else "证据显示各成员处理的是不同事项。"
                ),
                "evidence_message_ids": [
                    candidate_by_id[draft_id]["source_message_ids"][0]
                    for draft_id in evidence_draft_ids
                ],
            }
        )
    return {
        "merged_groups": merged_groups,
        "singleton_draft_ids": singleton_draft_ids,
        "relation_resolutions": relation_resolutions,
    }


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

    (
        reviewed,
        warnings,
        attempts,
        component_records,
        component_count,
        request_count,
        retries,
        codex,
        metrics,
    ) = result
    assert [group.draft_ids for group in reviewed] == [["d1"], ["d2"], ["d3"], ["d4"]]
    assert analyzer.max_active == 2
    assert (component_count, request_count, retries, codex) == (2, 2, 0, 0)
    assert warnings == []
    assert len(attempts) == 2
    assert len(component_records) == 2
    assert metrics["relation_separate_count"] == 2


def test_local_review_can_split_existing_groups(tmp_path: Path) -> None:
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

    reviewed, warnings, attempts, component_records, components, requests, retries, codex, metrics = (
        runner._review_strongly_related_day_groups(
            target_date="2026-07-22",
            groups=groups,
            candidates=candidates,
            messages=[_message("m1"), _message("m2"), _message("m3")],
        )
    )

    assert [group.draft_ids for group in reviewed] == [["d1"], ["d2"], ["d3"]]
    assert warnings == []
    assert (components, requests, retries, codex) == (1, 1, 0, 0)
    assert all(item["status"] == "success" for item in attempts)
    assert component_records[0]["relation_sources"] == ["structural_relation"]
    assert metrics["split_group_count"] == 1


def test_day_group_review_validation_feedback_reaches_retry_and_codex(
    tmp_path: Path,
) -> None:
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
    analyzer = ReviewValidationFallbackAnalyzer()
    runner = _runner(tmp_path, analyzer, day_group_validation_retry_limit=1)

    reviewed, warnings, attempts, _records, components, requests, retries, codex, metrics = (
        runner._review_strongly_related_day_groups(
            target_date="2026-07-22",
            groups=groups,
            candidates=candidates,
            messages=[_message("m1"), _message("m2"), _message("m3")],
        )
    )

    assert [group.draft_ids for group in reviewed] == [["d1", "d2"], ["d3"]]
    assert warnings == []
    assert (components, requests, retries, codex) == (1, 3, 1, 1)
    assert [item["status"] for item in attempts] == ["failed", "failed", "success"]
    assert all(item["failure_kind"] == "validation" for item in attempts[:2])
    assert "missing_relations" in analyzer.prompts[1]["validation_feedback"]
    assert "missing_relations" in analyzer.fallback_prompts[0]["validation_feedback"]
    assert "required_draft_ids=['d1', 'd2', 'd3']" in analyzer.prompts[1][
        "validation_feedback"
    ]
    assert attempts[0]["raw_function_payload"]["relation_resolutions"] == []
    assert metrics["review_failure_count"] == 0


def test_day_group_review_request_failure_does_not_consume_validation_retry(
    tmp_path: Path,
) -> None:
    candidates = [
        _draft("d1", "m1", slice_id="slice-a"),
        _draft("d2", "m2", slice_id="slice-a"),
    ]
    analyzer = ReviewTechnicalFailureAnalyzer()
    runner = _runner(tmp_path, analyzer, day_group_validation_retry_limit=1)

    reviewed, warnings, attempts, _records, components, requests, retries, codex, metrics = (
        runner._review_strongly_related_day_groups(
            target_date="2026-07-22",
            groups=_singletons(candidates).groups,
            candidates=candidates,
            messages=[_message("m1"), _message("m2")],
        )
    )

    assert [group.draft_ids for group in reviewed] == [["d1"], ["d2"]]
    assert warnings and "all request routes failed" in warnings[0]
    assert analyzer.calls == 1
    assert (components, requests, retries, codex) == (1, 1, 0, 1)
    assert attempts[0]["failure_kind"] == "request"
    assert metrics["review_failure_count"] == 1


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


def test_personal_group_render_rewrites_locked_multi_group_content(
    tmp_path: Path,
) -> None:
    class RenderAnalyzer:
        def __init__(self) -> None:
            self.calls = 0

        def request_function(self, prompt, *, function_spec, allow_oversized_input=False):
            self.calls += 1
            assert function_spec.request_kind == "personal_group_render"
            return {
                "groups": [
                    {
                        "group_id": "group-001",
                        "covered_draft_ids": ["d1", "d2"],
                        "fact_items": [
                            {
                                "field": "topic",
                                "text": "事项需求推进与成果交付",
                                "evidence_message_ids": ["m1", "m2"],
                            },
                            {
                                "field": "content",
                                "text": "先确认事项需求。",
                                "evidence_message_ids": ["m1"],
                            },
                            {
                                "field": "content",
                                "text": "随后完成成果交付。",
                                "evidence_message_ids": ["m2"],
                            },
                            {
                                "field": "object_hint",
                                "text": "事项需求及交付成果",
                                "evidence_message_ids": ["m1", "m2"],
                            },
                        ],
                    }
                ]
            }

    candidates = [_draft("d1", "m1"), _draft("d2", "m2")]
    group = CrossConversationGroup(
        group_id="group-001",
        draft_ids=["d1", "d2"],
        primary_draft_id="d1",
        merge_reason="两个候选直接参与同一完整过程。",
        evidence_message_ids=["m1", "m2"],
    )
    analyzer = RenderAnalyzer()
    runner = _runner(tmp_path, analyzer)

    outcome = runner._render_personal_multi_groups(
        target_date="2026-07-22",
        groups=[group],
        candidates=candidates,
    )
    drafts = materialize_grouped_merged_drafts(
        candidates,
        [group],
        target_date="2026-07-22",
        message_order=["m1", "m2"],
        rendered_groups=outcome.rendered_groups,
    )

    assert analyzer.calls == 1
    assert outcome.failure_count == 0
    assert drafts[0].topic == "事项需求推进与成果交付"
    assert drafts[0].content == "先确认事项需求。随后完成成果交付。"
    assert drafts[0].object_hint == "事项需求及交付成果"
    assert drafts[0].retention_reason == candidates[0].retention_reason


def test_personal_group_render_failure_uses_deterministic_content(
    tmp_path: Path,
) -> None:
    class InvalidRenderAnalyzer:
        def __init__(self) -> None:
            self.calls = 0

        def request_function(self, prompt, *, function_spec, allow_oversized_input=False):
            self.calls += 1
            return {
                "groups": [
                    {
                        "group_id": "group-001",
                        "covered_draft_ids": ["d1", "d2"],
                        "fact_items": [
                            {
                                "field": "topic",
                                "text": "不完整标题",
                                "evidence_message_ids": ["m1"],
                            },
                            {
                                "field": "content",
                                "text": "只覆盖第一个候选。",
                                "evidence_message_ids": ["m1"],
                            },
                            {
                                "field": "object_hint",
                                "text": "不完整对象",
                                "evidence_message_ids": ["m1"],
                            },
                        ],
                    }
                ]
            }

    candidates = [_draft("d1", "m1"), _draft("d2", "m2")]
    group = CrossConversationGroup(
        group_id="group-001",
        draft_ids=["d1", "d2"],
        primary_draft_id="d1",
        merge_reason="两个候选直接参与同一完整过程。",
        evidence_message_ids=["m1", "m2"],
    )
    analyzer = InvalidRenderAnalyzer()
    runner = _runner(tmp_path, analyzer, day_group_validation_retry_limit=1)

    outcome = runner._render_personal_multi_groups(
        target_date="2026-07-22",
        groups=[group],
        candidates=candidates,
    )
    drafts = materialize_grouped_merged_drafts(
        candidates,
        [group],
        target_date="2026-07-22",
        message_order=["m1", "m2"],
        rendered_groups=outcome.rendered_groups,
    )

    assert analyzer.calls == 2
    assert outcome.failure_count == 1
    assert outcome.rendered_groups == {}
    assert drafts[0].topic == candidates[0].topic
    assert drafts[0].content == "\n\n".join(
        candidate.content for candidate in candidates
    )
    assert any("Kept deterministic personal group content" in item for item in outcome.warnings)


def test_cross_batch_results_keep_existing_multi_event_evidence_without_summary_request(
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
    assert len(analyzer.validation_feedback) == 1
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
        discovery_artifact={
            "status": "success",
            "input": {"groups": []},
            "attempts": [],
            "result": {"candidate_groups": [], "group_checks": []},
        },
        review_attempts=[],
            review_components=[],
            render_artifact={"status": "not_needed", "groups": [], "attempts": []},
            groups=_singletons([candidate]).groups,
        warnings=[],
        summary=summary,
    )

    directory = tmp_path / "debug" / "2026-07-22" / "_merge_day_candidates"
    assert {path.name for path in directory.iterdir()} == {
        "input.json",
        "prompt.txt",
        "grouping_attempts.json",
        "day_group_discovery.json",
        "day_group_review.json",
        "personal_group_render.json",
        "resolved_groups.json",
    }
    assert "workstream" not in "\n".join(
        path.read_text(encoding="utf-8") for path in directory.iterdir()
    ).lower()


class DiscoveryAnalyzer:
    def __init__(
        self,
        online_payloads: list[object],
        codex_payload: object | Exception | None = None,
    ) -> None:
        self.online_payloads = list(online_payloads)
        self.codex_payload = codex_payload
        self.prompts: list[dict[str, object]] = []
        self.allow_oversized_values: list[bool] = []
        self.fallback_calls = 0

    def request_function(
        self,
        prompt: str,
        *,
        function_spec,
        allow_oversized_input: bool = False,
    ):
        assert function_spec.request_kind == "day_group_discovery"
        self.prompts.append(json.loads(prompt))
        self.allow_oversized_values.append(allow_oversized_input)
        payload = self.online_payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload

    def last_request_used_fallback(self) -> bool:
        return False

    def fallback_current_request(self, method_name: str, *args, **kwargs):
        assert method_name == "request_function"
        self.fallback_calls += 1
        if isinstance(self.codex_payload, Exception):
            raise self.codex_payload
        return self.codex_payload


def test_day_group_discovery_submits_only_ids_and_titles_even_when_oversized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [_draft("d1", "m1"), _draft("d2", "m2"), _draft("d3", "m3")]
    analyzer = DiscoveryAnalyzer(
        [
            {
                "group_checks": [
                    {
                        "group_id": "model-1",
                        "related_group_ids": ["model-2", "model-3"],
                        "reason": "标题显示为同一事项的连续过程。",
                    },
                    {
                        "group_id": "model-2",
                        "related_group_ids": [],
                        "reason": "关系已由其他组提出。",
                    },
                    {
                        "group_id": "model-3",
                        "related_group_ids": [],
                        "reason": "关系已由其他组提出。",
                    },
                ]
            }
        ]
    )
    runner = _runner(tmp_path, analyzer, model_input_batch_target_tokens=5200)
    monkeypatch.setattr(
        runner_module,
        "estimate_structured_input_tokens",
        lambda *args, **kwargs: {
            "prompt_estimated_tokens": 5100,
            "online_input_estimated_tokens": 5201,
            "codex_input_estimated_tokens": 5300,
            "input_estimated_tokens": 5300,
        },
    )

    outcome = runner._discover_day_group_review_candidates(
        target_date="2026-07-22",
        groups=_singletons(candidates).groups,
        candidates=candidates,
    )

    assert len(outcome.result.candidate_groups) == 1
    assert outcome.oversized_submission_count == 1
    assert analyzer.allow_oversized_values == [True]
    submitted_groups = analyzer.prompts[0]["groups"]
    assert all(set(item) == {"group_id", "title"} for item in submitted_groups)
    assert not any(
        forbidden in json.dumps(submitted_groups, ensure_ascii=False)
        for forbidden in (
            "draft_id",
            "content",
            "message_id",
            "conversation",
            "attachment",
            "sender",
        )
    )
    attempt = outcome.artifact["attempts"][0]
    assert attempt["online_input_estimated_tokens"] == 5201
    assert attempt["codex_input_estimated_tokens"] == 5300


def test_day_group_discovery_failure_returns_empty_candidates_and_warning(
    tmp_path: Path,
) -> None:
    candidates = [_draft("d1", "m1"), _draft("d2", "m2")]
    invalid = {
        "group_checks": [
            {
                "group_id": "model-1",
                "related_group_ids": [],
                "reason": "没有发现关联。",
            }
        ]
    }
    analyzer = DiscoveryAnalyzer(
        [invalid, invalid],
        AnalyzerProtocolError("Codex unavailable"),
    )
    runner = _runner(tmp_path, analyzer, day_group_validation_retry_limit=1)

    outcome = runner._discover_day_group_review_candidates(
        target_date="2026-07-22",
        groups=_singletons(candidates).groups,
        candidates=candidates,
    )

    assert outcome.result.candidate_groups == []
    assert outcome.failure_count == 1
    assert outcome.retry_count == 1
    assert outcome.codex_fallback_count == 1
    assert outcome.warnings and "Skipped day group discovery" in outcome.warnings[0]
    assert outcome.artifact["status"] == "abandoned"
    assert analyzer.fallback_calls == 1
    assert "missing_group_checks" in analyzer.prompts[1]["retry_validation_errors"]
