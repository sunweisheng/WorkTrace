from __future__ import annotations

import json

from ..analyzers.prompts import serialize_slice_for_prompt
from ..config import RuntimeConfig
from ..models import AnalysisBatch, ConversationSlice


def estimate_slice_tokens(
    conversation_slice: ConversationSlice,
    config: RuntimeConfig,
) -> int:
    payload = serialize_slice_for_prompt(conversation_slice, config)
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return max(1, len(serialized) // 3 + 50)


def build_analysis_batches(
    target_date: str,
    slices: list[ConversationSlice],
    config: RuntimeConfig,
    *,
    retry_round: int = 0,
) -> list[AnalysisBatch]:
    batches: list[AnalysisBatch] = []
    current_slices: list[ConversationSlice] = []
    current_tokens = 0
    batch_index = 1

    for conversation_slice in slices:
        estimated = estimate_slice_tokens(conversation_slice, config)
        if estimated > config.single_slice_hard_limit:
            batches.append(
                AnalysisBatch(
                    target_date=target_date,
                    batch_id=f"batch-{batch_index:03d}",
                    retry_round=retry_round,
                    estimated_tokens=estimated,
                    slices=[conversation_slice],
                )
            )
            batch_index += 1
            continue

        would_exceed_count = len(current_slices) >= config.batch_slice_limit
        would_exceed_target = (
            current_slices and current_tokens + estimated > config.batch_target_tokens
        )
        would_exceed_hard = (
            current_slices and current_tokens + estimated > config.batch_hard_limit
        )

        if would_exceed_count or would_exceed_target or would_exceed_hard:
            batches.append(
                AnalysisBatch(
                    target_date=target_date,
                    batch_id=f"batch-{batch_index:03d}",
                    retry_round=retry_round,
                    estimated_tokens=current_tokens,
                    slices=current_slices,
                )
            )
            batch_index += 1
            current_slices = []
            current_tokens = 0

        current_slices.append(conversation_slice)
        current_tokens += estimated

    if current_slices:
        batches.append(
            AnalysisBatch(
                target_date=target_date,
                batch_id=f"batch-{batch_index:03d}",
                retry_round=retry_round,
                estimated_tokens=current_tokens,
                slices=current_slices,
            )
        )

    return batches
