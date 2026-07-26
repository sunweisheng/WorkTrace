from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence
import unicodedata

from ..errors import AnalyzerProtocolError
from ..models import (
    CrossConversationGroup,
    CrossConversationGroupResult,
    DayGroupDiscoveryResult,
    DayGroupReviewResult,
    NormalizedMessage,
    SourceBackedEventDraft,
)
from ..utils.hashing import file_key_from_attachment_id, file_key_from_url
from ..utils.link_refs import build_message_link_candidates
from ..utils.text import choose_preferred_text, clean_text, combine_group_titles
from .validation import validate_cross_conversation_groups


@dataclass(frozen=True)
class DayGroupReviewComponent:
    component_id: str
    groups: list[CrossConversationGroup]
    candidates: list[SourceBackedEventDraft]
    relation_reasons: list[dict[str, object]]
    relation_sources: list[str]


def build_day_group_discovery_groups(
    groups: list[CrossConversationGroup],
    candidates: list[SourceBackedEventDraft],
) -> list[dict[str, str]]:
    candidate_by_id = {item.draft_id: item for item in candidates}
    result: list[dict[str, str]] = []
    for group in groups:
        members = [
            candidate_by_id[draft_id]
            for draft_id in group.draft_ids
            if draft_id in candidate_by_id
        ]
        primary = candidate_by_id.get(group.primary_draft_id)
        primary_title = clean_text(primary.topic) if primary is not None else ""
        member_titles = [item.topic for item in members]
        title = combine_group_titles(primary_title, member_titles)
        if not title:
            title = choose_preferred_text(member_titles)
        result.append({"group_id": group.group_id, "title": title})
    return result


def build_day_group_review_typical_arguments(
    component: DayGroupReviewComponent,
) -> dict[str, object]:
    candidate_by_id = {item.draft_id: item for item in component.candidates}
    group_by_id = {item.group_id: item for item in component.groups}
    fallback_message_ids = [
        message_id
        for candidate in component.candidates
        for message_id in candidate.source_message_ids
    ]

    resolutions: list[dict[str, object]] = []
    for relation in component.relation_reasons:
        representative_draft_ids: list[str] = []
        left_draft_id = str(relation.get("left_draft_id", "")).strip()
        right_draft_id = str(relation.get("right_draft_id", "")).strip()
        if left_draft_id and right_draft_id:
            representative_draft_ids.extend([left_draft_id, right_draft_id])
        else:
            for group_id in relation.get("group_ids", []):
                group = group_by_id.get(str(group_id))
                if group is not None and group.draft_ids:
                    representative_draft_ids.append(group.draft_ids[0])
        representative_draft_ids = list(dict.fromkeys(representative_draft_ids))

        evidence_message_ids = list(
            dict.fromkeys(
                candidate_by_id[draft_id].source_message_ids[0]
                for draft_id in representative_draft_ids
                if draft_id in candidate_by_id
                and candidate_by_id[draft_id].source_message_ids
            )
        )
        if not evidence_message_ids and fallback_message_ids:
            evidence_message_ids = [fallback_message_ids[0]]
        resolutions.append(
            {
                "relation_id": str(relation["relation_id"]),
                "decision": "separate",
                "connected_draft_ids": representative_draft_ids,
                "reason": "结构占位理由，不代表当前关系应当分开。",
                "evidence_message_ids": evidence_message_ids,
            }
        )

    return {
        "relation_resolutions": resolutions,
        "merged_groups": [],
        "singleton_draft_ids": [item.draft_id for item in component.candidates],
    }


def build_day_group_review_components(
    groups: list[CrossConversationGroup],
    candidates: list[SourceBackedEventDraft],
    messages: list[NormalizedMessage],
    *,
    discovery_result: DayGroupDiscoveryResult | None = None,
    attachment_version_suffix_patterns: Sequence[str] = (),
    attachment_ignored_mime_type_prefixes: Sequence[str] = (),
) -> list[DayGroupReviewComponent]:
    candidate_by_id = {item.draft_id: item for item in candidates}
    candidate_order = {item.draft_id: index for index, item in enumerate(candidates)}
    message_by_id = {item.message_id: item for item in messages}
    file_keys_by_draft = {
        item.draft_id: _candidate_file_keys(item, message_by_id)
        for item in candidates
    }
    attachment_by_id = {
        attachment.attachment_id: attachment
        for message in messages
        for attachment in message.attachments
        if attachment.attachment_id
    }
    compiled_version_patterns = [
        re.compile(pattern) for pattern in attachment_version_suffix_patterns
    ]
    attachment_base_names_by_draft = {
        item.draft_id: _candidate_attachment_base_names(
            item,
            attachment_by_id=attachment_by_id,
            version_suffix_patterns=compiled_version_patterns,
            ignored_mime_type_prefixes=attachment_ignored_mime_type_prefixes,
        )
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
                attachment_base_names_by_draft=attachment_base_names_by_draft,
            )
            if not reasons:
                continue
            adjacency[left_index].add(right_index)
            adjacency[right_index].add(left_index)
            relations_by_pair[(left_index, right_index)] = reasons

    group_index_by_id = {
        group.group_id: index for index, group in enumerate(groups)
    }
    discovery = discovery_result or DayGroupDiscoveryResult()
    discovery_reasons: list[tuple[set[int], dict[str, object]]] = []
    discovery_pair_reasons: dict[tuple[int, int], list[str]] = {}
    for check in discovery.group_checks:
        source_index = group_index_by_id.get(check.group_id)
        if source_index is None:
            continue
        for related_group_id in check.related_group_ids:
            related_index = group_index_by_id.get(related_group_id)
            if related_index is None or related_index == source_index:
                continue
            pair = tuple(sorted((source_index, related_index)))
            reasons = discovery_pair_reasons.setdefault(pair, [])
            reason = clean_text(check.reason)
            if reason and reason not in reasons:
                reasons.append(reason)

    if discovery_pair_reasons:
        for (left_index, right_index), reasons in discovery_pair_reasons.items():
            adjacency[left_index].add(right_index)
            adjacency[right_index].add(left_index)
            discovery_reasons.append(
                (
                    {left_index, right_index},
                    {
                        "relation_types": ["title_discovery"],
                        "group_ids": [
                            groups[left_index].group_id,
                            groups[right_index].group_id,
                        ],
                        "reason": "；".join(reasons),
                    },
                )
            )
    else:
        # Older traces only contain merged candidate groups, so retain their
        # original multi-group relation shape when replaying them.
        for candidate_group in discovery.candidate_groups:
            indexes = [
                group_index_by_id[group_id]
                for group_id in candidate_group.group_ids
                if group_id in group_index_by_id
            ]
            if len(indexes) < 2:
                continue
            anchor = indexes[0]
            for index in indexes[1:]:
                adjacency[anchor].add(index)
                adjacency[index].add(anchor)
            discovery_reasons.append(
                (
                    set(indexes),
                    {
                        "relation_types": ["title_discovery"],
                        "group_ids": list(candidate_group.group_ids),
                        "reason": candidate_group.reason,
                    },
                )
            )

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
        relation_reasons.extend(
            reason
            for reason_indexes, reason in discovery_reasons
            if reason_indexes.issubset(indexes)
        )
        relation_types = {
            str(relation_type)
            for reason in relation_reasons
            for relation_type in reason.get("relation_types", [])
        }
        relation_sources = []
        if relation_types.difference(
            {"same_attachment_base_name", "title_discovery"}
        ):
            relation_sources.append("structural_relation")
        if "same_attachment_base_name" in relation_types:
            relation_sources.append("same_attachment_base_name")
        if "title_discovery" in relation_types:
            relation_sources.append("title_discovery")
        component_id = f"day-group-review-{len(components) + 1:03d}"
        numbered_relation_reasons = [
            {
                "relation_id": f"{component_id}-relation-{index:03d}",
                **reason,
            }
            for index, reason in enumerate(relation_reasons, start=1)
        ]
        components.append(
            DayGroupReviewComponent(
                component_id=component_id,
                groups=component_groups,
                candidates=component_candidates,
                relation_reasons=numbered_relation_reasons,
                relation_sources=relation_sources,
            )
        )
    return components


def validate_day_group_review_result(
    result: DayGroupReviewResult,
    component: DayGroupReviewComponent,
) -> DayGroupReviewResult:
    validated = validate_cross_conversation_groups(
        result.grouping_result,
        component.candidates,
    )
    returned_group_by_draft = {
        draft_id: index
        for index, group in enumerate(validated.groups)
        for draft_id in group.draft_ids
    }
    candidate_by_id = {item.draft_id: item for item in component.candidates}
    original_group_by_id = {item.group_id: item for item in component.groups}
    relation_by_id = {
        str(item.get("relation_id", "")): item
        for item in component.relation_reasons
    }
    resolution_by_id = {
        item.relation_id: item for item in result.relation_resolutions
    }
    if set(resolution_by_id) != set(relation_by_id):
        raise AnalyzerProtocolError(
            "Day group review must resolve every relation exactly once."
        )

    for relation_id, relation in relation_by_id.items():
        resolution = resolution_by_id[relation_id]
        structural_ids = [
            str(relation.get(field, ""))
            for field in ("left_draft_id", "right_draft_id")
            if str(relation.get(field, ""))
        ]
        related_group_ids = [
            str(value) for value in relation.get("group_ids", [])
        ]
        related_groups = [
            original_group_by_id[group_id]
            for group_id in related_group_ids
            if group_id in original_group_by_id
        ]
        if structural_ids:
            required_sides = [[draft_id] for draft_id in structural_ids]
            relation_is_joined = len(
                {returned_group_by_draft[draft_id] for draft_id in structural_ids}
            ) == 1
        else:
            required_sides = [list(group.draft_ids) for group in related_groups]
            relation_is_joined = _spans_original_groups_in_one_result(
                related_groups,
                returned_group_by_draft=returned_group_by_draft,
            )

        evidence_ids = set(resolution.evidence_message_ids)
        evidence_sides = required_sides
        if resolution.connected_draft_ids:
            evidence_sides = [
                [draft_id]
                for draft_id in resolution.connected_draft_ids
            ]
        missing_evidence_sides = [
            side
            for side in evidence_sides
            if not any(
                evidence_ids.intersection(candidate_by_id[draft_id].source_message_ids)
                for draft_id in side
                if draft_id in candidate_by_id
            )
        ]
        if missing_evidence_sides:
            raise AnalyzerProtocolError(
                "Day group review relation resolution evidence must cover every "
                f"relation side: relation_id={relation_id}."
            )

        if resolution.decision == "merged":
            if len(resolution.connected_draft_ids) < 2:
                raise AnalyzerProtocolError(
                    "Merged day group relation resolution requires at least two "
                    f"connected drafts: relation_id={relation_id}."
                )
            returned_indexes = {
                returned_group_by_draft[draft_id]
                for draft_id in resolution.connected_draft_ids
            }
            if len(returned_indexes) != 1 or not relation_is_joined:
                raise AnalyzerProtocolError(
                    "Merged day group relation resolution is not reflected in the "
                    f"returned groups: relation_id={relation_id}."
                )
            if structural_ids and not set(structural_ids).issubset(
                resolution.connected_draft_ids
            ):
                raise AnalyzerProtocolError(
                    "Merged structural relation must include both related drafts: "
                    f"relation_id={relation_id}."
                )
            if related_groups and not _drafts_span_original_groups(
                resolution.connected_draft_ids,
                related_groups,
            ):
                raise AnalyzerProtocolError(
                    "Merged title relation must connect drafts from different existing "
                    f"groups: relation_id={relation_id}."
                )
            continue

        if resolution.decision != "separate":
            raise AnalyzerProtocolError(
                f"Day group review relation decision is invalid: {relation_id}."
            )
        if resolution.connected_draft_ids:
            if len(resolution.connected_draft_ids) < 2:
                raise AnalyzerProtocolError(
                    "Separate day group relation resolution requires at least two "
                    f"representative drafts when provided: relation_id={relation_id}."
                )
            returned_indexes = {
                returned_group_by_draft[draft_id]
                for draft_id in resolution.connected_draft_ids
            }
            if len(returned_indexes) < 2:
                raise AnalyzerProtocolError(
                    "Separate day group relation representatives must remain in "
                    f"different returned groups: relation_id={relation_id}."
                )
            if structural_ids and not set(structural_ids).issubset(
                resolution.connected_draft_ids
            ):
                raise AnalyzerProtocolError(
                    "Separate structural relation representatives must include both "
                    f"related drafts: relation_id={relation_id}."
                )
            if related_groups and not _drafts_span_original_groups(
                resolution.connected_draft_ids,
                related_groups,
            ):
                raise AnalyzerProtocolError(
                    "Separate title relation representatives must come from different "
                    f"existing groups: relation_id={relation_id}."
                )
            continue
        if relation_is_joined:
            raise AnalyzerProtocolError(
                "Separate day group relation resolution contradicts the returned "
                f"groups: relation_id={relation_id}."
            )

    return DayGroupReviewResult(
        grouping_result=validated,
        relation_resolutions=list(result.relation_resolutions),
    )


def _spans_original_groups_in_one_result(
    original_groups: list[CrossConversationGroup],
    *,
    returned_group_by_draft: dict[str, int],
) -> bool:
    result_indexes_by_original = [
        {returned_group_by_draft[draft_id] for draft_id in group.draft_ids}
        for group in original_groups
    ]
    return any(
        left_indexes.intersection(right_indexes)
        for left_index, left_indexes in enumerate(result_indexes_by_original)
        for right_indexes in result_indexes_by_original[left_index + 1 :]
    )


def _drafts_span_original_groups(
    draft_ids: list[str],
    original_groups: list[CrossConversationGroup],
) -> bool:
    selected = set(draft_ids)
    covered_group_count = sum(
        bool(selected.intersection(group.draft_ids)) for group in original_groups
    )
    return covered_group_count >= 2


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
    attachment_base_names_by_draft: dict[str, set[str]],
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
            shared_attachment_base_names = sorted(
                attachment_base_names_by_draft[left_id]
                & attachment_base_names_by_draft[right_id]
            )
            if shared_attachment_base_names:
                relation_types.append("same_attachment_base_name")
            if relation_types:
                reasons.append(
                    {
                        "left_draft_id": left_id,
                        "right_draft_id": right_id,
                        "relation_types": relation_types,
                        "shared_message_ids": shared_message_ids,
                        "shared_file_keys": shared_file_keys,
                        "shared_attachment_base_names": (
                            shared_attachment_base_names
                        ),
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


def normalize_attachment_base_name(
    file_name: str,
    version_suffix_patterns: Sequence[str | re.Pattern[str]],
) -> str:
    normalized = unicodedata.normalize("NFKC", file_name or "").strip().casefold()
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized:
        return ""
    if "." in normalized and not normalized.startswith("."):
        stem, extension = normalized.rsplit(".", 1)
        extension = f".{extension.strip()}"
    else:
        stem = normalized
        extension = ""
    compiled_patterns = [
        pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)
        for pattern in version_suffix_patterns
    ]
    changed = True
    while changed and stem:
        changed = False
        for pattern in compiled_patterns:
            updated = pattern.sub("", stem).strip(" -_()（）[]【】")
            if updated != stem:
                stem = updated
                changed = True
    stem = re.sub(r"\s+", " ", stem).strip()
    return f"{stem}{extension}" if stem else ""


def _candidate_attachment_base_names(
    candidate: SourceBackedEventDraft,
    *,
    attachment_by_id: dict[str, object],
    version_suffix_patterns: Sequence[re.Pattern[str]],
    ignored_mime_type_prefixes: Sequence[str],
) -> set[str]:
    ignored_prefixes = tuple(
        prefix.strip().casefold()
        for prefix in ignored_mime_type_prefixes
        if prefix.strip()
    )
    names: set[str] = set()
    for attachment_id in candidate.referenced_attachment_ids:
        attachment = attachment_by_id.get(attachment_id)
        if attachment is None:
            continue
        mime_type = str(getattr(attachment, "mime_type", "")).casefold()
        if ignored_prefixes and mime_type.startswith(ignored_prefixes):
            continue
        normalized = normalize_attachment_base_name(
            str(getattr(attachment, "file_name", "")),
            version_suffix_patterns,
        )
        if normalized:
            names.add(normalized)
    return names
