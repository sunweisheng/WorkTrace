from __future__ import annotations

from pathlib import Path

from src.worktrace.analyzers.prompts import serialize_slice_for_prompt
from src.worktrace.config import RuntimeConfig
from src.worktrace.models import ConversationSlice, NormalizedMessage
from src.worktrace.pipeline.batching import build_analysis_batches, estimate_slice_tokens


def _slice(slice_id: str, text: str) -> ConversationSlice:
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id=f"{slice_id}-m1",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text=text,
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    return ConversationSlice(
        slice_id=slice_id,
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=[message.message_id],
        in_day_message_ids=[message.message_id],
        messages=[message],
        attachment_texts=[],
    )


def test_batching_respects_slice_limit(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data", batch_slice_limit=2, batch_target_tokens=10000)
    batches = build_analysis_batches("2026-06-22", [_slice("s1", "a"), _slice("s2", "b"), _slice("s3", "c")], config)
    assert len(batches) == 2
    assert len(batches[0].slices) == 2


def test_batching_estimate_uses_prompt_payload(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_message_char_limit=10,
        prompt_slice_message_limit=1,
    )
    conversation_slice = _slice("s1", "abcdefghijklmnopqrstuvwxyz")
    serialized = serialize_slice_for_prompt(conversation_slice, config)
    estimated = estimate_slice_tokens(conversation_slice, config)

    assert serialized["messages"][0]["text"].endswith("...")
    assert estimated > 50
