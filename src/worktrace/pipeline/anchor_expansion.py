from __future__ import annotations

from dataclasses import replace

from ..config import RuntimeConfig
from ..constants import ContextDirection, ContextRequestType
from ..models import (
    AnchorUnit,
    AttachmentTextBlock,
    ContextRequest,
    LinkedFileTextBlock,
    NormalizedMessage,
)
from ..resolvers.base import ContentResolver
from ..reaction_catalog import ReactionCatalog, enrich_message_reactions
from ..sources.base import ChatSource


def _normalize_valid_message_ids(message_ids: list[str]) -> list[str]:
    valid: list[str] = []
    seen: set[str] = set()
    for message_id in message_ids:
        if not isinstance(message_id, str):
            continue
        normalized = message_id.strip()
        if not normalized.startswith("om_"):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        valid.append(normalized)
    return valid


def validate_anchor_context_request(request: ContextRequest) -> bool:
    valid_message_ids = _normalize_valid_message_ids(request.target_message_ids)
    if not valid_message_ids:
        return False
    if request.request_type not in {
        ContextRequestType.EARLIER_MESSAGES.value,
        ContextRequestType.LATER_MESSAGES.value,
        ContextRequestType.ATTACHMENT_TEXT.value,
        ContextRequestType.LINKED_FILE_TEXT.value,
    }:
        return False
    if request.request_type == ContextRequestType.ATTACHMENT_TEXT.value:
        return bool(request.target_attachment_ids) and not request.target_link_ids
    if request.request_type == ContextRequestType.LINKED_FILE_TEXT.value:
        return bool(request.target_link_ids) and not request.target_attachment_ids
    return not request.target_attachment_ids and not request.target_link_ids


def expand_anchor_unit_context(
    anchor_unit: AnchorUnit,
    requests: list[ContextRequest],
    *,
    chat_source: ChatSource,
    content_resolver: ContentResolver,
    config: RuntimeConfig,
    reaction_catalog: ReactionCatalog | None = None,
    existing_attachment_texts: list[AttachmentTextBlock] | None = None,
    existing_linked_file_texts: list[LinkedFileTextBlock] | None = None,
) -> tuple[
    AnchorUnit,
    list[NormalizedMessage],
    list[AttachmentTextBlock],
    list[AttachmentTextBlock],
    list[LinkedFileTextBlock],
    list[LinkedFileTextBlock],
]:
    if not requests:
        existing = list(existing_attachment_texts or [])
        existing_links = list(existing_linked_file_texts or [])
        return anchor_unit, [], existing, [], existing_links, []

    request_order = {
        ContextRequestType.ATTACHMENT_TEXT.value: 0,
        ContextRequestType.LINKED_FILE_TEXT.value: 1,
        ContextRequestType.EARLIER_MESSAGES.value: 2,
        ContextRequestType.LATER_MESSAGES.value: 3,
    }
    ordered = sorted(
        [item for item in requests if validate_anchor_context_request(item)],
        key=lambda item: request_order[item.request_type],
    )

    message_by_id = {message.message_id: message for message in anchor_unit.messages}
    attachment_blocks: dict[str, AttachmentTextBlock] = {
        block.attachment_id: block for block in (existing_attachment_texts or [])
    }
    linked_file_blocks: dict[str, LinkedFileTextBlock] = {
        block.link_id: block for block in (existing_linked_file_texts or [])
    }
    new_message_ids: list[str] = []
    new_attachment_ids: list[str] = []
    new_link_ids: list[str] = []

    for request in ordered:
        valid_message_ids = _normalize_valid_message_ids(request.target_message_ids)
        if not valid_message_ids:
            continue

        if request.request_type == ContextRequestType.ATTACHMENT_TEXT.value:
            for message_id in valid_message_ids:
                message = message_by_id.get(message_id)
                if not message:
                    continue
                loaded = content_resolver.load_attachment_text_if_needed(
                    message,
                    request.target_attachment_ids,
                    request.reason,
                )
                for block in loaded or []:
                    if block.attachment_id not in attachment_blocks:
                        new_attachment_ids.append(block.attachment_id)
                    attachment_blocks.setdefault(block.attachment_id, block)
            continue
        if request.request_type == ContextRequestType.LINKED_FILE_TEXT.value:
            for message_id in valid_message_ids:
                message = message_by_id.get(message_id)
                if not message:
                    continue
                loaded = content_resolver.load_link_text_if_needed(
                    message,
                    request.target_link_ids,
                    request.reason,
                )
                for block in loaded or []:
                    if block.link_id not in linked_file_blocks:
                        new_link_ids.append(block.link_id)
                    linked_file_blocks.setdefault(block.link_id, block)
            continue

        direction = (
            ContextDirection.EARLIER
            if request.request_type == ContextRequestType.EARLIER_MESSAGES.value
            else ContextDirection.LATER
        )
        related = chat_source.fetch_related_messages(
            anchor_unit.conversation_id,
            valid_message_ids,
            direction,
            min(request.limit, config.max_model_input_tokens),
        )
        if reaction_catalog is not None:
            related = enrich_message_reactions(related, reaction_catalog)
        for message in related:
            if message.message_id not in message_by_id:
                new_message_ids.append(message.message_id)
            message_by_id.setdefault(message.message_id, message)

    updated_messages = sorted(
        message_by_id.values(),
        key=lambda item: (item.send_time, item.message_id),
    )
    updated_anchor_unit = replace(
        anchor_unit,
        in_day_message_ids=[item.message_id for item in updated_messages],
        messages=updated_messages,
    )
    new_messages = [message_by_id[item] for item in new_message_ids if item in message_by_id]
    all_attachment_texts = sorted(
        attachment_blocks.values(),
        key=lambda item: (item.message_id, item.attachment_id),
    )
    all_linked_file_texts = sorted(
        linked_file_blocks.values(),
        key=lambda item: (item.message_id, item.link_id),
    )
    new_attachment_texts = [
        block for block in all_attachment_texts if block.attachment_id in set(new_attachment_ids)
    ]
    new_linked_file_texts = [
        block for block in all_linked_file_texts if block.link_id in set(new_link_ids)
    ]
    return (
        updated_anchor_unit,
        new_messages,
        all_attachment_texts,
        new_attachment_texts,
        all_linked_file_texts,
        new_linked_file_texts,
    )
