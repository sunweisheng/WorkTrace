from __future__ import annotations

from ..errors import AnalyzerProtocolError
from ..models import (
    AnchorAnalysisResult,
    BatchAnalysisResult,
    BatchAnchorAnalysisResult,
    BatchSegmentAnalysisResult,
    CollectedGroupingResult,
    CollectedMergeResult,
    ConversationSegmentationResult,
    CrossConversationGroupResult,
    PersonalFactReviewResult,
    RetentionReviewResult,
)
from ..pipeline.validation import expect_json_object


def parse_batch_analysis_payload(payload: object) -> BatchAnalysisResult:
    data = expect_json_object(payload, "Batch analysis result")
    try:
        return BatchAnalysisResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid batch analysis payload.") from exc


def parse_anchor_analysis_payload(payload: object) -> AnchorAnalysisResult:
    data = expect_json_object(payload, "Anchor analysis result")
    try:
        return AnchorAnalysisResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid anchor analysis payload.") from exc


def parse_anchor_batch_analysis_payload(payload: object) -> BatchAnchorAnalysisResult:
    data = expect_json_object(payload, "Batch anchor analysis result")
    try:
        return BatchAnchorAnalysisResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid batch anchor analysis payload.") from exc


def parse_conversation_segmentation_payload(payload: object) -> ConversationSegmentationResult:
    data = expect_json_object(payload, "Conversation segmentation result")
    try:
        return ConversationSegmentationResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid conversation segmentation payload.") from exc


def parse_segment_batch_analysis_payload(payload: object) -> BatchSegmentAnalysisResult:
    data = expect_json_object(payload, "Segment batch analysis result")
    try:
        return BatchSegmentAnalysisResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid segment batch analysis payload.") from exc


def parse_retention_review_payload(payload: object) -> RetentionReviewResult:
    data = expect_json_object(payload, "Retention review result")
    try:
        _validate_retention_review_payload_shape(data)
        return RetentionReviewResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid retention review payload.") from exc


def _validate_retention_review_payload_shape(data: dict[str, object]) -> None:
    if set(data) != {"results"} or not isinstance(data["results"], list):
        raise ValueError("Retention review results must be a list.")
    for item in data["results"]:
        if not isinstance(item, dict) or set(item) != {
            "draft_id",
            "routine_signals",
            "substantive_signals",
        }:
            raise ValueError("Retention review item fields do not match the contract.")
        if not isinstance(item["draft_id"], str):
            raise ValueError("Retention review draft_id must be a string.")
        for field_name in ("routine_signals", "substantive_signals"):
            signals = item[field_name]
            if not isinstance(signals, list):
                raise ValueError("Retention review signals must be lists.")
            for signal in signals:
                if not isinstance(signal, dict) or set(signal) != {
                    "type",
                    "evidence_message_ids",
                }:
                    raise ValueError(
                        "Retention review signal fields do not match the contract."
                    )
                if not isinstance(signal["type"], str) or not isinstance(
                    signal["evidence_message_ids"], list
                ):
                    raise ValueError("Retention review signal values are invalid.")
                if any(
                    not isinstance(message_id, str)
                    for message_id in signal["evidence_message_ids"]
                ):
                    raise ValueError(
                        "Retention review evidence message ids must be strings."
                    )


def parse_personal_fact_review_payload(payload: object) -> PersonalFactReviewResult:
    data = expect_json_object(payload, "Personal fact review result")
    try:
        _validate_personal_fact_review_payload_shape(data)
        return PersonalFactReviewResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid personal fact review payload.") from exc


def _validate_personal_fact_review_payload_shape(data: dict[str, object]) -> None:
    if set(data) != {"results"} or not isinstance(data["results"], list):
        raise ValueError("Personal fact review results must be a list.")
    required_fields = {
        "draft_id",
        "supported",
        "topic",
        "content",
        "action_label",
        "object_hint",
        "retention_detail",
        "workstream_key",
        "fact_items",
        "removed_claims",
    }
    for item in data["results"]:
        if not isinstance(item, dict) or set(item) != required_fields:
            raise ValueError("Personal fact review item fields do not match the contract.")
        string_fields = required_fields.difference(
            {"supported", "fact_items", "removed_claims"}
        )
        if any(not isinstance(item[field], str) for field in string_fields):
            raise ValueError("Personal fact review text fields must be strings.")
        if not isinstance(item["supported"], bool):
            raise ValueError("Personal fact review supported must be a boolean.")
        if not isinstance(item["fact_items"], list) or not isinstance(
            item["removed_claims"], list
        ):
            raise ValueError("Personal fact review list fields are invalid.")
        if any(not isinstance(claim, str) for claim in item["removed_claims"]):
            raise ValueError("Personal fact review removed_claims must be strings.")
        for fact in item["fact_items"]:
            if not isinstance(fact, dict) or set(fact) != {
                "field",
                "text",
                "evidence_message_ids",
            }:
                raise ValueError("Personal fact item fields do not match the contract.")
            if not isinstance(fact["field"], str) or not isinstance(
                fact["text"], str
            ) or not isinstance(fact["evidence_message_ids"], list):
                raise ValueError("Personal fact item values are invalid.")
            if any(
                not isinstance(message_id, str)
                for message_id in fact["evidence_message_ids"]
            ):
                raise ValueError("Personal fact evidence ids must be strings.")


def parse_merge_payload(payload: object) -> CrossConversationGroupResult:
    data = expect_json_object(payload, "Cross-conversation merge result")
    try:
        return CrossConversationGroupResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid cross-conversation merge payload.") from exc


def parse_collected_grouping_payload(payload: object) -> CollectedGroupingResult:
    data = expect_json_object(payload, "Collected grouping result")
    try:
        return CollectedGroupingResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid collected grouping payload.") from exc


def parse_collected_merge_payload(payload: object) -> CollectedMergeResult:
    data = expect_json_object(payload, "Collected merge result")
    try:
        return CollectedMergeResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid collected merge payload.") from exc
