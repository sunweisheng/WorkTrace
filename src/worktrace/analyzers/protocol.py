from __future__ import annotations

from typing import Sequence

from ..errors import AnalyzerProtocolError, PersonalGroupingValidationError
from ..models import (
    AnchorAnalysisResult,
    BatchAnalysisResult,
    BatchAnchorAnalysisResult,
    BatchSegmentAnalysisResult,
    CollectedGroupMemberConnection,
    CollectedGroupingGroup,
    CollectedGroupingResult,
    CollectedMergeResult,
    ConversationSegmentationResult,
    CrossConversationGroup,
    CrossConversationGroupResult,
    PersonalFactReviewResult,
    RetentionReviewResult,
    SourceBackedEventDraft,
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
        normalized = _normalize_personal_fact_review_payload(data)
        return PersonalFactReviewResult.from_dict(normalized)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError(
            f"Invalid personal fact review payload: {exc}"
        ) from exc


def _normalize_personal_fact_review_payload(
    data: dict[str, object],
) -> dict[str, object]:
    if set(data) != {"results"} or not isinstance(data["results"], list):
        raise ValueError("Personal fact review results must be a list.")
    required_fields = {
        "draft_id",
        "supported",
        "fact_items",
        "removed_claims",
    }
    normalized_results: list[dict[str, object]] = []
    for item in data["results"]:
        if not isinstance(item, dict) or set(item) != required_fields:
            raise ValueError("Personal fact review item fields do not match the contract.")
        if not isinstance(item["draft_id"], str):
            raise ValueError("Personal fact review draft_id must be a string.")
        if not isinstance(item["supported"], bool):
            raise ValueError("Personal fact review supported must be a boolean.")
        if not isinstance(item["fact_items"], dict) or not isinstance(
            item["removed_claims"], list
        ):
            raise ValueError("Personal fact review list fields are invalid.")
        if any(not isinstance(claim, str) for claim in item["removed_claims"]):
            raise ValueError("Personal fact review removed_claims must be strings.")
        normalized_facts = _normalize_personal_fact_review_items(item["fact_items"])
        text_fields = _personal_fact_review_text_fields(normalized_facts)
        normalized_results.append(
            {
                **item,
                **text_fields,
                "fact_items": normalized_facts,
            }
        )
    return {"results": normalized_results}


def _personal_fact_review_text_fields(
    fact_items: list[dict[str, object]],
) -> dict[str, str]:
    field_names = (
        "topic",
        "content",
        "action_label",
        "object_hint",
        "retention_detail",
    )
    values: dict[str, str] = {}
    for field_name in field_names:
        texts = [
            str(item["text"])
            for item in fact_items
            if item["field"] == field_name
        ]
        values[field_name] = "".join(texts)
    return values


def _normalize_personal_fact_review_items(
    payload: dict[str, object],
) -> list[dict[str, object]]:
    field_names = (
        "topic",
        "content",
        "action_label",
        "object_hint",
        "retention_detail",
    )
    if set(payload) != set(field_names):
        raise ValueError("Personal fact review fields do not match the contract.")

    normalized: list[dict[str, object]] = []
    for field_name in field_names:
        raw_items = payload[field_name]
        items = raw_items if field_name == "content" else [raw_items]
        if not isinstance(items, list):
            raise ValueError("Personal fact review content facts must be a list.")
        for fact in items:
            if not isinstance(fact, dict) or set(fact) != {
                "text",
                "evidence_message_ids",
            }:
                raise ValueError("Personal fact item fields do not match the contract.")
            text = fact["text"]
            evidence_ids = fact["evidence_message_ids"]
            if not isinstance(text, str) or not isinstance(evidence_ids, list):
                raise ValueError("Personal fact item values are invalid.")
            if any(not isinstance(message_id, str) for message_id in evidence_ids):
                raise ValueError("Personal fact evidence ids must be strings.")
            if text or evidence_ids:
                normalized.append(
                    {
                        "field": field_name,
                        "text": text,
                        "evidence_message_ids": evidence_ids,
                    }
                )
    return normalized


def parse_merge_payload(payload: object) -> CrossConversationGroupResult:
    data = expect_json_object(payload, "Cross-conversation merge result")
    try:
        return CrossConversationGroupResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid cross-conversation merge payload.") from exc


def parse_personal_grouping_function_payload(
    payload: object,
    *,
    candidates: Sequence[SourceBackedEventDraft],
    allowed_semantic_reasons: Sequence[str],
) -> CrossConversationGroupResult:
    try:
        data = expect_json_object(payload, "Personal grouping Function result")
    except AnalyzerProtocolError as exc:
        raise PersonalGroupingValidationError(str(exc)) from exc
    raw_groups = data.get("merged_groups")
    raw_singletons = data.get("singleton_draft_ids")
    if not isinstance(raw_groups, list) or not isinstance(raw_singletons, list):
        raise PersonalGroupingValidationError(
            "Personal grouping Function result must contain merged_groups and singleton_draft_ids arrays.",
            partial_result=CrossConversationGroupResult(),
        )

    expected = [candidate.draft_id for candidate in candidates]
    expected_set = set(expected)
    candidate_by_id = {candidate.draft_id: candidate for candidate in candidates}
    candidate_order = {draft_id: index for index, draft_id in enumerate(expected)}
    allowed_reason_set = set(allowed_semantic_reasons)
    seen: set[str] = set()
    errors: list[str] = []
    groups: list[CrossConversationGroup] = []
    unexpected_top_level_fields = sorted(
        set(data).difference({"merged_groups", "singleton_draft_ids"})
    )
    if unexpected_top_level_fields:
        errors.append(
            "unexpected_fields field=personal_grouping fields="
            f"{unexpected_top_level_fields}"
        )

    for group_index, raw_group in enumerate(raw_groups, start=1):
        field_prefix = f"merged_groups[{group_index - 1}]"
        if not isinstance(raw_group, dict):
            errors.append(f"invalid_group field={field_prefix}")
            continue
        group_error_count = len(errors)
        unexpected_group_fields = sorted(
            set(raw_group).difference(
                {
                    "draft_ids",
                    "primary_draft_id",
                    "common_object",
                    "semantic_reasons",
                    "reason_detail",
                    "member_connections",
                }
            )
        )
        if unexpected_group_fields:
            errors.append(
                f"unexpected_fields field={field_prefix} fields={unexpected_group_fields}"
            )
        raw_draft_ids = raw_group.get("draft_ids")
        draft_ids = (
            [str(value) for value in raw_draft_ids]
            if isinstance(raw_draft_ids, list)
            else []
        )
        if len(draft_ids) < 2:
            errors.append(
                f"merged_group_too_small field={field_prefix}.draft_ids draft_ids={draft_ids}"
            )
        local_duplicates = sorted(
            {draft_id for draft_id in draft_ids if draft_ids.count(draft_id) > 1}
        )
        if local_duplicates:
            errors.append(
                f"duplicate_group_member field={field_prefix}.draft_ids draft_ids={local_duplicates}"
            )
        unknown_ids = [draft_id for draft_id in draft_ids if draft_id not in expected_set]
        if unknown_ids:
            errors.append(
                f"unknown_group_member field={field_prefix}.draft_ids draft_ids={unknown_ids}"
            )
        for draft_id in draft_ids:
            if draft_id in seen:
                errors.append(
                    f"duplicate_global_member field={field_prefix}.draft_ids draft_id={draft_id}"
                )
            seen.add(draft_id)

        primary_draft_id = str(raw_group.get("primary_draft_id", ""))
        if primary_draft_id not in draft_ids:
            errors.append(
                f"invalid_primary field={field_prefix}.primary_draft_id draft_id={primary_draft_id}"
            )
        raw_common_object = raw_group.get("common_object")
        common_object = (
            raw_common_object.strip()
            if isinstance(raw_common_object, str)
            else ""
        )
        if not common_object:
            errors.append(f"common_object_missing field={field_prefix}.common_object")
        raw_reason_detail = raw_group.get("reason_detail")
        reason_detail = (
            raw_reason_detail.strip()
            if isinstance(raw_reason_detail, str)
            else ""
        )
        if not reason_detail:
            errors.append(f"reason_detail_missing field={field_prefix}.reason_detail")

        raw_reasons = raw_group.get("semantic_reasons")
        semantic_reasons = (
            [str(value) for value in raw_reasons]
            if isinstance(raw_reasons, list)
            else []
        )
        if not semantic_reasons:
            errors.append(
                f"semantic_reason_missing field={field_prefix}.semantic_reasons"
            )
        invalid_reasons = [
            reason for reason in semantic_reasons if reason not in allowed_reason_set
        ]
        duplicate_reasons = sorted(
            {
                reason
                for reason in semantic_reasons
                if semantic_reasons.count(reason) > 1
            }
        )
        if duplicate_reasons:
            errors.append(
                f"duplicate_semantic_reason field={field_prefix}.semantic_reasons reasons={duplicate_reasons}"
            )
        if invalid_reasons:
            errors.append(
                f"unknown_semantic_reason field={field_prefix}.semantic_reasons reasons={invalid_reasons}"
            )

        raw_connections = raw_group.get("member_connections")
        connections = raw_connections if isinstance(raw_connections, list) else []
        connection_ids: list[str] = []
        evidence_message_ids: list[str] = []
        for connection_index, raw_connection in enumerate(connections):
            connection_prefix = (
                f"{field_prefix}.member_connections[{connection_index}]"
            )
            if not isinstance(raw_connection, dict):
                errors.append(f"invalid_member_connection field={connection_prefix}")
                continue
            unexpected_connection_fields = sorted(
                set(raw_connection).difference(
                    {"draft_id", "connection_detail", "evidence_message_ids"}
                )
            )
            if unexpected_connection_fields:
                errors.append(
                    f"unexpected_fields field={connection_prefix} fields={unexpected_connection_fields}"
                )
            draft_id = str(raw_connection.get("draft_id", ""))
            connection_ids.append(draft_id)
            if draft_id not in draft_ids:
                errors.append(
                    f"unknown_member_connection field={connection_prefix}.draft_id draft_id={draft_id}"
                )
            raw_connection_detail = raw_connection.get("connection_detail")
            if not (
                isinstance(raw_connection_detail, str)
                and raw_connection_detail.strip()
            ):
                errors.append(
                    f"member_connection_detail_missing field={connection_prefix}.connection_detail"
                )
            raw_evidence = raw_connection.get("evidence_message_ids")
            connection_evidence = (
                [str(value) for value in raw_evidence]
                if isinstance(raw_evidence, list)
                else []
            )
            if not connection_evidence:
                errors.append(
                    f"member_connection_evidence_missing field={connection_prefix}.evidence_message_ids"
                )
            duplicate_evidence = sorted(
                {
                    message_id
                    for message_id in connection_evidence
                    if connection_evidence.count(message_id) > 1
                }
            )
            if duplicate_evidence:
                errors.append(
                    f"member_connection_evidence_duplicate field={connection_prefix}.evidence_message_ids "
                    f"message_ids={duplicate_evidence}"
                )
            candidate = candidate_by_id.get(draft_id)
            allowed_evidence = set(candidate.source_message_ids) if candidate else set()
            invalid_evidence = [
                message_id
                for message_id in connection_evidence
                if message_id not in allowed_evidence
            ]
            if invalid_evidence:
                errors.append(
                    f"member_connection_evidence_invalid field={connection_prefix}.evidence_message_ids "
                    f"draft_id={draft_id} message_ids={invalid_evidence}"
                )
            evidence_message_ids.extend(connection_evidence)

        duplicate_connections = sorted(
            {
                draft_id
                for draft_id in connection_ids
                if connection_ids.count(draft_id) > 1
            }
        )
        if duplicate_connections:
            errors.append(
                f"duplicate_member_connection field={field_prefix}.member_connections draft_ids={duplicate_connections}"
            )
        missing_connections = [
            draft_id for draft_id in draft_ids if draft_id not in connection_ids
        ]
        if missing_connections:
            errors.append(
                f"missing_member_connection field={field_prefix}.member_connections draft_ids={missing_connections}"
            )

        known_draft_ids = [draft_id for draft_id in draft_ids if draft_id in expected_set]
        known_draft_ids.sort(key=candidate_order.__getitem__)
        if known_draft_ids and len(errors) == group_error_count:
            groups.append(
                CrossConversationGroup(
                    group_id=f"group-{len(groups) + 1:03d}",
                    draft_ids=known_draft_ids,
                    primary_draft_id=primary_draft_id,
                    merge_reason=(
                        f"共同事项：{common_object}。{reason_detail}"
                        if common_object and reason_detail
                        else reason_detail
                    ),
                    evidence_message_ids=list(dict.fromkeys(evidence_message_ids)),
                )
            )

    for singleton_index, raw_draft_id in enumerate(raw_singletons):
        draft_id = str(raw_draft_id)
        if draft_id not in expected_set:
            errors.append(
                f"unknown_singleton field=singleton_draft_ids[{singleton_index}] draft_id={draft_id}"
            )
            continue
        if draft_id in seen:
            errors.append(
                f"duplicate_global_member field=singleton_draft_ids[{singleton_index}] draft_id={draft_id}"
            )
            continue
        seen.add(draft_id)
        groups.append(
            CrossConversationGroup(
                group_id=f"group-{len(groups) + 1:03d}",
                draft_ids=[draft_id],
                primary_draft_id=draft_id,
                merge_reason="单条保留",
                evidence_message_ids=[],
            )
        )

    missing_ids = [draft_id for draft_id in expected if draft_id not in seen]
    if missing_ids:
        errors.append(f"missing_global_members draft_ids={missing_ids}")
    groups.sort(
        key=lambda group: min(candidate_order[draft_id] for draft_id in group.draft_ids)
    )
    if errors:
        raise PersonalGroupingValidationError(
            "Personal grouping Function result is invalid: " + "; ".join(errors),
            partial_result=CrossConversationGroupResult(groups=groups),
        )
    return CrossConversationGroupResult(groups=groups)


def parse_collected_grouping_payload(payload: object) -> CollectedGroupingResult:
    data = expect_json_object(payload, "Collected grouping result")
    try:
        return CollectedGroupingResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid collected grouping payload.") from exc


def parse_collected_grouping_function_payload(
    payload: object,
    *,
    evidence_catalog: list[object],
    allowed_semantic_reasons: Sequence[str] = (),
    allow_model_evidence_relation_ids: bool = False,
    require_member_connections: bool = True,
) -> tuple[CollectedGroupingResult, list[str]]:
    from .collected_evidence import (
        EvidenceRelation,
        derive_group_evidence,
        selected_relations_cover_group,
    )

    data = expect_json_object(payload, "Collected grouping Function result")
    catalog = {
        item.relation_id: item
        for item in evidence_catalog
        if isinstance(item, EvidenceRelation)
    }
    allowed_semantic_reason_set = set(allowed_semantic_reasons)
    raw_groups = data.get("merged_groups")
    raw_singletons = data.get("singleton_draft_ids")
    if not isinstance(raw_groups, list) or not isinstance(raw_singletons, list):
        raise AnalyzerProtocolError(
            "Collected grouping Function result must contain group and singleton arrays."
        )

    groups: list[CollectedGroupingGroup] = []
    errors: list[str] = []
    for index, raw_group in enumerate(raw_groups, start=1):
        if not isinstance(raw_group, dict):
            errors.append(f"invalid_group field=merged_groups[{index - 1}]")
            continue
        group_id = str(raw_group.get("group_id", "")).strip() or f"merged-{index:03d}"
        draft_ids = [str(value) for value in raw_group.get("draft_ids", [])]
        if len(draft_ids) < 2:
            errors.append(
                "merged_group_too_small "
                f"field=merged_groups[{index - 1}].draft_ids group_id={group_id} "
                f"draft_ids={draft_ids}"
            )
        duplicate_draft_ids = sorted(
            {
                draft_id
                for draft_id in draft_ids
                if draft_ids.count(draft_id) > 1
            }
        )
        if duplicate_draft_ids:
            errors.append(
                "duplicate_group_member "
                f"field=merged_groups[{index - 1}].draft_ids group_id={group_id} "
                f"draft_ids={duplicate_draft_ids}"
            )
        raw_semantic_reasons = [
            str(value) for value in raw_group.get("semantic_reasons", [])
        ]
        semantic_reasons = [
            value
            for value in raw_semantic_reasons
            if value in allowed_semantic_reason_set
        ]
        for reason in raw_semantic_reasons:
            if reason not in allowed_semantic_reason_set:
                errors.append(
                    "unknown_semantic_reason "
                    f"field=merged_groups[{index - 1}].semantic_reasons "
                    f"group_id={group_id} reason={reason}"
                )
        model_relation_ids = [
            str(value) for value in raw_group.get("evidence_relation_ids", [])
        ]
        if model_relation_ids and not allow_model_evidence_relation_ids:
            errors.append(
                "model_evidence_relation_ids_not_allowed "
                f"field=merged_groups[{index - 1}].evidence_relation_ids "
                f"group_id={group_id} relation_ids={model_relation_ids}"
            )
        evidence_audit = derive_group_evidence(draft_ids, list(catalog.values()))
        selected_relations: list[EvidenceRelation]
        relation_ids: list[str]
        if allow_model_evidence_relation_ids:
            duplicate_relation_ids = sorted(
                {
                    relation_id
                    for relation_id in model_relation_ids
                    if model_relation_ids.count(relation_id) > 1
                }
            )
            if duplicate_relation_ids:
                errors.append(
                    "duplicate_evidence_relation "
                    f"field=merged_groups[{index - 1}].evidence_relation_ids "
                    f"group_id={group_id} relation_ids={duplicate_relation_ids}"
                )
            selected_relations = []
            for relation_id in model_relation_ids:
                relation = catalog.get(relation_id)
                if relation is None:
                    errors.append(
                        "unknown_evidence_relation "
                        f"field=merged_groups[{index - 1}].evidence_relation_ids "
                        f"group_id={group_id} relation_id={relation_id}"
                    )
                    continue
                if not set(relation.draft_ids).issubset(set(draft_ids)):
                    errors.append(
                        "evidence_outside_group "
                        f"field=merged_groups[{index - 1}].evidence_relation_ids "
                        f"group_id={group_id} relation_id={relation_id} "
                        f"relation_draft_ids={list(relation.draft_ids)} "
                        f"group_draft_ids={draft_ids}"
                    )
                    continue
                selected_relations.append(relation)
            relation_ids = list(model_relation_ids)
            if (
                not semantic_reasons
                and selected_relations
                and not selected_relations_cover_group(draft_ids, selected_relations)
            ):
                errors.append(
                    "evidence_does_not_cover_group "
                    f"field=merged_groups[{index - 1}].evidence_relation_ids "
                    f"group_id={group_id} relation_ids={relation_ids} "
                    "relation_endpoints="
                    f"{[list(item.draft_ids) for item in selected_relations]}"
                )
        else:
            relation_ids = list(evidence_audit.basis_relation_ids)
            selected_relations = [catalog[value] for value in relation_ids]
            if (
                not semantic_reasons
                and evidence_audit.contained_relation_ids
                and not evidence_audit.connected
            ):
                errors.append(
                    "evidence_does_not_cover_group "
                    f"field=merged_groups[{index - 1}].draft_ids "
                    f"group_id={group_id} "
                    f"relation_ids={list(evidence_audit.contained_relation_ids)} "
                    "relation_endpoints="
                    f"{[list(catalog[value].draft_ids) for value in evidence_audit.contained_relation_ids]} "
                    f"uncovered_draft_ids={list(evidence_audit.uncovered_draft_ids)}"
                )
        derived_reasons = [relation.relation_type for relation in selected_relations]
        if (
            not semantic_reasons
            and not selected_relations
            and (
                allow_model_evidence_relation_ids
                or not evidence_audit.contained_relation_ids
            )
        ):
            errors.append(
                "merge_reason_missing "
                f"field=merged_groups[{index - 1}] group_id={group_id}"
            )
        reason_detail = str(raw_group.get("reason_detail", "")).strip()
        if not reason_detail:
            errors.append(
                "reason_detail_missing "
                f"field=merged_groups[{index - 1}].reason_detail group_id={group_id}"
            )
        member_connections: list[CollectedGroupMemberConnection] = []
        raw_connections = raw_group.get("member_connections")
        if not isinstance(raw_connections, list):
            raw_connections = []
            if require_member_connections:
                errors.append(
                    "member_connections_missing "
                    f"field=merged_groups[{index - 1}].member_connections "
                    f"group_id={group_id} draft_ids={draft_ids}"
                )
        for connection_index, raw_connection in enumerate(raw_connections):
            if not isinstance(raw_connection, dict) or set(raw_connection) != {
                "draft_id",
                "connection_detail",
            }:
                errors.append(
                    "invalid_member_connection "
                    f"field=merged_groups[{index - 1}].member_connections"
                    f"[{connection_index}] group_id={group_id}"
                )
                continue
            connection = CollectedGroupMemberConnection.from_dict(raw_connection)
            member_connections.append(connection)
            if not connection.connection_detail.strip():
                errors.append(
                    "member_connection_detail_missing "
                    f"field=merged_groups[{index - 1}].member_connections"
                    f"[{connection_index}].connection_detail group_id={group_id} "
                    f"draft_id={connection.draft_id}"
                )
        if require_member_connections:
            connection_ids = [item.draft_id for item in member_connections]
            duplicate_connection_ids = sorted(
                {
                    draft_id
                    for draft_id in connection_ids
                    if connection_ids.count(draft_id) > 1
                }
            )
            unknown_connection_ids = sorted(set(connection_ids).difference(draft_ids))
            missing_connection_ids = sorted(set(draft_ids).difference(connection_ids))
            for error_name, invalid_ids in (
                ("duplicate_member_connection", duplicate_connection_ids),
                ("unknown_member_connection", unknown_connection_ids),
                ("missing_member_connection", missing_connection_ids),
            ):
                if invalid_ids:
                    errors.append(
                        f"{error_name} "
                        f"field=merged_groups[{index - 1}].member_connections "
                        f"group_id={group_id} draft_ids={invalid_ids}"
                    )
        groups.append(
            CollectedGroupingGroup(
                group_id=group_id,
                draft_ids=draft_ids,
                summary_title=str(raw_group.get("summary_title", "")),
                summary_content=str(raw_group.get("summary_content", "")),
                summary_object_hint=str(raw_group.get("summary_object_hint", "")),
                group_reason=list(dict.fromkeys([*derived_reasons, *semantic_reasons])),
                semantic_reasons=semantic_reasons,
                evidence_relation_ids=relation_ids,
                reason_detail=reason_detail,
                member_connections=member_connections,
                risk_flags=[str(value) for value in raw_group.get("risk_flags", [])],
            )
        )
    for index, draft_id in enumerate(raw_singletons, start=1):
        groups.append(
            CollectedGroupingGroup(
                group_id=f"singleton-{index:03d}",
                draft_ids=[str(draft_id)],
            )
        )
    return (
        CollectedGroupingResult(
            groups=groups,
            split_reason=str(data.get("split_reason", "")).strip(),
            validation_errors=errors,
        ),
        errors,
    )


def parse_collected_merge_payload(payload: object) -> CollectedMergeResult:
    data = expect_json_object(payload, "Collected merge result")
    try:
        return CollectedMergeResult.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProtocolError("Invalid collected merge payload.") from exc
