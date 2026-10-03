from dataclasses import replace
import json
from pathlib import Path

import pytest

from src.worktrace.analyzers.protocol import parse_personal_group_render_payload
from src.worktrace.analyzers.prompts import build_personal_group_render_prompt
from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import (
    AttachmentTextBlock,
    ConversationSegmentUnit,
    ConversationSlice,
    CrossConversationGroup,
    DayDocument,
    EventFileLink,
    LinkedFileTextBlock,
    NormalizedMessage,
    SelfRelationEvidence,
    SelfIdentity,
    SegmentAnalysisBatch,
    SourceBackedEventDraft,
    WorkEvent,
)
from src.worktrace.pipeline.cross_conversation_merge import (
    materialize_grouped_merged_drafts,
)
from src.worktrace.pipeline.llm_checkpoints import LLMCheckpointStore
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore


CONFIG = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())
GROUP = CrossConversationGroup("g1", ["d1"], "d1")


def _candidate() -> SourceBackedEventDraft:
    return SourceBackedEventDraft(
        draft_id="d1",
        date="2026-07-15",
        topic="报告交付",
        content="同事交付报告，本人确认接收。",
        source_message_ids=["m_other", "m_self"],
        source_conversation_id="c1",
        source_slice_id="s1",
        confidence=0.9,
        action_label="交付报告",
        object_hint="业务报告",
        retention_reason="substantive_approval",
        retention_detail="完成审批。",
        self_evidence_message_ids=["m_self"],
        self_relations=[
            SelfRelationEvidence("primary_execution", ["m_self"]),
        ],
    )


def _payload(*, supported: bool = True) -> dict:
    facts = [
        ("topic", "业务报告交付及接收确认", ["m_other", "m_self"]),
        ("content", "同事交付业务报告，本人回复确认接收。",
         ["m_other", "m_self"]),
        ("object_hint", "业务报告", ["m_other"]),
        ("action_label", "确认报告接收", ["m_self"]),
        ("retention_reason", "deliverable_updated", ["m_other"]),
        ("retention_detail", "同事交付报告，本人确认接收。",
         ["m_other", "m_self"]),
    ]
    return {"groups": [{
        "group_id": "g1",
        "covered_draft_ids": ["d1"],
        "supported": supported,
        "fact_items": [
            {"field": field, "text": text,
             "evidence_message_ids": evidence,
             "actor": "self" if "m_self" in evidence else "other",
             "self_action_indices": [0] if "m_self" in evidence else []}
            for field, text, evidence in facts
        ] if supported else [],
        "self_relations": [{
            "relation": "response_only",
            "evidence_message_ids": ["m_self"],
        }] if supported else [],
        "self_actions": [{
            "kind": "receipt", "evidence_message_ids": ["m_self"],
        }] if supported else [],
        "removed_claims": [] if supported else [
            "仅查询原则释义，没有明确业务动作或结果。",
        ],
    }]}


def _parse(payload: dict):
    return parse_personal_group_render_payload(
        payload, group=GROUP, candidates=[_candidate()],
        allowed_self_relations=tuple(
            item.key for item in CONFIG.self_relation_types
        ),
        action_relation_support={
            item.key: item.supported_relations
            for item in CONFIG.retention_policy.contribution_actions
        },
    )


def test_final_render_corrects_retention_role_and_action_together():
    result = _parse(_payload())
    drafts = materialize_grouped_merged_drafts(
        [_candidate()], [GROUP], target_date="2026-07-15",
        message_order=["m_other", "m_self"],
        rendered_groups={"g1": result.groups[0]},
    )

    assert drafts[0].retention_reason == "deliverable_updated"
    assert drafts[0].retention_detail == "同事交付报告，本人确认接收。"
    assert drafts[0].self_relations == ["response_only"]
    assert drafts[0].action_labels == ["确认报告接收"]


@pytest.mark.parametrize("change, expected_code", [
    ("schema", "render_schema"),
    ("evidence", "render_evidence"),
    ("role", "render_role"),
    ("coverage", "render_coverage"),
    ("retention", "render_retention"),
])
def test_final_render_validation_has_stable_error_code(change, expected_code):
    payload = _payload()
    group = payload["groups"][0]
    if change == "schema":
        payload["groups"] = []
    elif change == "evidence":
        group["self_actions"][0]["evidence_message_ids"] = ["m_other"]
    elif change == "role":
        group["self_actions"][0]["kind"] = "uncertain"
        group["self_relations"][0]["relation"] = "primary_execution"
    elif change == "coverage":
        group["covered_draft_ids"] = []
    else:
        group["fact_items"][4]["text"] = "unknown_reason"

    with pytest.raises(AnalyzerProtocolError) as error:
        _parse(payload)

    assert type(error.value).__name__ == "PersonalRenderValidationError"
    assert error.value.code == expected_code
    assert "m_other" not in error.value.code


def test_final_render_validation_reports_each_failure_category():
    payload = _payload()
    group = payload["groups"][0]
    group["self_relations"][0]["relation"] = "primary_execution"
    group["fact_items"][1]["evidence_message_ids"] = ["m_other"]

    with pytest.raises(AnalyzerProtocolError) as error:
        _parse(payload)

    assert set(error.value.codes) == {"render_role", "render_evidence"}


@pytest.mark.parametrize("kind", ["receipt", "forwarding", "uncertain"])
def test_final_render_never_assigns_primary_role_to_non_execution_action(kind):
    payload = _payload()
    rendered = payload["groups"][0]
    rendered["self_actions"][0]["kind"] = kind
    rendered["self_relations"][0]["relation"] = "primary_execution"
    if kind == "uncertain":
        with pytest.raises(AnalyzerProtocolError, match=r"self_relations\[0\]"):
            _parse(payload)
    else:
        result = _parse(payload).groups[0]
        assert result.self_relations == [
            SelfRelationEvidence("response_only", ["m_self"]),
        ]


def test_final_render_combines_valid_evidence_for_repeated_role():
    payload = _payload()
    group = payload["groups"][0]
    group["self_actions"].append({
        "kind": "receipt", "evidence_message_ids": ["m_self_2"],
    })
    group["self_relations"].append({
        "relation": "response_only", "evidence_message_ids": ["m_self_2"],
    })
    group["fact_items"].append({
        "field": "content", "text": "本人再次确认接收。",
        "evidence_message_ids": ["m_self_2"], "actor": "self",
        "self_action_indices": [1],
    })
    candidate = replace(
        _candidate(), self_evidence_message_ids=["m_self", "m_self_2"],
    )

    result = parse_personal_group_render_payload(
        payload, group=GROUP, candidates=[candidate],
        allowed_self_relations=tuple(
            item.key for item in CONFIG.self_relation_types
        ),
        action_relation_support={
            item.key: item.supported_relations
            for item in CONFIG.retention_policy.contribution_actions
        },
    )

    assert result.groups[0].self_relations == [
        SelfRelationEvidence("response_only", ["m_self", "m_self_2"]),
    ]


def test_final_render_repeated_role_still_rejects_other_person_evidence():
    payload = _payload()
    payload["groups"][0]["self_relations"].append({
        "relation": "response_only", "evidence_message_ids": ["m_other"],
    })

    with pytest.raises(AnalyzerProtocolError) as error:
        _parse(payload)

    assert error.value.code == "render_evidence"


def test_final_render_repeated_role_still_requires_matching_action():
    payload = _payload()
    payload["groups"][0]["self_relations"].append({
        "relation": "response_only", "evidence_message_ids": ["m_self_2"],
    })
    candidate = replace(
        _candidate(), self_evidence_message_ids=["m_self", "m_self_2"],
    )

    with pytest.raises(AnalyzerProtocolError) as error:
        parse_personal_group_render_payload(
            payload, group=GROUP, candidates=[candidate],
            allowed_self_relations=tuple(
                item.key for item in CONFIG.self_relation_types
            ),
            action_relation_support={
                item.key: item.supported_relations
                for item in CONFIG.retention_policy.contribution_actions
            },
        )

    assert error.value.code == "render_role"


def test_final_render_rejects_self_claim_without_action():
    payload = _payload()
    payload["groups"][0]["self_actions"] = []
    payload["groups"][0]["self_relations"] = []
    with pytest.raises(AnalyzerProtocolError, match="self.*action"):
        _parse(payload)


def test_final_render_identifies_action_with_invalid_self_evidence():
    payload = _payload()
    payload["groups"][0]["self_actions"][0]["evidence_message_ids"] = [
        "m_other"
    ]
    with pytest.raises(
        AnalyzerProtocolError,
        match=r"self_actions\[0\].*self_evidence_message_ids",
    ):
        _parse(payload)


def test_final_render_identifies_fact_missing_action_evidence():
    payload = _payload()
    payload["groups"][0]["fact_items"][1]["evidence_message_ids"] = [
        "m_other"
    ]
    with pytest.raises(
        AnalyzerProtocolError,
        match=r"fact_items\[1\].*self_actions\[0\]",
    ):
        _parse(payload)


def test_final_render_identifies_unsupported_relation():
    payload = _payload()
    payload["groups"][0]["self_actions"][0]["kind"] = "collaboration"
    payload["groups"][0]["self_relations"][0]["relation"] = (
        "primary_execution"
    )
    with pytest.raises(
        AnalyzerProtocolError,
        match=r"self_relations\[0\].*primary_execution.*execution",
    ):
        _parse(payload)


def test_final_render_identifies_action_missing_from_content():
    payload = _payload()
    payload["groups"][0]["self_actions"].append({
        "kind": "execution", "evidence_message_ids": ["m_execute"],
    })
    payload["groups"][0]["self_relations"].append({
        "relation": "primary_execution",
        "evidence_message_ids": ["m_execute"],
    })
    candidate = replace(
        _candidate(), self_evidence_message_ids=["m_self", "m_execute"],
    )
    with pytest.raises(
        AnalyzerProtocolError,
        match=r"content.*self_actions\[1\]",
    ):
        parse_personal_group_render_payload(
            payload, group=GROUP, candidates=[candidate],
            action_relation_support={
                item.key: item.supported_relations
                for item in CONFIG.retention_policy.contribution_actions
            },
            allowed_self_relations=tuple(
                item.key for item in CONFIG.self_relation_types
            ),
        )


def test_final_render_reports_independent_relation_and_content_errors():
    payload = _payload()
    item = payload["groups"][0]
    item["self_actions"][0]["kind"] = "uncertain"
    item["self_actions"].append({
        "kind": "execution", "evidence_message_ids": ["m_execute"],
    })
    item["self_relations"][0]["relation"] = "primary_execution"
    candidate = replace(
        _candidate(), self_evidence_message_ids=["m_self", "m_execute"],
    )
    with pytest.raises(AnalyzerProtocolError) as error:
        parse_personal_group_render_payload(
            payload, group=GROUP, candidates=[candidate],
            action_relation_support={
                action.key: action.supported_relations
                for action in CONFIG.retention_policy.contribution_actions
            },
            allowed_self_relations=tuple(
                relation.key for relation in CONFIG.self_relation_types
            ),
        )
    feedback = str(error.value)
    assert "self_relations[0]" in feedback
    assert "self_actions[1]" in feedback
    assert "content" in feedback


@pytest.mark.parametrize("kind", ["execution", "commitment"])
def test_final_render_keeps_primary_role_with_execution_evidence(kind):
    payload = _payload()
    payload["groups"][0]["self_actions"][0]["kind"] = kind
    payload["groups"][0]["self_relations"][0]["relation"] = "primary_execution"
    result = _parse(payload)
    assert result.groups[0].self_relations[0].relation == "primary_execution"


def _message(message_id, text, *, conversation="c1", minute=0):
    return NormalizedMessage(
        conversation_id=conversation, conversation_name="业务沟通",
        message_id=message_id, sender_open_id=message_id,
        sender_name=message_id, send_time=f"2026-07-15T10:{minute:02d}:00+08:00",
        message_type="text", text=text,
        reply_to_message_id=None, quote_message_id=None,
    )


def _slice(messages):
    return ConversationSlice(
        slice_id="s1", conversation_id="c1", conversation_name="业务沟通",
        anchor_message_ids=["m_self"], in_day_message_ids=[m.message_id for m in messages],
        messages=messages,
    )


@pytest.mark.parametrize("earlier, later, conversation, expected", [
    ("本批核查数据\r\n已完成。", "本批核查数据\n已完成。", "c1", True),
    ("", "", "c1", False),
    ("本批核查数据已完成。", "本批核查数据已完成。", "c2", False),
    ("很长的原文" * 3000 + "甲", "很长的原文" * 3000 + "乙", "c1", False),
], ids=["same_full_body", "empty_attachments", "different_chat", "different_tail"])
def test_final_review_adds_only_same_conversation_full_text_context(
    earlier, later, conversation, expected,
):
    data = json.loads(build_personal_group_render_prompt(
        "2026-07-15", group=GROUP, candidates=[_candidate()], config=CONFIG,
        messages=[
            _message("m_original", earlier, conversation=conversation),
            _message("m_other", "数据交付"),
            _message("m_self", later, minute=2),
        ],
        conversation_slices=[_slice([
            _message("m_original", earlier, conversation=conversation),
            _message("m_self", later, minute=2),
        ])],
    ))
    ids = [item["id"] for item in data["messages"]]
    assert ("m_original" in ids) == expected
    assert data["same_text_messages"] == ([{
        "earlier_message_id": "m_original", "later_message_id": "m_self",
    }] if expected else [])


def test_duplicate_context_does_not_include_unrelated_old_acknowledgements():
    old = [_message(f"old-{i}", "收到") for i in range(100)]
    messages = [*old, _message("m_other", "报告已交付", minute=1),
                _message("m_self", "收到", minute=2)]
    data = json.loads(build_personal_group_render_prompt(
        "2026-07-15", group=GROUP, candidates=[_candidate()], config=CONFIG,
        messages=messages, conversation_slices=[_slice(messages[-2:])],
    ))
    assert [item["id"] for item in data["messages"]] == ["m_other", "m_self"]
    assert data["same_text_messages"] == []


def test_final_render_can_drop_knowledge_query_without_forcing_a_reason():
    result = _parse(_payload(supported=False))
    drafts = materialize_grouped_merged_drafts(
        [_candidate()], [GROUP], target_date="2026-07-15",
        message_order=["m_other", "m_self"],
        rendered_groups={"g1": result.groups[0]},
    )
    assert drafts == []


@pytest.mark.parametrize("evidence", [["m_other"], [], ["unknown"]])
def test_final_render_rejects_role_without_valid_self_evidence(evidence):
    payload = _payload()
    payload["groups"][0]["self_relations"][0][
        "evidence_message_ids"
    ] = evidence
    with pytest.raises(AnalyzerProtocolError, match="self.*evidence"):
        _parse(payload)


def test_final_render_rejects_invalid_retention_category():
    payload = _payload()
    payload["groups"][0]["fact_items"][4]["text"] = "unknown"
    with pytest.raises(AnalyzerProtocolError, match="retention_reason"):
        _parse(payload)


def test_final_render_sends_source_messages_and_uses_reviewed_metadata(tmp_path):
    class Analyzer:
        def request_function(self, prompt, *, function_spec,
                             allow_oversized_input=False):
            data = json.loads(prompt)
            assert [item["id"] for item in data["messages"]] == [
                "m_other", "m_self",
            ]
            assert data["self_evidence_message_ids"] == ["m_self"]
            assert data["messages"][0]["x"] == "业务报告已交付。"
            assert data["messages"][1]["x"] == "OK"
            assert [item["text"] for item in data["attachment_texts"]] == [
                "附件中的核查结论。",
            ]
            assert [item["text"] for item in data["linked_file_texts"]] == [
                "在线文档中的核查结论。",
            ]
            properties = function_spec.parameters["properties"][
                "groups"
            ]["items"]["properties"]
            assert "self_relations" not in properties
            assert "supported" in properties
            assert properties["self_actions"]["items"]["properties"][
                "evidence_message_ids"
            ]["items"]["enum"] == ["m_self"]
            assert set(data["retention_reason_values"]) == {
                "deliverable_updated", "decision_made", "issue_or_risk_found",
                "follow_up_assigned", "external_business_progress",
                "substantive_approval",
            }
            return _simple_payload()

    messages = [
        NormalizedMessage(
            conversation_id="c1", conversation_name="业务沟通",
            message_id=message_id, sender_open_id=sender,
            sender_name=sender, send_time="2026-07-15T10:00:00+08:00",
            message_type="text", text=text,
            reply_to_message_id=None, quote_message_id=None,
        )
        for message_id, sender, text in [
            ("m_other", "other", "业务报告已交付。"),
            ("m_self", "self", "OK"),
        ]
    ]
    config = replace(CONFIG, data_root=tmp_path)
    runner = DailyTraceRunner(config, RuntimeDependencies(
        chat_source=object(), content_resolver=object(), analyzer=Analyzer(),
        delivery_channel=object(),
        event_store=MarkdownEventStore(config),
    ))
    outcome = runner._render_personal_multi_groups(
        target_date="2026-07-15", groups=[GROUP],
        candidates=[replace(_candidate(), source_message_ids=["m_other"])],
        messages=messages,
        conversation_slices=[ConversationSlice(
            slice_id="s1", conversation_id="c1", conversation_name="业务沟通",
            messages=messages, anchor_message_ids=["m_self"],
            in_day_message_ids=["m_other", "m_self"],
            attachment_texts=[
                AttachmentTextBlock("a1", "m_other", "报告.txt", "附件中的核查结论。"),
                AttachmentTextBlock("a2", "unrelated", "其他.txt", "无关附件内容。"),
            ],
            linked_file_texts=[LinkedFileTextBlock(
                "l1", "m_other", "业务报告", "https://example.com/report",
                "在线文档中的核查结论。",
            )],
        )],
    )
    assert outcome.failure_count == 0
    assert outcome.rendered_groups["g1"].retention_reason == (
        "deliverable_updated"
    )


def test_final_render_retry_receives_specific_evidence_feedback(tmp_path):
    class Analyzer:
        def __init__(self):
            self.prompts = []

        def request_function(self, prompt, *, function_spec, **kwargs):
            self.prompts.append(json.loads(prompt))
            payload = _payload()
            if len(self.prompts) == 1:
                payload["groups"][0]["fact_items"][1][
                    "evidence_message_ids"
                ] = ["m_other"]
            return payload

    analyzer = Analyzer()
    config = replace(CONFIG, data_root=tmp_path,
                     day_group_validation_retry_limit=1)
    runner = DailyTraceRunner(config, RuntimeDependencies(
        chat_source=object(), content_resolver=object(), analyzer=analyzer,
        delivery_channel=object(), event_store=MarkdownEventStore(config),
    ))
    outcome = runner._render_personal_multi_groups(
        target_date="2026-07-15", groups=[GROUP], candidates=[_candidate()],
    )

    assert outcome.failure_count == 0
    assert outcome.retry_count == 1
    assert analyzer.prompts[0]["previous_result"] is None
    previous = analyzer.prompts[1]["previous_result"]
    assert previous["groups"][0]["fact_items"][1][
        "evidence_message_ids"
    ] == ["m_other"]
    assert "fact_items[1]" in analyzer.prompts[1]["validation_feedback"]
    assert "self_actions[0]" in analyzer.prompts[1]["validation_feedback"]


def test_final_render_counts_each_validation_failure_without_raw_evidence(
    tmp_path: Path,
) -> None:
    class InvalidRoleAnalyzer:
        def request_function(self, prompt, **kwargs):
            payload = _payload()
            payload["groups"][0]["self_actions"][0]["kind"] = "uncertain"
            payload["groups"][0]["self_relations"][0]["relation"] = (
                "primary_execution"
            )
            return payload

    config = replace(
        CONFIG, data_root=tmp_path, day_group_validation_retry_limit=1,
        llm_mode="online_only",
    )
    runner = DailyTraceRunner(config, RuntimeDependencies(
        chat_source=object(), content_resolver=object(),
        analyzer=InvalidRoleAnalyzer(), delivery_channel=object(),
        event_store=MarkdownEventStore(config),
    ))

    outcome = runner._render_personal_multi_groups(
        target_date="2026-07-15", groups=[GROUP], candidates=[_candidate()],
    )

    assert outcome.failure_count == 1
    assert outcome.retry_count == 1
    assert outcome.error_counts == {"render_role": 2}
    assert all(
        attempt["safe_error_code"] == "render_role"
        for attempt in outcome.artifact["attempts"]
    )


def test_final_render_counts_each_category_once_per_attempt(tmp_path: Path) -> None:
    class InvalidAnalyzer:
        def request_function(self, prompt, **kwargs):
            payload = _payload()
            group = payload["groups"][0]
            group["self_relations"][0]["relation"] = "primary_execution"
            group["fact_items"][1]["evidence_message_ids"] = ["m_other"]
            return payload

    config = replace(
        CONFIG, data_root=tmp_path, day_group_validation_retry_limit=0,
        llm_mode="online_only",
    )
    runner = DailyTraceRunner(config, RuntimeDependencies(
        chat_source=object(), content_resolver=object(),
        analyzer=InvalidAnalyzer(), delivery_channel=object(),
        event_store=MarkdownEventStore(config),
    ))

    outcome = runner._render_personal_multi_groups(
        target_date="2026-07-15", groups=[GROUP], candidates=[_candidate()],
    )

    assert outcome.error_counts == {"render_role": 1, "render_evidence": 1}


def test_final_render_missing_capability_has_request_category(tmp_path: Path) -> None:
    config = replace(CONFIG, data_root=tmp_path)
    runner = DailyTraceRunner(config, RuntimeDependencies(
        chat_source=object(), content_resolver=object(),
        analyzer=object(), delivery_channel=object(),
        event_store=MarkdownEventStore(config),
    ))

    outcome = runner._render_personal_multi_groups(
        target_date="2026-07-15", groups=[GROUP], candidates=[_candidate()],
    )

    assert outcome.failure_count == 1
    assert outcome.error_counts == {"render_request": 1}
    assert outcome.artifact["summary"]["error_counts"] == outcome.error_counts


def test_empty_final_render_response_is_a_schema_failure(tmp_path: Path) -> None:
    class EmptyAnalyzer:
        def request_function(self, prompt, **kwargs):
            return None

    config = replace(CONFIG, data_root=tmp_path, llm_mode="online_only")
    runner = DailyTraceRunner(config, RuntimeDependencies(
        chat_source=object(), content_resolver=object(), analyzer=EmptyAnalyzer(),
        delivery_channel=object(), event_store=MarkdownEventStore(config),
    ))

    outcome = runner._render_personal_multi_groups(
        target_date="2026-07-15", groups=[GROUP], candidates=[_candidate()],
    )

    assert outcome.failure_count == 1
    assert outcome.error_counts == {"render_schema": 1}


def test_final_render_preserves_separate_self_evidence_in_event_sources():
    candidate = replace(_candidate(), source_message_ids=["m_other"])
    result = parse_personal_group_render_payload(
        _payload(), group=GROUP, candidates=[candidate],
        action_relation_support={
            item.key: item.supported_relations
            for item in CONFIG.retention_policy.contribution_actions
        },
        allowed_self_relations=tuple(
            item.key for item in CONFIG.self_relation_types
        ),
    )
    drafts = materialize_grouped_merged_drafts(
        [candidate], [GROUP], target_date="2026-07-15",
        message_order=["m_other", "m_self"],
        rendered_groups={"g1": result.groups[0]},
    )
    assert drafts[0].source_message_ids == ["m_other", "m_self"]
    assert drafts[0].self_relations == ["response_only"]


@pytest.mark.parametrize("valid_fallback", [False, True])
def test_final_review_checks_actions_on_primary_and_fallback(tmp_path, valid_fallback):
    class Analyzer:
        def request_function(self, prompt, **kwargs):
            invalid = _payload()
            invalid["groups"][0]["self_actions"][0]["kind"] = "uncertain"
            invalid["groups"][0]["self_relations"][0]["relation"] = "primary_execution"
            return invalid

        def last_request_used_fallback(self):
            return False

        def fallback_current_request(self, method_name, prompt, **kwargs):
            return _payload() if valid_fallback else self.request_function(prompt)

    config = replace(CONFIG, data_root=tmp_path)
    runner = DailyTraceRunner(config, RuntimeDependencies(
        chat_source=object(), content_resolver=object(), analyzer=Analyzer(),
        delivery_channel=object(), event_store=MarkdownEventStore(config),
    ))
    outcome = runner._render_personal_multi_groups(
        target_date="2026-07-15", groups=[GROUP], candidates=[_candidate()],
    )
    assert outcome.failure_count == (0 if valid_fallback else 1)
    assert outcome.error_counts == {
        "render_role": 2 if valid_fallback else 3,
    }
    if valid_fallback:
        assert outcome.rendered_groups["g1"].self_relations[0].relation == "response_only"
    else:
        assert outcome.rendered_groups == {}


def test_repeated_body_context_reaches_schema_parser_and_materialized_sources(tmp_path):
    payload = _payload()
    payload["groups"][0]["self_actions"][0]["kind"] = "forwarding"
    payload["groups"][0]["fact_items"][1]["evidence_message_ids"].append("m_original")

    class Analyzer:
        def request_function(self, prompt, *, function_spec, **kwargs):
            data = json.loads(prompt)
            assert data["same_text_messages"] == [{
                "earlier_message_id": "m_original", "later_message_id": "m_self",
            }]
            fields = function_spec.parameters["properties"]["groups"]["items"]["properties"]
            fact_properties = fields["fact_items"]["items"]["properties"]
            fact_ids = fact_properties["evidence_message_ids"]["items"]["enum"]
            assert "m_original" in fact_ids
            action_properties = fields["self_actions"]["items"]["properties"]
            action_ids = action_properties["evidence_message_ids"]["items"]["enum"]
            assert action_ids == ["m_self"]
            return payload

    config = replace(CONFIG, data_root=tmp_path)
    runner = DailyTraceRunner(config, RuntimeDependencies(
        chat_source=object(), content_resolver=object(), analyzer=Analyzer(),
        delivery_channel=object(), event_store=MarkdownEventStore(config),
    ))
    candidate = _candidate()
    outcome = runner._render_personal_multi_groups(
        target_date="2026-07-15", groups=[GROUP], candidates=[candidate],
        messages=[_message("m_original", "本批数据已核查"),
                  _message("m_other", "附件已经交付", minute=1),
                  _message("m_self", "本批数据已核查", minute=2)],
        conversation_slices=[_slice([
            _message("m_original", "本批数据已核查"),
            _message("m_other", "附件已经交付", minute=1),
            _message("m_self", "本批数据已核查", minute=2),
        ])],
    )
    assert outcome.failure_count == 0
    drafts = materialize_grouped_merged_drafts(
        [candidate], [GROUP], target_date="2026-07-15",
        message_order=["m_original", "m_other", "m_self"],
        rendered_groups=outcome.rendered_groups,
    )
    assert drafts[0].source_message_ids == ["m_original", "m_other", "m_self"]
    assert drafts[0].self_relations == ["response_only"]


def test_forwarding_does_not_cancel_independent_execution_evidence():
    payload = _payload()
    item = payload["groups"][0]
    item["self_actions"] = [
        {"kind": "forwarding", "evidence_message_ids": ["m_self"]},
        {"kind": "execution", "evidence_message_ids": ["m_execute"]},
    ]
    item["self_relations"] = [{
        "relation": "primary_execution", "evidence_message_ids": ["m_execute"],
    }, {
        "relation": "response_only", "evidence_message_ids": ["m_self"],
    }]
    item["fact_items"][1].update({
        "text": "同事提供数据，本人完成核查并再次发送数据。",
        "actor": "shared", "self_action_indices": [0, 1],
        "evidence_message_ids": ["m_other", "m_self", "m_execute"],
    })
    candidate = replace(_candidate(), self_evidence_message_ids=["m_self", "m_execute"])
    result = parse_personal_group_render_payload(
        payload, group=GROUP, candidates=[candidate],
        allowed_self_relations=[item.key for item in CONFIG.self_relation_types],
        action_relation_support={item.key: item.supported_relations
                                 for item in CONFIG.retention_policy.contribution_actions},
    )
    assert result.groups[0].self_relations[0].relation == "primary_execution"


def test_execution_claim_cannot_keep_only_a_receipt_role():
    payload = _payload()
    item = payload["groups"][0]
    item["self_actions"].append({
        "kind": "execution", "evidence_message_ids": ["m_self"],
    })
    item["fact_items"][1]["self_action_indices"] = [0, 1]
    with pytest.raises(AnalyzerProtocolError, match="action.*relation"):
        _parse(payload)


def test_personal_action_label_cannot_only_describe_other_peoples_work():
    payload = _payload()
    payload["groups"][0]["fact_items"][3].update({
        "text": "完成数据汇总及报告交付", "actor": "other",
        "self_action_indices": [], "evidence_message_ids": ["m_other"],
    })
    with pytest.raises(AnalyzerProtocolError, match="action_label.*self action"):
        _parse(payload)


def test_personal_action_label_must_reference_a_known_action():
    payload = _simple_payload()
    item = payload["groups"][0]
    item["self_actions"].append({
        "kind": "uncertain", "evidence_message_ids": ["m_uncertain"],
        "relation": None,
    })
    item["fact_items"][1]["evidence_message_ids"].append("m_uncertain")
    item["fact_items"][3]["evidence_message_ids"] = ["m_uncertain"]
    with pytest.raises(AnalyzerProtocolError, match="action_label.*self action"):
        parse_personal_group_render_payload(
            payload, group=GROUP,
            candidates=[replace(_candidate(), self_evidence_message_ids=[
                "m_self", "m_uncertain",
            ])],
            allowed_self_relations=tuple(x.key for x in CONFIG.self_relation_types),
            action_relation_support={x.key: x.supported_relations
                                     for x in CONFIG.retention_policy.contribution_actions},
        )


def test_failed_final_review_cannot_materialize_unreviewed_metadata():
    with pytest.raises(AnalyzerProtocolError, match="final review"):
        materialize_grouped_merged_drafts(
            [_candidate()], [GROUP], target_date="2026-07-15",
            message_order=["m_other", "m_self"], rendered_groups={},
        )


def test_resumed_analysis_restores_messages_and_supplemental_evidence(tmp_path):
    class Analyzer:
        def analyze_segment_batch(self, batch):
            raise AssertionError("A complete checkpoint should be reused")

    messages = [NormalizedMessage(
        conversation_id="c1", conversation_name="业务沟通",
        message_id=message_id, sender_open_id=sender, sender_name=sender,
        send_time="2026-07-15T10:00:00+08:00", message_type="text", text=text,
        reply_to_message_id=None, quote_message_id=None,
    ) for message_id, sender, text in [
        ("m_other", "other", "业务报告已交付。"),
        ("m_self", "self", "OK"),
        ("m_context", "other", "补读的核查结论。"),
    ]]
    unit = ConversationSegmentUnit(
        segment_id="s1", conversation_id="c1", conversation_name="业务沟通",
        primary_message_ids=["m_other", "m_self"], context_message_ids=[],
        self_evidence_message_ids=["m_self"], response_signals=[],
        response_assessments=[], messages=messages[:2],
    )
    batch = SegmentAnalysisBatch(
        target_date="2026-07-15", conversation_id="c1",
        conversation_name="业务沟通", self_open_id="self",
        self_display_name="本人", segments=[unit],
    )
    evidence_slice = ConversationSlice(
        slice_id="s1", conversation_id="c1", conversation_name="业务沟通",
        anchor_message_ids=["m_self"], in_day_message_ids=["m_other", "m_self"],
        messages=messages,
        attachment_texts=[AttachmentTextBlock(
            "a1", "m_other", "报告.txt", "附件中的核查结论。",
        )],
        linked_file_texts=[LinkedFileTextBlock(
            "l1", "m_other", "报告", "https://example.com/report",
            "在线文档中的核查结论。",
        )],
    )
    config = replace(CONFIG, data_root=tmp_path)
    store = LLMCheckpointStore(config, "2026-07-15")
    store.save_analysis(
        batch, [_candidate()], [], 0, evidence_slices=[evidence_slice],
    )
    runner = DailyTraceRunner(config, RuntimeDependencies(
        chat_source=object(), content_resolver=object(), analyzer=Analyzer(),
        delivery_channel=object(), event_store=MarkdownEventStore(config),
    ))
    runner.checkpoint_store = store
    candidates, _warnings, _skipped, calls = (
        runner._analyze_segment_batch_with_retry(
            batch=batch, self_identity=SelfIdentity("self", "本人", "test"),
        )
    )
    assert calls == 0
    assert candidates == [_candidate()]
    restored = runner._analyzed_slices["s1"]
    assert restored.messages == messages
    assert restored.attachment_texts == evidence_slice.attachment_texts
    assert restored.linked_file_texts == evidence_slice.linked_file_texts


def test_legacy_analysis_checkpoint_without_evidence_is_not_reused(tmp_path):
    store = LLMCheckpointStore(replace(CONFIG, data_root=tmp_path), "2026-07-15")
    batch = SegmentAnalysisBatch(
        "2026-07-15", "c1", "业务沟通", "self", "本人", [],
    )
    store.save_analysis(batch, [], [], 0)
    path = next(tmp_path.rglob("analysis/*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["result"].pop("evidence_slices", None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert store.load_analysis(batch) is None


@pytest.mark.parametrize("changed, has_provenance", [
    (False, True), (True, True), (False, False),
])
def test_legacy_v3_duplicate_files_do_not_hide_actual_manual_edits(
    changed, has_provenance,
):
    fixture = Path(__file__).parents[1] / "fixtures" / (
        "personal_duplicate_files_v3.md"
    )
    markdown = fixture.read_text(encoding="utf-8")
    if not has_provenance:
        markdown = markdown.replace('"sha256:' + "a" * 64 + '"', "")
    if changed:
        markdown = markdown.replace("本人确认接收。", "本人退回修订。")
    store = MarkdownEventStore(CONFIG)
    loaded = store.parse_day_document(markdown)
    expected = "manual_modified" if changed else (
        "" if has_provenance else "manual_unknown"
    )
    assert loaded.events[0].manual_edit_type == expected
    rerendered = store.render_day_document(loaded)
    assert rerendered.count("  - 《业务报告.docx》") == 1
    assert store.parse_day_document(rerendered).events[0].manual_edit_type == expected


def test_duplicate_display_files_preserve_identity_and_roundtrip():
    event = WorkEvent(
        date="2026-07-15", event_id="event", title="报告交付",
        content="同事交付报告，本人确认接收。",
        source_message_ids=["m1"],
        conversation_fingerprints=["sha256:" + "a" * 64],
        retention_reason="deliverable_updated",
        object_hint="业务报告", retention_detail="报告已交付。",
        referenced_attachment_ids=["upload-a", "upload-b"],
        file_links=[
            EventFileLink("", "业务报告.docx", "attachment"),
            EventFileLink("", "业务报告.docx", "attachment"),
            EventFileLink("https://example.com/a", "附件", "document"),
            EventFileLink("https://example.com/b", "附件", "document"),
        ],
    )
    store = MarkdownEventStore(CONFIG)
    markdown = store.render_day_document(DayDocument(
        "2026-07-15", [event], "2026-07-15T10:00:00+08:00",
    ))
    assert markdown.count("  - 《业务报告.docx》") == 1
    assert "[附件](https://example.com/a)" in markdown
    assert "[附件](https://example.com/b)" in markdown
    loaded = store.parse_day_document(markdown).events[0]
    assert loaded.manual_edit_type == ""
    assert len(loaded.file_keys) == 4


def _simple_payload():
    payload = _payload()
    item = payload["groups"][0]
    del item["self_relations"]
    for action in item["self_actions"]:
        action["relation"] = None
    for fact in item["fact_items"]:
        del fact["self_action_indices"]
    return payload


@pytest.mark.parametrize("kind, role", [
    ("receipt", "response_only"),
    ("decision", "decision_confirmation"),
    ("initiation", "initiated"),
    ("acceptance", "feedback_acceptance"),
])
def test_final_render_derives_unique_role_and_fact_actions(kind, role):
    payload = _simple_payload()
    payload["groups"][0]["self_actions"][0]["kind"] = kind
    result = _parse(payload).groups[0]
    assert result.self_relations == [SelfRelationEvidence(role, ["m_self"])]
    assert result.fact_items[1].self_action_indices == [0]


def test_final_render_schema_does_not_request_action_indices():
    from src.worktrace.analyzers.output_schemas import (
        personal_group_render_output_schema,
    )
    schema = personal_group_render_output_schema(
        group_id="g1", draft_ids=["d1"], message_ids=["m_self"],
        self_message_ids=["m_self"], config=CONFIG,
    )
    facts = schema["properties"]["groups"]["items"]["properties"][
        "fact_items"
    ]["items"]
    assert "self_action_indices" not in facts["properties"]
    assert "self_action_indices" not in facts["required"]


def test_final_render_fact_can_use_relevant_subset_of_action_evidence():
    payload = _simple_payload()
    payload["groups"][0]["self_actions"][0]["evidence_message_ids"] = [
        "m_self", "m_self_2",
    ]
    result = parse_personal_group_render_payload(
        payload, group=GROUP,
        candidates=[replace(_candidate(), self_evidence_message_ids=[
            "m_self", "m_self_2",
        ])],
        allowed_self_relations=tuple(x.key for x in CONFIG.self_relation_types),
        action_relation_support={x.key: x.supported_relations
                                 for x in CONFIG.retention_policy.contribution_actions},
    )
    assert result.groups[0].fact_items[1].evidence_message_ids == [
        "m_other", "m_self",
    ]
    assert result.groups[0].fact_items[1].self_action_indices == [0]


def test_final_render_simple_contract_still_rejects_unsupported_self_claim():
    payload = _simple_payload()
    payload["groups"][0]["fact_items"][1]["evidence_message_ids"] = ["m_other"]
    with pytest.raises(AnalyzerProtocolError) as error:
        _parse(payload)
    assert error.value.code == "render_evidence"


def test_final_render_ambiguous_action_still_requires_role_choice():
    payload = _simple_payload()
    payload["groups"][0]["self_actions"][0]["kind"] = "execution"
    with pytest.raises(AnalyzerProtocolError) as error:
        _parse(payload)
    assert error.value.code == "render_role"


@pytest.mark.parametrize("kind, supplied, expected", [
    ("receipt", "primary_execution", "response_only"),
    ("decision", "initiated", "decision_confirmation"),
])
def test_final_render_normalizes_redundant_role_without_raising_contribution(
    kind, supplied, expected,
):
    payload = _simple_payload()
    item = payload["groups"][0]
    item["self_actions"][0]["kind"] = kind
    item["self_actions"][0]["relation"] = supplied
    assert _parse(payload).groups[0].self_relations == [
        SelfRelationEvidence(expected, ["m_self"]),
    ]


def test_final_render_ignores_legacy_indices_and_uses_real_evidence():
    payload = _payload()
    payload["groups"][0]["fact_items"][1]["self_action_indices"] = [999]
    result = _parse(payload)
    assert result.groups[0].fact_items[1].self_action_indices == [0]


@pytest.mark.parametrize("kind, role", [
    ("execution", "primary_execution"),
    ("execution", "collaboration"),
    ("commitment", "assigned"),
    ("collaboration", "response_only"),
])
def test_final_render_role_choice_shares_its_actions_evidence(kind, role):
    payload = _simple_payload()
    payload["groups"][0]["self_actions"][0].update({
        "kind": kind, "relation": role,
    })
    result = _parse(payload).groups[0]
    assert result.self_relations == [SelfRelationEvidence(role, ["m_self"])]


def test_final_render_schema_places_role_on_action():
    from src.worktrace.analyzers.output_schemas import (
        personal_group_render_output_schema,
    )
    schema = personal_group_render_output_schema(
        group_id="g1", draft_ids=["d1"], message_ids=["m_self"],
        self_message_ids=["m_self"], config=CONFIG,
    )
    group = schema["properties"]["groups"]["items"]
    assert "self_relations" not in group["properties"]
    assert "self_relations" not in group["required"]
    assert "relation" in group["properties"]["self_actions"]["items"]["required"]


def test_new_contract_cannot_borrow_automatic_role_from_another_action():
    payload = _simple_payload()
    payload["groups"][0]["self_actions"].append({
        "kind": "collaboration", "evidence_message_ids": ["m_self"],
        "relation": None,
    })
    with pytest.raises(AnalyzerProtocolError, match=r"self_actions\[1\].relation"):
        _parse(payload)


def test_new_contract_rejects_incompatible_choice_on_ambiguous_action():
    payload = _simple_payload()
    payload["groups"][0]["self_actions"][0].update({
        "kind": "execution", "relation": "initiated",
    })
    with pytest.raises(AnalyzerProtocolError) as error:
        _parse(payload)
    assert error.value.code == "render_role"


def test_new_contract_combines_role_evidence_without_duplicate_role_output():
    payload = _simple_payload()
    payload["groups"][0]["self_actions"].append({
        "kind": "forwarding", "evidence_message_ids": ["m_self"],
        "relation": None,
    })
    result = _parse(payload).groups[0]
    assert result.self_relations == [
        SelfRelationEvidence("response_only", ["m_self"]),
    ]
    assert result.fact_items[1].self_action_indices == [0, 1]


def test_mixed_contract_cannot_borrow_another_actions_role_choice():
    payload = _simple_payload()
    group = payload["groups"][0]
    group["self_relations"] = []
    group["self_actions"] = [
        {"kind": "collaboration", "relation": "primary_execution",
         "evidence_message_ids": ["m_self"]},
        {"kind": "execution", "relation": "collaboration",
         "evidence_message_ids": ["m_self"]},
    ]
    with pytest.raises(AnalyzerProtocolError) as error:
        _parse(payload)
    assert error.value.code == "render_schema"


def test_legacy_group_role_cannot_override_an_explicit_action_choice():
    payload = _simple_payload()
    group = payload["groups"][0]
    group["self_actions"][0].update({
        "kind": "execution", "relation": "collaboration",
    })
    group["self_relations"] = [{
        "relation": "primary_execution", "evidence_message_ids": ["m_self"],
    }]
    with pytest.raises(AnalyzerProtocolError):
        _parse(payload)
