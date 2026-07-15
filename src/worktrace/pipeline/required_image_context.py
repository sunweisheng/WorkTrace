from __future__ import annotations

from dataclasses import replace

from ..models import AnchorUnit, AttachmentTextBlock, NormalizedMessage
from ..reaction_catalog import ReactionCatalog, enrich_message_reactions
from ..resolvers.base import ContentResolver
from ..sources.base import ChatSource


def enrich_required_image_context(
    anchor_units: list[AnchorUnit],
    *,
    self_open_id: str,
    chat_source: ChatSource,
    content_resolver: ContentResolver,
    reaction_catalog: ReactionCatalog | None = None,
) -> list[AnchorUnit]:
    """Attach required image summaries for self messages and their direct parents."""
    messages_by_conversation: dict[str, dict[str, NormalizedMessage]] = {}
    parent_ids_by_conversation: dict[str, set[str]] = {}
    for unit in anchor_units:
        known = messages_by_conversation.setdefault(unit.conversation_id, {})
        known.update({message.message_id: message for message in unit.messages})
        for message in unit.messages:
            if message.sender_open_id != self_open_id:
                continue
            parent_ids_by_conversation.setdefault(unit.conversation_id, set()).update(
                relation_id
                for relation_id in (
                    message.reply_to_message_id,
                    message.quote_message_id,
                )
                if relation_id
            )

    fetch_by_ids = getattr(chat_source, "fetch_messages_by_ids", None)
    if callable(fetch_by_ids):
        for conversation_id, parent_ids in parent_ids_by_conversation.items():
            known = messages_by_conversation[conversation_id]
            missing_ids = sorted(parent_ids.difference(known))
            if not missing_ids:
                continue
            parents = fetch_by_ids(conversation_id, missing_ids)
            if reaction_catalog is not None:
                parents = enrich_message_reactions(parents, reaction_catalog)
            known.update({message.message_id: message for message in parents})

    enriched: list[AnchorUnit] = []
    for unit in anchor_units:
        known = messages_by_conversation[unit.conversation_id]
        selected_by_id = {message.message_id: message for message in unit.messages}
        original_message_ids = set(selected_by_id)
        required_message_ids: set[str] = set()
        for message in unit.messages:
            if message.sender_open_id != self_open_id:
                continue
            if _image_attachment_ids(message):
                required_message_ids.add(message.message_id)
            for relation_id in (message.reply_to_message_id, message.quote_message_id):
                parent = known.get(relation_id or "")
                if parent is None or not _image_attachment_ids(parent):
                    continue
                selected_by_id.setdefault(parent.message_id, parent)
                required_message_ids.add(parent.message_id)

        attachment_blocks: dict[tuple[str, str], AttachmentTextBlock] = {
            (block.message_id, block.attachment_id): block
            for block in unit.attachment_texts
        }
        for message_id in sorted(required_message_ids):
            message = selected_by_id[message_id]
            for block in content_resolver.load_required_image_summaries(
                message,
                _image_attachment_ids(message),
            ) or []:
                attachment_blocks.setdefault((block.message_id, block.attachment_id), block)

        added_relation_ids = [
            message_id
            for message_id in selected_by_id
            if message_id not in original_message_ids
        ]
        enriched.append(
            replace(
                unit,
                messages=sorted(
                    selected_by_id.values(),
                    key=lambda item: (item.send_time, item.message_id),
                ),
                relation_context_message_ids=list(
                    dict.fromkeys([*unit.relation_context_message_ids, *added_relation_ids])
                ),
                attachment_texts=sorted(
                    attachment_blocks.values(),
                    key=lambda item: (item.message_id, item.attachment_id),
                ),
            )
        )
    return enriched


def _image_attachment_ids(message: NormalizedMessage) -> list[str]:
    if message.message_type in {"image", "media", "post"}:
        return [attachment.attachment_id for attachment in message.attachments]
    return [
        attachment.attachment_id
        for attachment in message.attachments
        if attachment.mime_type.startswith("image/")
    ]
