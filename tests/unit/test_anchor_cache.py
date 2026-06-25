from __future__ import annotations

from pathlib import Path

from src.worktrace.cache import FileSystemAnchorCacheStore, build_anchor_input_fingerprint
from src.worktrace.models import (
    AnchorCacheEntry,
    AnchorUnit,
    AttachmentMeta,
    AttachmentTextBlock,
    ContextRequest,
    NormalizedMessage,
    SourceBackedEventDraft,
)


def sample_anchor_unit() -> AnchorUnit:
    message_1 = NormalizedMessage(
        conversation_id="oc_123",
        conversation_name="项目群",
        message_id="om_001",
        sender_open_id="ou_001",
        sender_name="Alice",
        send_time="2026-06-23T10:00:00+08:00",
        message_type="text",
        text="请整理今天的进展",
        reply_to_message_id=None,
        quote_message_id=None,
        attachments=[
            AttachmentMeta(
                attachment_id="att_001",
                file_name="a.txt",
                mime_type="text/plain",
                file_size=1,
            )
        ],
    )
    message_2 = NormalizedMessage(
        conversation_id="oc_123",
        conversation_name="项目群",
        message_id="om_002",
        sender_open_id="ou_002",
        sender_name="Bob",
        send_time="2026-06-23T10:03:00+08:00",
        message_type="text",
        text="好的，我中午前发你",
        reply_to_message_id="om_001",
        quote_message_id=None,
    )
    return AnchorUnit(
        anchor_unit_id="anchor-001",
        conversation_id="oc_123",
        conversation_name="项目群",
        anchor_message_ids=["om_001"],
        in_day_message_ids=["om_001", "om_002"],
        base_message_ids=["om_001", "om_002"],
        messages=[message_1, message_2],
        reply_relation_ids=["om_001"],
        quote_relation_ids=[],
        attachment_refs=message_1.attachments,
    )


def test_anchor_input_fingerprint_is_stable() -> None:
    anchor_unit = sample_anchor_unit()

    assert build_anchor_input_fingerprint(anchor_unit) == build_anchor_input_fingerprint(
        anchor_unit
    )


def test_filesystem_anchor_cache_store_roundtrip(tmp_path: Path) -> None:
    cache = FileSystemAnchorCacheStore(tmp_path / "cache")
    anchor_unit = sample_anchor_unit()
    fingerprint = build_anchor_input_fingerprint(
        anchor_unit,
        attachment_texts=[
            AttachmentTextBlock(
                attachment_id="att_001",
                message_id="om_001",
                file_name="a.txt",
                text="附件正文",
            )
        ],
    )
    entry = AnchorCacheEntry(
        target_date="2026-06-23",
        anchor_unit_id=anchor_unit.anchor_unit_id,
        input_fingerprint=fingerprint,
        status="completed",
        pass_index=2,
        prompt_version="v1",
        schema_version="v1",
        analyzer_key="codex:gpt-5.4",
        candidate_events=[
            SourceBackedEventDraft(
                draft_id="draft-001",
                date="2026-06-23",
                topic="日报整理",
                content="确认中午前整理并发送进展",
                result="",
                source_message_ids=["om_001", "om_002"],
                source_conversation_id="oc_123",
                source_slice_id=anchor_unit.anchor_unit_id,
                confidence=0.91,
            )
        ],
        context_requests=[
            ContextRequest(
                slice_id=anchor_unit.anchor_unit_id,
                request_type="later_messages",
                target_message_ids=["om_002"],
                target_attachment_ids=[],
                reason="确认是否已经发送",
                limit=5,
            )
        ],
        included_message_ids=["om_001", "om_002"],
        included_attachment_ids=["att_001"],
        needs_cross_anchor_merge=False,
        created_at="2026-06-24T10:00:00+08:00",
    )

    cache.write(entry)
    loaded = cache.read(
        target_date="2026-06-23",
        anchor_unit_id=anchor_unit.anchor_unit_id,
        input_fingerprint=fingerprint,
    )

    assert loaded == entry


def test_filesystem_anchor_cache_store_invalidate_day(tmp_path: Path) -> None:
    cache = FileSystemAnchorCacheStore(tmp_path / "cache")
    anchor_unit = sample_anchor_unit()
    fingerprint = build_anchor_input_fingerprint(anchor_unit)
    entry = AnchorCacheEntry(
        target_date="2026-06-23",
        anchor_unit_id=anchor_unit.anchor_unit_id,
        input_fingerprint=fingerprint,
        status="completed",
        pass_index=1,
        prompt_version="v1",
        schema_version="v1",
        analyzer_key="codex:gpt-5.4",
        created_at="2026-06-24T10:00:00+08:00",
    )

    cache.write(entry)

    assert cache.invalidate_day("2026-06-23") == 1
    assert cache.read(
        target_date="2026-06-23",
        anchor_unit_id=anchor_unit.anchor_unit_id,
        input_fingerprint=fingerprint,
    ) is None
