from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from time import perf_counter

from .config import RuntimeConfig
from .constants import DailyRunStatus
from .errors import AnalyzerProtocolError, ChatSourceError, DeliveryError, StoreWriteError
from .factories import RuntimeDependencies, build_runtime_dependencies
from .logging_utils import log_timing
from .models import (
    AnalysisBatch,
    DailyRunResult,
    EventFileLink,
    WorkEvent,
    MergedEventDraft,
    SourceBackedEventDraft,
)
from .pipeline.conversation_first_pass import build_conversation_level_slices
from .pipeline.context_expansion import (
    build_single_slice_retry_batch,
    expand_slice_context,
)
from .pipeline.cross_conversation_merge import materialize_grouped_merged_drafts
from .pipeline.direct_relation_filter import filter_self_related_candidate_drafts
from .pipeline.event_merge import build_work_events
from .pipeline.filtering import filter_messages
from .pipeline.retention_filter import (
    filter_retained_candidate_drafts,
    filter_retained_merged_drafts,
    filter_retained_work_events,
)
from .pipeline.sensitive_filter import (
    filter_excluded_candidate_drafts,
    filter_sensitive_merged_drafts,
)
from .pipeline.validation import (
    normalize_cross_conversation_groups_with_fallback,
    validate_batch_analysis_result,
    validate_cross_conversation_groups,
    validate_merged_event_drafts,
)
from .utils.link_refs import build_message_link_id
from .models import AnalysisBatch, BatchAnalysisResult, ConversationSlice, SelfIdentity
from .utils.json_io import dump_json

logger = logging.getLogger("worktrace")
_LINK_TEXT_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")
_GENERIC_LINK_HINT_TOKENS = {
    "https",
    "http",
    "www",
    "github",
    "feishu",
    "larksuite",
    "docx",
    "wiki",
    "share",
    "base",
    "form",
    "space",
    "global",
    "com",
    "cn",
}


@dataclass
class DailyTraceRunner:
    config: RuntimeConfig
    dependencies: RuntimeDependencies

    def run(self, target_date: str) -> DailyRunResult:
        run_started_at = perf_counter()
        warning_messages: list[str] = []
        skipped_slice_count = 0

        try:
            stage_started_at = perf_counter()
            self_identity = self.dependencies.chat_source.get_self_identity()
            log_timing(
                logger,
                "runner.stage.completed",
                stage_started_at,
                stage="get_self_identity",
                target_date=target_date,
            )
            stage_started_at = perf_counter()
            conversations = self.dependencies.chat_source.list_target_conversations(
                target_date, self_identity
            )
            log_timing(
                logger,
                "runner.stage.completed",
                stage_started_at,
                stage="list_target_conversations",
                target_date=target_date,
                conversation_count=len(conversations),
            )
            stage_started_at = perf_counter()
            messages = self.dependencies.chat_source.fetch_conversation_messages(
                target_date,
                [item.conversation_id for item in conversations],
            )
            log_timing(
                logger,
                "runner.stage.completed",
                stage_started_at,
                stage="fetch_conversation_messages",
                target_date=target_date,
                message_count=len(messages),
            )
        except ChatSourceError as exc:
            return self._finish_run(
                run_started_at,
                self._failed_result(target_date, str(exc)),
            )

        if not conversations and not messages:
            return self._finish_run(
                run_started_at,
                self._write_empty_day(
                    target_date,
                    self_identity=self_identity,
                    conversation_count=0,
                    message_count=0,
                    slice_count=0,
                    batch_count=0,
                ),
            )

        stage_started_at = perf_counter()
        filtered_messages = filter_messages(messages)
        log_timing(
            logger,
            "runner.stage.completed",
            stage_started_at,
            stage="filter_messages",
            input_message_count=len(messages),
            output_message_count=len(filtered_messages),
        )
        stage_started_at = perf_counter()
        conversation_slices = build_conversation_level_slices(
            filtered_messages,
            self_identity.open_id,
            self.config,
        )
        log_timing(
            logger,
            "runner.stage.completed",
            stage_started_at,
            stage="build_conversation_level_slices",
            slice_count=len(conversation_slices),
        )
        all_candidates: list[SourceBackedEventDraft] = []
        analyzed_batch_count = 0
        all_message_order = [message.message_id for message in filtered_messages]

        try:
            for conversation_slice in conversation_slices:
                (
                    validated_result,
                    slice_warnings,
                    unresolved,
                    run_count,
                ) = self._analyze_conversation_slice_with_retry(
                    target_date=target_date,
                    conversation_slice=conversation_slice,
                    self_identity=self_identity,
                )
                analyzed_batch_count += run_count

                for candidate in validated_result.candidate_events:
                    all_candidates.append(candidate)

                if unresolved:
                    skipped_slice_count += 1
                warning_messages.extend(slice_warnings)
        except AnalyzerProtocolError as exc:
            return self._finish_run(
                run_started_at,
                self._failed_result(target_date, str(exc)),
            )

        merged_drafts: list[MergedEventDraft] = []
        if all_candidates:
            try:
                all_candidates, excluded_candidate_warnings = (
                    filter_excluded_candidate_drafts(all_candidates, self.config)
                )
                warning_messages.extend(excluded_candidate_warnings)
                all_candidates, self_relation_candidate_warnings = (
                    filter_self_related_candidate_drafts(
                        all_candidates,
                        {
                            item.slice_id: item
                            for item in conversation_slices
                        },
                        self_open_id=self_identity.open_id,
                        self_display_name=self_identity.display_name,
                        self_assignment_cues=self.config.self_assignment_cues,
                        self_assignment_actions=self.config.self_assignment_actions,
                    )
                )
                warning_messages.extend(self_relation_candidate_warnings)
                all_candidates, retention_candidate_warnings = (
                    filter_retained_candidate_drafts(all_candidates)
                )
                warning_messages.extend(retention_candidate_warnings)

                if not all_candidates:
                    return self._finish_run(
                        run_started_at,
                        self._write_empty_day(
                            target_date,
                            self_identity=self_identity,
                            conversation_count=len(conversations),
                            message_count=len(messages),
                            slice_count=len(conversation_slices),
                            batch_count=analyzed_batch_count,
                            warning_messages=warning_messages,
                            skipped_slice_count=skipped_slice_count,
                        ),
                    )

                if len(all_candidates) == 1:
                    merged_drafts = materialize_grouped_merged_drafts(
                        all_candidates,
                        [[all_candidates[0].draft_id]],
                        target_date=target_date,
                    )
                else:
                    merge_started_at = perf_counter()
                    group_result = self.dependencies.analyzer.merge_day_candidates(
                        target_date,
                        all_candidates,
                    )
                    self._dump_merge_debug_artifacts(
                        target_date=target_date,
                        candidates=all_candidates,
                        output_payload=getattr(
                            self.dependencies.analyzer,
                            "last_merge_payload",
                            None,
                        ),
                    )
                    try:
                        group_result = validate_cross_conversation_groups(
                            group_result,
                            all_candidates,
                        )
                    except AnalyzerProtocolError:
                        group_result, merge_repair_warnings = (
                            normalize_cross_conversation_groups_with_fallback(
                                group_result,
                                all_candidates,
                            )
                        )
                        warning_messages.extend(merge_repair_warnings)
                    merged_drafts = materialize_grouped_merged_drafts(
                        all_candidates,
                        [group.draft_ids for group in group_result.groups],
                        target_date=target_date,
                    )
                    merged_drafts = validate_merged_event_drafts(
                        merged_drafts,
                        message_order=all_message_order,
                    )
                    log_timing(
                        logger,
                        "runner.stage.completed",
                        merge_started_at,
                        stage="merge_day_candidates",
                        candidate_event_count=len(all_candidates),
                        merged_event_count=len(merged_drafts),
                    )
            except (AnalyzerProtocolError, ValueError) as exc:
                return self._finish_run(
                    run_started_at,
                    self._failed_result(target_date, str(exc)),
                )

        if not merged_drafts:
            return self._finish_run(
                run_started_at,
                self._write_empty_day(
                    target_date,
                    self_identity=self_identity,
                    conversation_count=len(conversations),
                    message_count=len(messages),
                    slice_count=len(conversation_slices),
                    batch_count=analyzed_batch_count,
                    warning_messages=warning_messages,
                    skipped_slice_count=skipped_slice_count,
                ),
            )

        try:
            merged_drafts, sensitive_warnings = filter_sensitive_merged_drafts(
                merged_drafts,
                self.config,
            )
            warning_messages.extend(sensitive_warnings)
            merged_drafts, retention_merged_warnings = filter_retained_merged_drafts(
                merged_drafts,
            )
            warning_messages.extend(retention_merged_warnings)
            event_build_started_at = perf_counter()
            events, merge_warnings = build_work_events(target_date, merged_drafts)
            events = _attach_event_file_links(
                events,
                messages=filtered_messages,
                content_resolver=self.dependencies.content_resolver,
            )
            events, retention_event_warnings = filter_retained_work_events(events)
            warning_messages.extend(retention_event_warnings)
            events = _sort_events_for_output(events, messages=filtered_messages)
            log_timing(
                logger,
                "runner.stage.completed",
                event_build_started_at,
                stage="build_work_events",
                event_count=len(events),
                warning_count=len(merge_warnings),
            )
            warning_messages.extend(merge_warnings)
            write_started_at = perf_counter()
            write_result = self.dependencies.event_store.replace_day(
                target_date,
                events,
                owner_display_name=self_identity.display_name,
            )
            log_timing(
                logger,
                "runner.stage.completed",
                write_started_at,
                stage="write_markdown",
                event_count=len(events),
                output_path=write_result.output_path,
            )
            delivery_status, delivery_target, delivery_error = _deliver_markdown_to_self(
                self.dependencies.delivery_channel,
                self_identity=self_identity,
                markdown_path=Path(write_result.output_path),
            )
            if delivery_error:
                warning_messages.append(delivery_error)
        except (AnalyzerProtocolError, StoreWriteError, ValueError) as exc:
            return self._finish_run(
                run_started_at,
                self._failed_result(target_date, str(exc)),
            )

        status = (
            DailyRunStatus.SUCCESS_WITH_WARNINGS.value
            if warning_messages or skipped_slice_count
            else DailyRunStatus.SUCCESS.value
        )
        return self._finish_run(
            run_started_at,
            DailyRunResult(
                target_date=target_date,
                conversation_count=len(conversations),
                message_count=len(messages),
                slice_count=len(conversation_slices),
                batch_count=analyzed_batch_count,
                event_count=len(events),
                skipped_slice_count=skipped_slice_count,
                warning_count=len(warning_messages),
                status=status,
                output_path=write_result.output_path,
                error_summary="; ".join(warning_messages),
                self_delivery_status=delivery_status,
                self_delivery_target=delivery_target,
                self_delivery_error=delivery_error,
            ),
        )

    def _write_empty_day(
        self,
        target_date: str,
        *,
        self_identity: SelfIdentity,
        conversation_count: int,
        message_count: int,
        slice_count: int,
        batch_count: int,
        warning_messages: list[str] | None = None,
        skipped_slice_count: int = 0,
    ) -> DailyRunResult:
        warning_messages = warning_messages or []
        write_result = self.dependencies.event_store.replace_day(
            target_date,
            [],
            owner_display_name=self_identity.display_name,
        )
        delivery_status, delivery_target, delivery_error = _deliver_markdown_to_self(
            self.dependencies.delivery_channel,
            self_identity=self_identity,
            markdown_path=Path(write_result.output_path),
        )
        if delivery_error:
            warning_messages.append(delivery_error)
        status = (
            DailyRunStatus.SUCCESS_WITH_WARNINGS.value
            if warning_messages or skipped_slice_count
            else DailyRunStatus.SUCCESS.value
        )
        return DailyRunResult(
            target_date=target_date,
            conversation_count=conversation_count,
            message_count=message_count,
            slice_count=slice_count,
            batch_count=batch_count,
            event_count=0,
            skipped_slice_count=skipped_slice_count,
            warning_count=len(warning_messages),
            status=status,
            output_path=write_result.output_path,
            error_summary="; ".join(warning_messages),
            self_delivery_status=delivery_status,
            self_delivery_target=delivery_target,
            self_delivery_error=delivery_error,
        )

    def _failed_result(self, target_date: str, error_summary: str) -> DailyRunResult:
        return DailyRunResult(
            target_date=target_date,
            conversation_count=0,
            message_count=0,
            slice_count=0,
            batch_count=0,
            event_count=0,
            skipped_slice_count=0,
            warning_count=0,
            status=DailyRunStatus.FAILED.value,
            output_path=None,
            error_summary=error_summary,
            self_delivery_status="",
            self_delivery_target="",
            self_delivery_error="",
        )

    def _finish_run(
        self,
        run_started_at: float,
        result: DailyRunResult,
    ) -> DailyRunResult:
        log_timing(
            logger,
            "runner.run.completed",
            run_started_at,
            target_date=result.target_date,
            status=result.status,
            conversation_count=result.conversation_count,
            message_count=result.message_count,
            slice_count=result.slice_count,
            batch_count=result.batch_count,
            event_count=result.event_count,
            warning_count=result.warning_count,
            skipped_slice_count=result.skipped_slice_count,
        )
        return result

    def _analyze_conversation_slice_with_retry(
        self,
        *,
        target_date: str,
        conversation_slice: ConversationSlice,
        self_identity: SelfIdentity,
    ) -> tuple[BatchAnalysisResult, list[str], bool, int]:
        current_slice = conversation_slice
        warning_messages: list[str] = []
        run_count = 0

        for retry_round in range(0, self.config.slice_retry_limit + 1):
            if retry_round == 0:
                batch_input = AnalysisBatch(
                    target_date=target_date,
                    batch_id=f"conversation-{run_count + 1:03d}",
                    retry_round=0,
                    estimated_tokens=0,
                    self_open_id=self_identity.open_id,
                    self_display_name=self_identity.display_name,
                    slices=[current_slice],
                )
            else:
                batch_input = build_single_slice_retry_batch(
                    target_date,
                    current_slice,
                    retry_round=retry_round,
                    self_open_id=self_identity.open_id,
                    self_display_name=self_identity.display_name,
                    config=self.config,
                )

            batch_started_at = perf_counter()
            prompt = self.dependencies.analyzer.build_batch_prompt(
                batch_input
            ) if hasattr(self.dependencies.analyzer, "build_batch_prompt") else None
            try:
                batch_result = self.dependencies.analyzer.analyze_batch(target_date, batch_input)
            except AnalyzerProtocolError as exc:
                if retry_round == 0:
                    self._dump_conversation_debug_artifacts(
                        target_date=target_date,
                        conversation_slice=current_slice,
                        batch_input=batch_input,
                        prompt=prompt,
                        elapsed_ms=(perf_counter() - batch_started_at) * 1000,
                        error_summary=str(exc),
                    )
                raise
            validated_result = validate_batch_analysis_result(
                batch_result,
                {current_slice.slice_id: current_slice},
                self_open_id=self_identity.open_id,
            )
            run_count += 1
            if retry_round == 0:
                self._dump_conversation_debug_artifacts(
                    target_date=target_date,
                    conversation_slice=current_slice,
                    batch_input=batch_input,
                    prompt=prompt,
                    elapsed_ms=(perf_counter() - batch_started_at) * 1000,
                    output_payload=batch_result.to_dict(),
                    validated_result=validated_result,
                )
            log_timing(
                logger,
                "runner.stage.completed",
                batch_started_at,
                stage="analyze_conversation_slice",
                batch_id=batch_input.batch_id,
                slice_id=current_slice.slice_id,
                slice_count=1,
                retry_round=retry_round,
                candidate_event_count=len(validated_result.candidate_events),
                context_request_count=len(validated_result.context_requests),
            )

            if not validated_result.context_requests:
                return validated_result, warning_messages, False, run_count

            if retry_round >= self.config.slice_retry_limit:
                warning_messages.extend(
                    [
                        (
                            f"Slice needs more context after retries: {request.slice_id} "
                            f"({request.request_type}) {request.reason}"
                        )
                        for request in validated_result.context_requests
                    ]
                )
                return validated_result, warning_messages, True, run_count

            expanded_slice = expand_slice_context(
                current_slice,
                validated_result.context_requests,
                chat_source=self.dependencies.chat_source,
                content_resolver=self.dependencies.content_resolver,
                config=self.config,
            )
            if _conversation_slice_signature(expanded_slice) == _conversation_slice_signature(
                current_slice
            ):
                warning_messages.extend(
                    [
                        (
                            f"Slice expansion produced no new context: {request.slice_id} "
                            f"({request.request_type}) {request.reason}"
                        )
                        for request in validated_result.context_requests
                    ]
                )
                return validated_result, warning_messages, True, run_count

            current_slice = expanded_slice

        return BatchAnalysisResult(), warning_messages, True, run_count

    def _dump_conversation_debug_artifacts(
        self,
        *,
        target_date: str,
        conversation_slice: ConversationSlice,
        batch_input: AnalysisBatch,
        elapsed_ms: float,
        prompt: str | None,
        output_payload: dict[str, object] | None = None,
        validated_result: BatchAnalysisResult | None = None,
        error_summary: str | None = None,
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return

        conversation_dir = (
            debug_root
            / target_date
            / _safe_conversation_dir_name(conversation_slice.slice_id)
            / "pass_01"
        )
        conversation_dir.mkdir(parents=True, exist_ok=True)
        (conversation_dir / "input.json").write_text(
            dump_json(batch_input.to_dict(), pretty=True) + "\n",
            encoding="utf-8",
        )
        if prompt is not None:
            (conversation_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        if output_payload is not None:
            (conversation_dir / "output.json").write_text(
                dump_json(output_payload, pretty=True) + "\n",
                encoding="utf-8",
            )

        meta = {
            "target_date": target_date,
            "slice_id": conversation_slice.slice_id,
            "conversation_id": conversation_slice.conversation_id,
            "conversation_name": conversation_slice.conversation_name,
            "retry_round": batch_input.retry_round,
            "elapsed_ms": round(elapsed_ms, 3),
            "message_count": len(conversation_slice.messages),
            "anchor_message_count": len(conversation_slice.anchor_message_ids),
            "in_day_message_count": len(conversation_slice.in_day_message_ids),
            "candidate_event_count": (
                len(validated_result.candidate_events) if validated_result is not None else None
            ),
            "context_request_count": (
                len(validated_result.context_requests) if validated_result is not None else None
            ),
            "status": "failed" if error_summary else "completed",
            "error_summary": error_summary or "",
        }
        (conversation_dir / "meta.json").write_text(
            dump_json(meta, pretty=True) + "\n",
            encoding="utf-8",
        )

    def _dump_merge_debug_artifacts(
        self,
        *,
        target_date: str,
        candidates: list[SourceBackedEventDraft],
        output_payload: dict[str, object] | None = None,
    ) -> None:
        debug_root = self.config.conversation_debug_root
        if debug_root is None:
            return

        merge_dir = debug_root / target_date / "_merge_day_candidates"
        merge_dir.mkdir(parents=True, exist_ok=True)
        input_payload = {
            "target_date": target_date,
            "candidates": [candidate.to_dict() for candidate in candidates],
        }
        (merge_dir / "input.json").write_text(
            dump_json(input_payload, pretty=True) + "\n",
            encoding="utf-8",
        )
        prompt = self.dependencies.analyzer.build_merge_prompt(target_date, candidates)
        (merge_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        if output_payload is not None:
            (merge_dir / "output.json").write_text(
                dump_json(output_payload, pretty=True) + "\n",
                encoding="utf-8",
            )


def _safe_conversation_dir_name(slice_id: str) -> str:
    return slice_id.replace("/", "_").replace(":", "__")
def run_daily_trace(target_date: str, config: RuntimeConfig) -> DailyRunResult:
    runner = DailyTraceRunner(config=config, dependencies=build_runtime_dependencies(config))
    return runner.run(target_date)


def _conversation_slice_signature(
    conversation_slice: ConversationSlice,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(message.message_id for message in conversation_slice.messages),
        tuple(block.attachment_id for block in conversation_slice.attachment_texts),
        tuple(block.link_id for block in conversation_slice.linked_file_texts),
    )


def _deliver_markdown_to_self(delivery_channel, *, self_identity, markdown_path: Path) -> tuple[str, str, str]:
    try:
        status, target = delivery_channel.deliver_to_self(
            self_identity=self_identity,
            markdown_path=markdown_path,
        )
        return status, target, ""
    except DeliveryError as exc:
        return "failed", self_identity.open_id, str(exc)


def _attach_event_file_links(
    events: list[WorkEvent],
    *,
    messages: list,
    content_resolver,
) -> list[WorkEvent]:
    link_by_id: dict[str, EventFileLink] = {}
    for message in messages:
        for index, link in enumerate(content_resolver.extract_links(message), start=1):
            link_by_id[build_message_link_id(message.message_id, index)] = EventFileLink(
                url=link.url,
                title=link.title,
                link_type=link.link_type,
            )
    attached: list[WorkEvent] = []

    for event in events:
        deduped: dict[str, EventFileLink] = {}
        for link_id in event.referenced_link_ids:
            link = link_by_id.get(link_id)
            if link is None:
                continue
            if not _event_supports_link(event, link):
                continue
            existing = deduped.get(link.url)
            if existing is None:
                deduped[link.url] = link
                continue
            if existing.title.strip() or not link.title.strip():
                continue
            deduped[link.url] = link

        attached.append(
            type(event)(
                date=event.date,
                event_id=event.event_id,
                title=event.title,
                content=event.content,
                source_message_ids=list(event.source_message_ids),
                file_links=list(deduped.values()),
                source_people=list(event.source_people),
                source_event_ids=list(event.source_event_ids),
                object_hint=event.object_hint,
                retention_reason=event.retention_reason,
                retention_detail=event.retention_detail,
                referenced_link_ids=list(event.referenced_link_ids),
            )
        )

    return attached


def _event_supports_link(event: WorkEvent, link: EventFileLink) -> bool:
    evidence_text = " ".join(
        [
            event.title,
            event.content,
            event.object_hint,
            event.retention_detail,
        ]
    ).lower()
    if not evidence_text.strip():
        return False

    for token in _link_evidence_tokens(link):
        if token in evidence_text:
            return True
    return False


def _link_evidence_tokens(link: EventFileLink) -> list[str]:
    evidence_source = link.title.strip() or link.url
    tokens = [token.lower() for token in _LINK_TEXT_TOKEN_RE.findall(evidence_source)]
    return [
        token
        for token in tokens
        if token not in _GENERIC_LINK_HINT_TOKENS and len(token.strip()) >= 2
    ]


def _sort_events_for_output(events: list[WorkEvent], *, messages: list) -> list[WorkEvent]:
    message_by_id = {message.message_id: message for message in messages}

    def _event_sort_key(event: WorkEvent) -> tuple[str, str, str]:
        source_times = [
            message_by_id[message_id].send_time
            for message_id in event.source_message_ids
            if message_id in message_by_id
        ]
        first_time = min(source_times) if source_times else ""
        first_message_id = event.source_message_ids[0] if event.source_message_ids else ""
        return (first_time, first_message_id, event.event_id)

    return sorted(events, key=_event_sort_key)
