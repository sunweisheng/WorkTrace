from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from ..analyzers.output_schemas import segment_batch_output_schema
from ..analyzers.prompts import build_segment_batch_analysis_prompt
from ..config import RuntimeConfig
from ..models import (
    BatchAnalysisResult,
    BatchSegmentAnalysisResult,
    ConversationSegment,
    ConversationSegmentationResult,
    ConversationSegmentUnit,
    ConversationSlice,
    NormalizedMessage,
    ResponseSignal,
    SegmentAnalysisBatch,
)
from ..reaction_catalog import ReactionCatalog
from ..utils.text import clean_text
from ..utils.token_estimation import estimate_model_input_tokens


def build_response_signals(
    messages: list[NormalizedMessage],
    *,
    self_open_id: str,
    reaction_catalog: ReactionCatalog | None = None,
) -> list[ResponseSignal]:
    signals: list[ResponseSignal] = []
    seen: set[str] = set()

    catalog = reaction_catalog or ReactionCatalog.empty("")
    for message in messages:
        if message.sender_open_id == self_open_id:
            signal = ResponseSignal(
                signal_id=f"text:{message.message_id}",
                kind="text",
                message_id=message.message_id,
                action_time=message.send_time,
            )
            signals.append(signal)
            seen.add(signal.signal_id)
        for index, reaction in enumerate(message.reactions):
            if reaction.operator_open_id != self_open_id:
                continue
            signal_id = reaction.reaction_id or f"reaction:{message.message_id}:{index}"
            if signal_id in seen:
                continue
            seen.add(signal_id)
            metadata = catalog.lookup(reaction.emoji_type)
            signals.append(
                ResponseSignal(
                    signal_id=signal_id,
                    kind="reaction",
                    message_id=message.message_id,
                    action_time=_normalize_action_time(reaction.action_time, message.send_time),
                    emoji_type=reaction.emoji_type,
                    emoji_name=metadata.name,
                    emoji_description=metadata.description,
                    semantic=metadata.semantic,
                )
            )

    return sorted(signals, key=lambda item: (item.action_time, item.signal_id))


def build_hard_boundary_message_ids(
    messages: list[NormalizedMessage],
    *,
    self_open_id: str,
) -> set[str]:
    boundaries: set[str] = set()
    for message in messages:
        if message.sender_open_id == self_open_id:
            continue
        if message.reply_to_message_id or message.quote_message_id:
            continue
        mentioned = set(message.mentioned_open_ids)
        if mentioned and self_open_id not in mentioned:
            boundaries.add(message.message_id)
    return boundaries


def validate_conversation_segmentation(
    result: ConversationSegmentationResult,
    messages: list[NormalizedMessage],
    *,
    self_open_id: str,
    self_display_name: str,
    self_assignment_keywords: tuple[str, ...],
    response_signals: list[ResponseSignal],
) -> tuple[list[ConversationSegmentUnit], list[str]]:
    if result.segment_start_message_ids:
        segments, start_warnings = _build_segments_from_start_ids(
            result.segment_start_message_ids,
            messages,
            self_open_id=self_open_id,
        )
        if start_warnings:
            return [], start_warnings
        result = replace(result, segment_start_message_ids=[], segments=segments)

    if not result.segments:
        return [], ["Skipped conversation because segmentation returned no segments."]

    ordered_ids = [item.message_id for item in messages]
    message_by_id = {item.message_id: item for item in messages}
    index_by_id = {message_id: index for index, message_id in enumerate(ordered_ids)}
    boundary_ids = build_hard_boundary_message_ids(messages, self_open_id=self_open_id)
    boundary_indexes = {index_by_id[item] for item in boundary_ids if item in index_by_id}
    primary_ids = [
        message_id
        for segment in result.segments
        for message_id in segment.primary_message_ids
    ]
    if primary_ids != ordered_ids:
        return [], ["Skipped conversation because segmentation did not partition messages in order."]

    units: list[ConversationSegmentUnit] = []
    warnings: list[str] = []
    seen_segment_ids: set[str] = set()
    for segment in result.segments:
        if not segment.segment_id or not segment.primary_message_ids:
            return [], ["Skipped conversation because segmentation returned an invalid segment."]
        if segment.segment_id in seen_segment_ids:
            return [], ["Skipped conversation because segmentation returned duplicate segment ids."]
        seen_segment_ids.add(segment.segment_id)
        primary_indexes = [index_by_id[item] for item in segment.primary_message_ids]
        if primary_indexes != list(range(primary_indexes[0], primary_indexes[-1] + 1)):
            return [], ["Skipped conversation because segment primary messages are not contiguous."]
        if any(primary_indexes[0] < boundary <= primary_indexes[-1] for boundary in boundary_indexes):
            return [], ["Skipped conversation because a segment crossed a recipient boundary."]
        context_message_ids = _derive_context_ids(
            segment.primary_message_ids,
            message_by_id=message_by_id,
            index_by_id=index_by_id,
        )
        derived_segment = replace(
            segment,
            context_message_ids=context_message_ids,
            self_evidence_message_ids=[],
            response_assessments=[],
        )

        eligible_evidence_ids = _eligible_self_evidence_ids(
            derived_segment,
            message_by_id=message_by_id,
            self_open_id=self_open_id,
            self_display_name=self_display_name,
            self_assignment_keywords=self_assignment_keywords,
            response_signals=response_signals,
        )
        included_ids = set(context_message_ids) | set(segment.primary_message_ids)
        self_evidence_message_ids = [
            item.message_id
            for item in messages
            if item.message_id in included_ids and item.message_id in eligible_evidence_ids
        ]
        if not self_evidence_message_ids:
            continue

        segment_signals = [
            signal
            for signal in response_signals
            if signal.message_id in included_ids
        ]
        units.append(
            ConversationSegmentUnit(
                segment_id=segment.segment_id,
                conversation_id=message_by_id[segment.primary_message_ids[0]].conversation_id,
                conversation_name=message_by_id[segment.primary_message_ids[0]].conversation_name,
                primary_message_ids=list(segment.primary_message_ids),
                context_message_ids=context_message_ids,
                self_evidence_message_ids=self_evidence_message_ids,
                response_signals=segment_signals,
                response_assessments=[],
                # The model receives this turn as one chronological timeline.  Earlier
                # quoted/replied messages stay visible as read-only context, while the
                # current turn remains the only allowed event source.
                messages=[item for item in messages if item.message_id in included_ids],
            )
        )

    if not units:
        warnings.append("No self-related conversation segments were retained.")
    return units, warnings


def _build_segments_from_start_ids(
    start_message_ids: list[str],
    messages: list[NormalizedMessage],
    *,
    self_open_id: str,
) -> tuple[list[ConversationSegment], list[str]]:
    ordered_ids = [item.message_id for item in messages]
    if not ordered_ids:
        return [], ["Skipped conversation because segmentation had no input messages."]
    if start_message_ids[0] != ordered_ids[0]:
        return [], ["Skipped conversation because segmentation did not start at the first message."]

    index_by_id = {message_id: index for index, message_id in enumerate(ordered_ids)}
    indexes = [index_by_id.get(message_id, -1) for message_id in start_message_ids]
    if any(index < 0 for index in indexes):
        return [], ["Skipped conversation because segmentation returned an unknown start message."]
    if indexes != sorted(set(indexes)):
        return [], ["Skipped conversation because segmentation start messages were not ordered."]

    required_boundary_ids = build_hard_boundary_message_ids(
        messages,
        self_open_id=self_open_id,
    )
    if not required_boundary_ids.issubset(start_message_ids):
        return [], ["Skipped conversation because segmentation omitted a recipient boundary."]

    segments: list[ConversationSegment] = []
    for segment_index, start_index in enumerate(indexes):
        end_index = (
            indexes[segment_index + 1]
            if segment_index + 1 < len(indexes)
            else len(ordered_ids)
        )
        segments.append(
            ConversationSegment(
                segment_id=f"turn-{segment_index + 1:03d}",
                primary_message_ids=ordered_ids[start_index:end_index],
            )
        )
    return segments, []


def segment_unit_to_slice(unit: ConversationSegmentUnit) -> ConversationSlice:
    return ConversationSlice(
        # Segment ids only need to be unique inside one conversation batch.  Scope the
        # slice id before it reaches the day-wide candidate pipeline.
        slice_id=f"{unit.conversation_id}:{unit.segment_id}",
        conversation_id=unit.conversation_id,
        conversation_name=unit.conversation_name,
        anchor_message_ids=list(unit.self_evidence_message_ids),
        in_day_message_ids=list(unit.primary_message_ids),
        messages=list(unit.messages),
        attachment_texts=list(unit.attachment_texts),
        linked_file_texts=list(unit.linked_file_texts),
        primary_message_ids=list(unit.primary_message_ids),
        context_message_ids=list(unit.context_message_ids),
        self_evidence_message_ids=list(unit.self_evidence_message_ids),
        response_signal_ids=[item.signal_id for item in unit.response_signals],
    )


def pack_segment_units(
    *,
    target_date: str,
    self_open_id: str,
    self_display_name: str,
    units: list[ConversationSegmentUnit],
    config: RuntimeConfig,
) -> list[SegmentAnalysisBatch]:
    if not units:
        return []
    # `units` is already in the validated conversation timeline order.  Message ids
    # are opaque identifiers, so sorting by them can silently reorder a batch.
    ordered = list(units)
    batches: list[SegmentAnalysisBatch] = []
    current: list[ConversationSegmentUnit] = []
    for unit in ordered:
        proposal = _build_segment_analysis_batch(
            target_date=target_date,
            self_open_id=self_open_id,
            self_display_name=self_display_name,
            units=[*current, unit],
        )
        if (
            current
            and _estimate_segment_batch_tokens(proposal, config)
            > config.model_input_batch_target_tokens
        ):
            batches.append(
                _build_segment_analysis_batch(
                    target_date=target_date,
                    self_open_id=self_open_id,
                    self_display_name=self_display_name,
                    units=current,
                )
            )
            current = [unit]
            continue
        current = [*current, unit]
    if current:
        batches.append(
            _build_segment_analysis_batch(
                target_date=target_date,
                self_open_id=self_open_id,
                self_display_name=self_display_name,
                units=current,
            )
        )
    marked_batches: list[SegmentAnalysisBatch] = []
    for batch in batches:
        estimated_tokens = _estimate_segment_batch_tokens(batch, config)
        marked_batches.append(
            replace(
                batch,
                estimated_input_tokens=estimated_tokens,
                input_target_tokens=config.model_input_batch_target_tokens,
                oversized_singleton=(
                    len(batch.segments) == 1
                    and estimated_tokens > config.model_input_batch_target_tokens
                ),
            )
        )
    return marked_batches


def validate_segment_batch_result(
    result: BatchSegmentAnalysisResult,
    batch: SegmentAnalysisBatch,
) -> tuple[dict[str, BatchAnalysisResult], list[ConversationSegmentUnit], list[str]]:
    unit_by_id = {item.segment_id: item for item in batch.segments}
    returned_ids = [item.segment_id for item in result.results]
    duplicate_ids = {item for item in returned_ids if returned_ids.count(item) > 1}
    valid: dict[str, BatchAnalysisResult] = {}
    warnings: list[str] = []
    for item in result.results:
        unit = unit_by_id.get(item.segment_id)
        if unit is None or item.segment_id in duplicate_ids:
            warnings.append("Filtered invalid segment batch result.")
            continue
        filtered_candidates = []
        source_ids = set(unit.primary_message_ids)
        evidence_ids = set(unit.self_evidence_message_ids)
        signal_ids = {signal.signal_id for signal in unit.response_signals}
        available_attachment_ids = {
            block.attachment_id for block in unit.attachment_texts
        }
        available_attachment_ids.update(
            attachment.attachment_id
            for message in unit.messages
            for attachment in message.attachments
        )
        attachment_ids_by_message_id = {
            message.message_id: [
                attachment.attachment_id for attachment in message.attachments
            ]
            for message in unit.messages
            if message.attachments
        }
        for candidate in item.analysis.candidate_events:
            candidate_sources = set(candidate.source_message_ids)
            if not candidate_sources or not candidate_sources.issubset(source_ids):
                warnings.append("Filtered candidate with cross-segment source.")
                continue
            candidate_signals = set(candidate.response_signal_ids)
            candidate_evidence = set(candidate.self_evidence_message_ids)
            if candidate_evidence and not candidate_evidence.issubset(evidence_ids):
                warnings.append("Filtered candidate with invalid self evidence.")
                continue
            if candidate_signals and not candidate_signals.issubset(signal_ids):
                warnings.append("Filtered candidate with invalid response signal.")
                continue
            if not candidate_evidence:
                candidate_evidence = candidate_sources & evidence_ids
            if not candidate_evidence and not (candidate_signals & signal_ids):
                warnings.append("Filtered candidate without self evidence.")
                continue
            resolved_attachment_ids: list[str] = []
            repaired_attachment_reference = False
            removed_attachment_reference = False
            for attachment_id in candidate.referenced_attachment_ids:
                if attachment_id in available_attachment_ids:
                    resolved_attachment_ids.append(attachment_id)
                    continue
                message_attachment_ids = attachment_ids_by_message_id.get(
                    attachment_id,
                    [],
                )
                if len(message_attachment_ids) == 1:
                    resolved_attachment_ids.extend(message_attachment_ids)
                    repaired_attachment_reference = True
                    continue
                removed_attachment_reference = True
            if repaired_attachment_reference:
                warnings.append("Repaired candidate attachment message reference.")
            if removed_attachment_reference:
                warnings.append("Removed unavailable attachment reference from candidate.")
            filtered_candidates.append(
                replace(
                    candidate,
                    self_evidence_message_ids=sorted(candidate_evidence),
                    referenced_attachment_ids=list(
                        dict.fromkeys(resolved_attachment_ids)
                    ),
                )
            )
        valid[item.segment_id] = replace(
            item.analysis,
            candidate_events=filtered_candidates,
        )

    missing = [unit for unit in batch.segments if unit.segment_id not in valid]
    if missing:
        warnings.append("Segment batch response omitted one or more segments.")
    return valid, missing, warnings


def _derive_context_ids(
    primary_message_ids: list[str],
    *,
    message_by_id: dict[str, NormalizedMessage],
    index_by_id: dict[str, int],
) -> list[str]:
    first_primary_index = min(index_by_id[item] for item in primary_message_ids)
    context_ids: set[str] = set()
    for message_id in primary_message_ids:
        pending = [message_id]
        seen: set[str] = set()
        while pending:
            current_id = pending.pop()
            if current_id in seen:
                continue
            seen.add(current_id)
            message = message_by_id.get(current_id)
            if message is None:
                continue
            for related_id in (message.reply_to_message_id, message.quote_message_id):
                if not related_id or related_id not in message_by_id:
                    continue
                if index_by_id[related_id] < first_primary_index:
                    context_ids.add(related_id)
                pending.append(related_id)
    return [
        message_id
        for message_id in message_by_id
        if message_id in context_ids
    ]


def _eligible_self_evidence_ids(
    segment: ConversationSegment,
    *,
    message_by_id: dict[str, NormalizedMessage],
    self_open_id: str,
    self_display_name: str,
    self_assignment_keywords: tuple[str, ...],
    response_signals: list[ResponseSignal],
) -> set[str]:
    included_ids = set(segment.primary_message_ids) | set(segment.context_message_ids)
    own_ids = {
        message_id
        for message_id in included_ids
        if message_by_id[message_id].sender_open_id == self_open_id
    }
    direct_reply_ids = {
        message_id
        for message_id in included_ids
        if message_by_id[message_id].reply_to_message_id in own_ids
        or message_by_id[message_id].quote_message_id in own_ids
    }
    assigned_ids = {
        message_id
        for message_id in included_ids
        if _explicitly_assigns_to_self(
            message_by_id[message_id],
            self_open_id=self_open_id,
            self_display_name=self_display_name,
            assignment_keywords=self_assignment_keywords,
        )
    }
    reaction_ids = {
        signal.message_id
        for signal in response_signals
        if signal.kind == "reaction" and signal.message_id in included_ids
    }
    return own_ids | direct_reply_ids | assigned_ids | reaction_ids


def _explicitly_assigns_to_self(
    message: NormalizedMessage,
    *,
    self_open_id: str,
    self_display_name: str,
    assignment_keywords: tuple[str, ...],
) -> bool:
    if self_open_id in message.mentioned_open_ids:
        mentioned_self = True
    else:
        name = clean_text(self_display_name)
        text = clean_text(message.text).replace(" ", "")
        mentioned_self = bool(name and (f"@{name}" in text or name in text))
    return mentioned_self and any(
        keyword and keyword in message.text for keyword in assignment_keywords
    )


def _build_segment_analysis_batch(
    *,
    target_date: str,
    self_open_id: str,
    self_display_name: str,
    units: list[ConversationSegmentUnit],
) -> SegmentAnalysisBatch:
    return SegmentAnalysisBatch(
        target_date=target_date,
        conversation_id=units[0].conversation_id,
        conversation_name=units[0].conversation_name,
        self_open_id=self_open_id,
        self_display_name=self_display_name,
        segments=list(units),
    )


def _estimate_segment_batch_tokens(
    batch: SegmentAnalysisBatch,
    config: RuntimeConfig,
) -> int:
    return estimate_model_input_tokens(
        build_segment_batch_analysis_prompt(batch, config=config),
        output_schema=segment_batch_output_schema(config),
        append_no_think=True,
    )


def _normalize_action_time(value: str, fallback: str) -> str:
    if value.isdigit():
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return fallback
    return value or fallback
