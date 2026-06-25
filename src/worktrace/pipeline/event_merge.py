from __future__ import annotations

from collections import defaultdict

from ..models import (
    BucketMergedDraft,
    CrossBucketMergeResult,
    CrossMergeBucketResult,
    MergedEventDraft,
    SourceBackedEventDraft,
    WorkEvent,
)
from ..utils.hashing import stable_event_id
from ..utils.text import choose_preferred_text, merge_content_texts


def merge_duplicate_drafts(
    drafts: list[MergedEventDraft],
) -> tuple[list[MergedEventDraft], list[str]]:
    grouped: dict[tuple[str, ...], list[MergedEventDraft]] = defaultdict(list)
    warnings: list[str] = []

    for draft in drafts:
        grouped[tuple(draft.source_message_ids)].append(draft)

    merged: list[MergedEventDraft] = []
    for message_ids, items in grouped.items():
        results = [item.result for item in items if item.result.strip()]
        if len(set(results)) > 1:
            warnings.append(
                f"Conflicting results for source set {','.join(message_ids)}; kept first preferred value."
            )

        merged.append(
            MergedEventDraft(
                date=items[0].date,
                topic=choose_preferred_text([item.topic for item in items]),
                content=merge_content_texts([item.content for item in items]),
                result=choose_preferred_text([item.result for item in items]),
                source_message_ids=list(message_ids),
                source_conversation_ids=sorted(
                    {cid for item in items for cid in item.source_conversation_ids}
                ),
            )
        )

    return merged, warnings


def build_work_events(
    target_date: str,
    drafts: list[MergedEventDraft],
) -> tuple[list[WorkEvent], list[str]]:
    merged_drafts, warnings = merge_duplicate_drafts(drafts)
    events: list[WorkEvent] = []
    seen_event_ids: set[str] = set()

    for draft in merged_drafts:
        event_id = stable_event_id(target_date, draft.source_message_ids)
        if event_id in seen_event_ids:
            raise ValueError(f"Unresolvable event_id collision: {event_id}")
        seen_event_ids.add(event_id)
        events.append(
            WorkEvent(
                date=target_date,
                event_id=event_id,
                topic=draft.topic,
                content=draft.content,
                result=draft.result,
            )
        )

    return sorted(events, key=lambda item: item.event_id), warnings


def materialize_cross_merge_buckets(
    candidates: list[SourceBackedEventDraft],
    bucket_result: CrossMergeBucketResult,
) -> list[list[SourceBackedEventDraft]]:
    if not candidates:
        return []

    draft_map = {candidate.draft_id: candidate for candidate in candidates}
    seen: set[str] = set()
    buckets: list[list[SourceBackedEventDraft]] = []

    for bucket in bucket_result.buckets:
        materialized: list[SourceBackedEventDraft] = []
        for draft_id in bucket.draft_ids:
            candidate = draft_map.get(draft_id)
            if candidate is None or draft_id in seen:
                continue
            seen.add(draft_id)
            materialized.append(candidate)
        if materialized:
            buckets.append(materialized)

    for candidate in candidates:
        if candidate.draft_id not in seen:
            buckets.append([candidate])
            seen.add(candidate.draft_id)

    return buckets


def try_merge_bucket_locally(
    bucket: list[SourceBackedEventDraft],
) -> list[MergedEventDraft] | None:
    if len(bucket) == 1:
        item = bucket[0]
        return [
            MergedEventDraft(
                date=item.date,
                topic=item.topic,
                content=item.content,
                result=item.result,
                source_message_ids=list(item.source_message_ids),
                source_conversation_ids=[item.source_conversation_id],
            )
        ]

    if len(bucket) != 2:
        return None

    left, right = bucket
    if not _should_merge_bucket_pair_locally(left, right):
        return None

    return [
        MergedEventDraft(
            date=left.date,
            topic=choose_preferred_text([left.topic, right.topic]),
            content=merge_content_texts([left.content, right.content]),
            result=choose_preferred_text([left.result, right.result]),
            source_message_ids=sorted(set(left.source_message_ids + right.source_message_ids)),
            source_conversation_ids=sorted(
                {left.source_conversation_id, right.source_conversation_id}
            ),
        )
    ]


def build_bucket_merged_drafts(
    bucket_ids: list[str],
    bucket_candidates: list[list[SourceBackedEventDraft]],
    bucket_merged_lists: list[list[MergedEventDraft]],
) -> list[BucketMergedDraft]:
    result: list[BucketMergedDraft] = []
    for bucket_id, candidates, merged_items in zip(
        bucket_ids,
        bucket_candidates,
        bucket_merged_lists,
        strict=False,
    ):
        upstream_ids = sorted({item.draft_id for item in candidates})
        for merged in merged_items:
            result.append(
                BucketMergedDraft(
                    bucket_id=bucket_id,
                    draft=merged,
                    upstream_draft_ids=upstream_ids,
                )
            )
    return result


def select_cross_bucket_candidate_pairs(
    merged_buckets: list[BucketMergedDraft],
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for index, left in enumerate(merged_buckets):
        for right in merged_buckets[index + 1 :]:
            if _should_review_bucket_pair(left, right):
                pairs.append((left.bucket_id, right.bucket_id))
    return pairs


def materialize_cross_bucket_merge_plan(
    merged_buckets: list[BucketMergedDraft],
    decision_result: CrossBucketMergeResult,
) -> list[list[BucketMergedDraft]]:
    if not merged_buckets:
        return []

    parent = list(range(len(merged_buckets)))
    bucket_index = {item.bucket_id: index for index, item in enumerate(merged_buckets)}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for decision in decision_result.merge_decisions:
        if not decision.should_merge:
            continue
        left_index = bucket_index.get(decision.left_bucket_id)
        right_index = bucket_index.get(decision.right_bucket_id)
        if left_index is None or right_index is None:
            continue
        union(left_index, right_index)

    grouped: dict[int, list[BucketMergedDraft]] = defaultdict(list)
    for index, item in enumerate(merged_buckets):
        grouped[find(index)].append(item)
    return list(grouped.values())


def flatten_bucket_merge_groups(
    groups: list[list[BucketMergedDraft]],
) -> list[MergedEventDraft]:
    drafts: list[MergedEventDraft] = []
    for group in groups:
        if len(group) == 1:
            drafts.append(group[0].draft)
            continue
        drafts.append(
            MergedEventDraft(
                date=group[0].draft.date,
                topic=choose_preferred_text([item.draft.topic for item in group]),
                content=merge_content_texts([item.draft.content for item in group]),
                result=choose_preferred_text([item.draft.result for item in group]),
                source_message_ids=sorted(
                    {
                        message_id
                        for item in group
                        for message_id in item.draft.source_message_ids
                    }
                ),
                source_conversation_ids=sorted(
                    {
                        conversation_id
                        for item in group
                        for conversation_id in item.draft.source_conversation_ids
                    }
                ),
            )
        )
    return drafts


def _should_review_bucket_pair(
    left: BucketMergedDraft,
    right: BucketMergedDraft,
) -> bool:
    left_tokens = _bucket_review_tokens(left)
    right_tokens = _bucket_review_tokens(right)
    shared = left_tokens & right_tokens
    if len(shared) >= 2:
        return True

    if _share_bucket_suffix_hint(left, right):
        return True

    if set(left.draft.source_conversation_ids) & set(right.draft.source_conversation_ids):
        if shared:
            return True

    return False


def _bucket_review_tokens(item: BucketMergedDraft) -> set[str]:
    values = f"{item.draft.topic}\n{item.draft.content}\n{item.draft.result}"
    cleaned = "".join(char.lower() if char.isalnum() else " " for char in values)
    tokens = {token for token in cleaned.split() if len(token) >= 4}
    return tokens


def _share_bucket_suffix_hint(
    left: BucketMergedDraft,
    right: BucketMergedDraft,
) -> bool:
    left_text = f"{left.draft.topic}{left.draft.content}{left.draft.result}"
    right_text = f"{right.draft.topic}{right.draft.content}{right.draft.result}"
    for suffix in ("汇报文档", "方案", "详情页设计", "海报", "代理商", "优惠券配置", "付款"):
        if suffix in left_text and suffix in right_text:
            return True
    return False


def _should_merge_bucket_pair_locally(
    left: SourceBackedEventDraft,
    right: SourceBackedEventDraft,
) -> bool:
    if left.date != right.date:
        return False

    left_tokens = _draft_tokens(left)
    right_tokens = _draft_tokens(right)
    shared = left_tokens & right_tokens
    if len(shared) >= 2:
        return True

    if _share_draft_suffix_hint(left, right):
        return True

    return False


def _draft_tokens(item: SourceBackedEventDraft) -> set[str]:
    values = f"{item.topic}\n{item.content}\n{item.result}"
    cleaned = "".join(char.lower() if char.isalnum() else " " for char in values)
    return {token for token in cleaned.split() if len(token) >= 4}


def _share_draft_suffix_hint(
    left: SourceBackedEventDraft,
    right: SourceBackedEventDraft,
) -> bool:
    left_text = f"{left.topic}{left.content}{left.result}"
    right_text = f"{right.topic}{right.content}{right.result}"
    for suffix in (
        "汇报文档",
        "执行方案",
        "详情页设计",
        "海报",
        "代理商切换",
        "优惠券配置",
        "提前付款",
        "手续费测算",
    ):
        if suffix in left_text and suffix in right_text:
            return True
    return False
