from dataclasses import replace
import json
from pathlib import Path

import pytest

from src.worktrace.analyzers.protocol import parse_personal_group_render_payload
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
             "evidence_message_ids": evidence}
            for field, text, evidence in facts
        ] if supported else [],
        "self_relations": [{
            "relation": "response_only",
            "evidence_message_ids": ["m_self"],
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
            assert "self_relations" in properties
            assert "supported" in properties
            assert properties["self_relations"]["items"]["properties"][
                "evidence_message_ids"
            ]["items"]["enum"] == ["m_self"]
            assert set(data["retention_reason_values"]) == {
                "deliverable_updated", "decision_made", "issue_or_risk_found",
                "follow_up_assigned", "external_business_progress",
                "substantive_approval",
            }
            return _payload()

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


def test_final_render_preserves_separate_self_evidence_in_event_sources():
    candidate = replace(_candidate(), source_message_ids=["m_other"])
    result = parse_personal_group_render_payload(
        _payload(), group=GROUP, candidates=[candidate],
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
    payload = json.loads(path.read_text())
    payload["result"].pop("evidence_slices", None)
    path.write_text(json.dumps(payload))
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
    markdown = fixture.read_text()
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
