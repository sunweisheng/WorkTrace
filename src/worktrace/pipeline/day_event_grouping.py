from __future__ import annotations

from dataclasses import dataclass

from ..errors import AnalyzerProtocolError
from ..models import (
    CrossConversationGroup,
    CrossConversationGroupResult,
    NormalizedMessage,
    SourceBackedEventDraft,
)
from ..utils.hashing import file_key_from_attachment_id, file_key_from_url
from ..utils.link_refs import build_message_link_candidates
from .validation import validate_cross_conversation_groups


@dataclass(frozen=True)
class DayGroupReviewComponent:
    component_id: str
    groups: list[CrossConversationGroup]
    candidates: list[SourceBackedEventDraft]
    relation_reasons: list[dict[str, object]]


def build_day_group_review_components(
    groups: list[CrossConversationGroup],
    candidates: list[SourceBackedEventDraft],
    messages: list[NormalizedMessage],
) -> list[DayGroupReviewComponent]:
    candidate_by_id = {item.draft_id: item for item in candidates}
    candidate_order = {item.draft_id: index for index, item in enumerate(candidates)}
    message_by_id = {item.message_id: item for item in messages}
    file_keys_by_draft = {
        item.draft_id: _candidate_file_keys(item, message_by_id)
        for item in candidates
    }
    adjacency: dict[int, set[int]] = {index: set() for index in range(len(groups))}
    relations_by_pair: dict[tuple[int, int], list[dict[str, object]]] = {}

    for left_index, left_group in enumerate(groups):
        for right_index in range(left_index + 1, len(groups)):
            right_group = groups[right_index]
            reasons = _group_relation_reasons(
                left_group,
                right_group,
                candidate_by_id=candidate_by_id,
                message_by_id=message_by_id,
                file_keys_by_draft=file_keys_by_draft,
            )
            if not reasons:
                continue
            adjacency[left_index].add(right_index)
            adjacency[right_index].add(left_index)
            relations_by_pair[(left_index, right_index)] = reasons

    components: list[DayGroupReviewComponent] = []
    visited: set[int] = set()
    for start_index in range(len(groups)):
        if start_index in visited or not adjacency[start_index]:
            continue
        stack = [start_index]
        indexes: list[int] = []
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            indexes.append(current)
            stack.extend(sorted(adjacency[current] - visited, reverse=True))
        indexes.sort()
        component_groups = [groups[index] for index in indexes]
        draft_ids = [
            draft_id
            for group in component_groups
            for draft_id in group.draft_ids
        ]
        component_candidates = [candidate_by_id[draft_id] for draft_id in draft_ids]
        component_candidates.sort(key=lambda item: candidate_order[item.draft_id])
        relation_reasons = [
            reason
            for (left_index, right_index), reasons in relations_by_pair.items()
            if left_index in indexes and right_index in indexes
            for reason in reasons
        ]
        components.append(
            DayGroupReviewComponent(
                component_id=f"day-group-review-{len(components) + 1:03d}",
                groups=component_groups,
                candidates=component_candidates,
                relation_reasons=relation_reasons,
            )
        )
    return components


def validate_day_group_review_result(
    result: CrossConversationGroupResult,
    component: DayGroupReviewComponent,
) -> CrossConversationGroupResult:
    validated = validate_cross_conversation_groups(result, component.candidates)
    returned_group_by_draft = {
        draft_id: index
        for index, group in enumerate(validated.groups)
        for draft_id in group.draft_ids
    }
    for original_group in component.groups:
        returned_indexes = {
            returned_group_by_draft[draft_id]
            for draft_id in original_group.draft_ids
        }
        if len(returned_indexes) != 1:
            raise AnalyzerProtocolError(
                "Day group review must not split an existing legal group: "
                f"{original_group.group_id}."
            )
    return validated


def replace_reviewed_day_group_components(
    groups: list[CrossConversationGroup],
    replacements: dict[str, CrossConversationGroupResult],
    components: list[DayGroupReviewComponent],
    candidates: list[SourceBackedEventDraft],
) -> list[CrossConversationGroup]:
    replacement_by_original_group: dict[str, list[CrossConversationGroup]] = {}
    for component in components:
        result = replacements.get(component.component_id)
        if result is None:
            continue
        replacement_groups = list(result.groups)
        for original_group in component.groups:
            replacement_by_original_group[original_group.group_id] = replacement_groups

    emitted_replacements: set[int] = set()
    combined: list[CrossConversationGroup] = []
    for group in groups:
        replacement = replacement_by_original_group.get(group.group_id)
        if replacement is None:
            combined.append(group)
            continue
        identity = id(replacement)
        if identity in emitted_replacements:
            continue
        emitted_replacements.add(identity)
        combined.extend(replacement)

    candidate_order = {item.draft_id: index for index, item in enumerate(candidates)}
    combined.sort(
        key=lambda item: min(candidate_order[draft_id] for draft_id in item.draft_ids)
    )
    return [
        CrossConversationGroup(
            group_id=f"group-{index:03d}",
            draft_ids=list(group.draft_ids),
            primary_draft_id=group.primary_draft_id,
            merge_reason=group.merge_reason,
            evidence_message_ids=list(group.evidence_message_ids),
        )
        for index, group in enumerate(combined, start=1)
    ]


def _group_relation_reasons(
    left_group: CrossConversationGroup,
    right_group: CrossConversationGroup,
    *,
    candidate_by_id: dict[str, SourceBackedEventDraft],
    message_by_id: dict[str, NormalizedMessage],
    file_keys_by_draft: dict[str, set[str]],
) -> list[dict[str, object]]:
    reasons: list[dict[str, object]] = []
    for left_id in left_group.draft_ids:
        left = candidate_by_id[left_id]
        left_messages = set(left.source_message_ids)
        for right_id in right_group.draft_ids:
            right = candidate_by_id[right_id]
            right_messages = set(right.source_message_ids)
            relation_types: list[str] = []
            shared_message_ids = sorted(left_messages & right_messages)
            if shared_message_ids:
                relation_types.append("shared_message")
            if left.source_slice_id and left.source_slice_id == right.source_slice_id:
                relation_types.append("same_source_slice")
            if _has_direct_message_relation(
                left_messages,
                right_messages,
                message_by_id,
            ):
                relation_types.append("direct_reply_or_quote")
            shared_file_keys = sorted(
                file_keys_by_draft[left_id] & file_keys_by_draft[right_id]
            )
            if shared_file_keys:
                relation_types.append("shared_file")
            if relation_types:
                reasons.append(
                    {
                        "left_draft_id": left_id,
                        "right_draft_id": right_id,
                        "relation_types": relation_types,
                        "shared_message_ids": shared_message_ids,
                        "shared_file_keys": shared_file_keys,
                    }
                )
    return reasons


def _has_direct_message_relation(
    left_ids: set[str],
    right_ids: set[str],
    message_by_id: dict[str, NormalizedMessage],
) -> bool:
    for message_id in left_ids | right_ids:
        message = message_by_id.get(message_id)
        if message is None:
            continue
        related_ids = {message.reply_to_message_id, message.quote_message_id}
        if message_id in left_ids and related_ids.intersection(right_ids):
            return True
        if message_id in right_ids and related_ids.intersection(left_ids):
            return True
    return False


def _candidate_file_keys(
    candidate: SourceBackedEventDraft,
    message_by_id: dict[str, NormalizedMessage],
) -> set[str]:
    keys = {
        key
        for attachment_id in candidate.referenced_attachment_ids
        if (key := file_key_from_attachment_id(attachment_id))
    }
    referenced_link_ids = set(candidate.referenced_link_ids)
    for message_id in candidate.source_message_ids:
        message = message_by_id.get(message_id)
        if message is None:
            continue
        for link in build_message_link_candidates(message):
            if link.link_id not in referenced_link_ids:
                continue
            if key := file_key_from_url(link.url):
                keys.add(key)
    return keys
