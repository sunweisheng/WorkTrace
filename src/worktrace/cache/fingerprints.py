from __future__ import annotations

import hashlib

from ..models import AnchorUnit, AttachmentTextBlock, LinkedFileTextBlock
from ..utils.json_io import dump_json


def build_anchor_input_fingerprint(
    anchor_unit: AnchorUnit,
    *,
    attachment_texts: list[AttachmentTextBlock] | None = None,
    linked_file_texts: list[LinkedFileTextBlock] | None = None,
    prompt_version: str = "v2",
    schema_version: str = "v2",
) -> str:
    payload = {
        "anchor_unit_id": anchor_unit.anchor_unit_id,
        "conversation_id": anchor_unit.conversation_id,
        "anchor_message_ids": anchor_unit.anchor_message_ids,
        "in_day_message_ids": anchor_unit.in_day_message_ids,
        "base_message_ids": anchor_unit.base_message_ids,
        "reply_relation_ids": anchor_unit.reply_relation_ids,
        "quote_relation_ids": anchor_unit.quote_relation_ids,
        "messages": [item.to_dict() for item in anchor_unit.messages],
        "attachment_refs": [item.to_dict() for item in anchor_unit.attachment_refs],
        "anchor_signals": [item.to_dict() for item in anchor_unit.anchor_signals],
        "attachment_texts": [
            item.to_dict() for item in (attachment_texts or anchor_unit.attachment_texts)
        ],
        "linked_file_texts": [
            item.to_dict() for item in (linked_file_texts or anchor_unit.linked_file_texts)
        ],
        "prompt_version": prompt_version,
        "schema_version": schema_version,
    }
    return hashlib.sha1(dump_json(payload).encode("utf-8")).hexdigest()
