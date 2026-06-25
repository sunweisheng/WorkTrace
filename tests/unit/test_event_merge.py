from __future__ import annotations

from src.worktrace.models import (
    BucketMergedDraft,
    CrossBucketMergeDecision,
    CrossBucketMergeResult,
    CrossMergeBucket,
    CrossMergeBucketResult,
    MergedEventDraft,
    SourceBackedEventDraft,
)
from src.worktrace.pipeline.event_merge import (
    flatten_bucket_merge_groups,
    materialize_cross_bucket_merge_plan,
    materialize_cross_merge_buckets,
    select_cross_bucket_candidate_pairs,
    try_merge_bucket_locally,
)


def test_materialize_cross_merge_buckets_uses_llm_bucket_result() -> None:
    candidates = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-23",
            topic="发布排期确认",
            content="同步 release-123 的发布时间",
            result="",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
        ),
        SourceBackedEventDraft(
            draft_id="d2",
            date="2026-06-23",
            topic="发布排期确认",
            content="另一个会话里继续讨论 release-123",
            result="",
            source_message_ids=["m2"],
            source_conversation_id="c2",
            source_slice_id="s2",
            confidence=0.9,
        ),
        SourceBackedEventDraft(
            draft_id="d3",
            date="2026-06-23",
            topic="合同沟通",
            content="跟进 contract-888",
            result="",
            source_message_ids=["m3"],
            source_conversation_id="c3",
            source_slice_id="s3",
            confidence=0.9,
        ),
    ]

    buckets = materialize_cross_merge_buckets(
        candidates,
        CrossMergeBucketResult(
            buckets=[
                CrossMergeBucket(bucket_id="b1", draft_ids=["d1", "d2"], reason="same release"),
                CrossMergeBucket(bucket_id="b2", draft_ids=["d3"], reason="different contract"),
            ]
        ),
    )
    bucket_sizes = sorted(len(bucket) for bucket in buckets)

    assert bucket_sizes == [1, 2]
    assert sorted(sorted(item.draft_id for item in bucket) for bucket in buckets) == [
        ["d1", "d2"],
        ["d3"],
    ]


def test_materialize_cross_merge_buckets_falls_back_to_singletons_for_missing_drafts() -> None:
    candidates = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-23",
            topic="苏州代理商切换安排",
            content="确认苏州代理商切换时间",
            result="",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
        ),
        SourceBackedEventDraft(
            draft_id="d2",
            date="2026-06-23",
            topic="苏州代理商切换计划",
            content="沟通苏州代理商切换方案",
            result="",
            source_message_ids=["m2"],
            source_conversation_id="c2",
            source_slice_id="s2",
            confidence=0.9,
        ),
        SourceBackedEventDraft(
            draft_id="d3",
            date="2026-06-23",
            topic="支付宝小程序宣传海报推进",
            content="跟进海报设计",
            result="",
            source_message_ids=["m3"],
            source_conversation_id="c3",
            source_slice_id="s3",
            confidence=0.9,
        ),
    ]

    buckets = materialize_cross_merge_buckets(
        candidates,
        CrossMergeBucketResult(
            buckets=[
                CrossMergeBucket(bucket_id="b1", draft_ids=["d1", "missing"], reason="partial"),
            ]
        ),
    )
    bucket_sizes = sorted(len(bucket) for bucket in buckets)

    assert bucket_sizes == [1, 1, 1]
    assert sorted(sorted(item.draft_id for item in bucket) for bucket in buckets) == [
        ["d1"],
        ["d2"],
        ["d3"],
    ]


def test_select_cross_bucket_candidate_pairs_uses_conservative_overlap() -> None:
    merged_buckets = [
        BucketMergedDraft(
            bucket_id="b1",
            draft=MergedEventDraft(
                date="2026-06-23",
                topic="逾期处理汇报文档进度",
                content="跟进逾期处理汇报文档进度",
                result="",
                source_message_ids=["m1"],
                source_conversation_ids=["c1"],
            ),
            upstream_draft_ids=["d1"],
        ),
        BucketMergedDraft(
            bucket_id="b2",
            draft=MergedEventDraft(
                date="2026-06-23",
                topic="会议纪要整理为汇报文档",
                content="根据会议纪要形成汇报文档",
                result="",
                source_message_ids=["m2"],
                source_conversation_ids=["c2"],
            ),
            upstream_draft_ids=["d2"],
        ),
        BucketMergedDraft(
            bucket_id="b3",
            draft=MergedEventDraft(
                date="2026-06-23",
                topic="淘宝闪购详情页设计",
                content="提交详情页设计",
                result="已发送",
                source_message_ids=["m3"],
                source_conversation_ids=["c3"],
            ),
            upstream_draft_ids=["d3"],
        ),
    ]

    pairs = select_cross_bucket_candidate_pairs(merged_buckets)

    assert ("b1", "b2") in pairs
    assert ("b1", "b3") not in pairs
    assert ("b2", "b3") not in pairs


def test_materialize_cross_bucket_merge_plan_and_flatten() -> None:
    merged_buckets = [
        BucketMergedDraft(
            bucket_id="b1",
            draft=MergedEventDraft(
                date="2026-06-23",
                topic="逾期处理汇报文档进度",
                content="跟进逾期处理汇报文档进度",
                result="",
                source_message_ids=["m1"],
                source_conversation_ids=["c1"],
            ),
            upstream_draft_ids=["d1"],
        ),
        BucketMergedDraft(
            bucket_id="b2",
            draft=MergedEventDraft(
                date="2026-06-23",
                topic="会议纪要整理为汇报文档",
                content="根据会议纪要形成汇报文档",
                result="",
                source_message_ids=["m2"],
                source_conversation_ids=["c2"],
            ),
            upstream_draft_ids=["d2"],
        ),
    ]

    groups = materialize_cross_bucket_merge_plan(
        merged_buckets,
        CrossBucketMergeResult(
            merge_decisions=[
                CrossBucketMergeDecision(
                    left_bucket_id="b1",
                    right_bucket_id="b2",
                    should_merge=True,
                    reason="same report thread",
                )
            ]
        ),
    )
    flattened = flatten_bucket_merge_groups(groups)

    assert len(groups) == 1
    assert len(flattened) == 1
    assert set(flattened[0].source_message_ids) == {"m1", "m2"}


def test_try_merge_bucket_locally_merges_obvious_two_item_bucket() -> None:
    bucket = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-23",
            topic="淘宝闪购详情页设计",
            content="提交淘宝闪购商品详情页设计供查看",
            result="淘宝闪购商品详情页设计已发送",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
        ),
        SourceBackedEventDraft(
            draft_id="d2",
            date="2026-06-23",
            topic="淘宝闪购详情页设计",
            content="许颖超在当晚提交了淘宝闪购商品详情页设计供孙维晟查看",
            result="淘宝闪购商品详情页设计已发出",
            source_message_ids=["m2"],
            source_conversation_id="c1",
            source_slice_id="s2",
            confidence=0.9,
        ),
    ]

    merged = try_merge_bucket_locally(bucket)

    assert merged is not None
    assert len(merged) == 1
    assert set(merged[0].source_message_ids) == {"m1", "m2"}


def test_try_merge_bucket_locally_returns_none_for_unclear_bucket() -> None:
    bucket = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-23",
            topic="优惠券配置核对",
            content="继续核对优惠券配置",
            result="",
            source_message_ids=["m1"],
            source_conversation_id="c1",
            source_slice_id="s1",
            confidence=0.9,
        ),
        SourceBackedEventDraft(
            draft_id="d2",
            date="2026-06-23",
            topic="续租优惠券改动通知同步",
            content="发送通知并要求同步",
            result="",
            source_message_ids=["m2"],
            source_conversation_id="c2",
            source_slice_id="s2",
            confidence=0.9,
        ),
    ]

    assert try_merge_bucket_locally(bucket) is None
