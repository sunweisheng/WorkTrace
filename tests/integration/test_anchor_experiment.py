from __future__ import annotations

import json
from pathlib import Path

from src.worktrace.anchor_experiment import (
    AnchorExperimentResult,
    _main_impl,
    main,
    render_anchor_experiment_json,
    render_anchor_experiment_summary_table,
)
from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.models import BatchAnchorAnalysisItem, BatchAnchorAnalysisResult, AnchorAnalysisResult


def test_render_anchor_experiment_json_roundtrip() -> None:
    result = AnchorExperimentResult(
        target_date="2026-06-23",
        status=DailyRunStatus.SUCCESS.value,
        conversation_count=1,
        message_count=2,
        anchor_unit_count=1,
        analyzed_anchor_count=1,
        status_counts={"completed": 1},
        cache_bypass_enabled=False,
        cache_refresh_count=0,
        cache_hit_count=0,
        cache_miss_count=1,
        completion_mode_counts={"first_pass_completed": 1},
        cross_anchor_merge_count=0,
        context_request_count=0,
        candidate_event_count=0,
        results_summary=[
            {
                "anchor_unit_id": "oc_1:om_1",
                "completion_mode": "first_pass_completed",
                "cache_hit": False,
                "pass_count": 1,
                "anchor_status": "completed",
                "candidate_event_count": 0,
                "context_request_count": 0,
                "needs_cross_anchor_merge": False,
            }
        ],
        results=[{"anchor_unit": {"anchor_unit_id": "oc_1:om_1"}, "analysis": {"anchor_status": "completed"}}],
        error_summary="",
    )

    payload = json.loads(render_anchor_experiment_json(result))

    assert payload["target_date"] == "2026-06-23"
    assert payload["status"] == DailyRunStatus.SUCCESS.value
    assert payload["status_counts"] == {"completed": 1}
    assert payload["cache_miss_count"] == 1
    assert payload["cache_bypass_enabled"] is False
    assert payload["completion_mode_counts"] == {"first_pass_completed": 1}
    assert payload["results_summary"][0]["anchor_unit_id"] == "oc_1:om_1"


def test_render_anchor_experiment_json_summary_only_omits_results() -> None:
    result = AnchorExperimentResult(
        target_date="2026-06-23",
        status=DailyRunStatus.SUCCESS.value,
        conversation_count=1,
        message_count=2,
        anchor_unit_count=1,
        analyzed_anchor_count=1,
        status_counts={"completed": 1},
        cache_bypass_enabled=False,
        cache_refresh_count=0,
        cache_hit_count=0,
        cache_miss_count=1,
        completion_mode_counts={"first_pass_completed": 1},
        cross_anchor_merge_count=0,
        context_request_count=0,
        candidate_event_count=0,
        results_summary=[
            {
                "anchor_unit_id": "oc_1:om_1",
                "completion_mode": "first_pass_completed",
                "cache_hit": False,
                "pass_count": 1,
                "anchor_status": "completed",
                "candidate_event_count": 0,
                "context_request_count": 0,
                "needs_cross_anchor_merge": False,
            }
        ],
        results=[{"anchor_unit": {"anchor_unit_id": "oc_1:om_1"}, "analysis": {"anchor_status": "completed"}}],
        error_summary="",
    )

    payload = json.loads(render_anchor_experiment_json(result, summary_only=True))

    assert "results" not in payload
    assert payload["results_summary"][0]["anchor_unit_id"] == "oc_1:om_1"


def test_render_anchor_experiment_summary_table() -> None:
    result = AnchorExperimentResult(
        target_date="2026-06-23",
        status=DailyRunStatus.SUCCESS.value,
        conversation_count=1,
        message_count=2,
        anchor_unit_count=1,
        analyzed_anchor_count=1,
        status_counts={"completed": 1},
        cache_bypass_enabled=False,
        cache_refresh_count=0,
        cache_hit_count=0,
        cache_miss_count=1,
        completion_mode_counts={"first_pass_completed": 1},
        cross_anchor_merge_count=0,
        context_request_count=0,
        candidate_event_count=0,
        results_summary=[
            {
                "anchor_unit_id": "oc_1:om_1",
                "completion_mode": "first_pass_completed",
                "cache_hit": False,
                "pass_count": 1,
                "anchor_status": "completed",
                "candidate_event_count": 0,
                "context_request_count": 0,
                "needs_cross_anchor_merge": False,
            }
        ],
        results=[],
        error_summary="",
    )

    table = render_anchor_experiment_summary_table(result)

    assert "anchor_unit_id" in table
    assert "first_pass_completed" in table
    assert "oc_1:om_1" in table


def test_anchor_experiment_main_returns_failed_result_on_preflight_error(capsys) -> None:
    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(ok=False, error_summary="codex unavailable")

    buffer: list[str] = []

    def write_output(text: str) -> int:
        buffer.append(text)
        return len(text)

    exit_code = _main_impl(
        ["--date", "2026-06-23", "--limit", "1"],
        write_output,
        config=RuntimeConfig(),
        preflight_func=fake_preflight,
        run_func=None,
    )
    payload = json.loads("".join(buffer))

    assert exit_code == 1
    assert payload["status"] == DailyRunStatus.FAILED.value
    assert payload["error_summary"] == "codex unavailable"


def test_anchor_experiment_main_returns_success_result_with_fake_runner() -> None:
    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(ok=True, details={"cwd": str(cwd)})

    def fake_run(*, target_date, config, limit, dump_dir, ignore_cache, refresh_cache):
        return AnchorExperimentResult(
            target_date=target_date,
            status=DailyRunStatus.SUCCESS.value,
            conversation_count=1,
            message_count=2,
            anchor_unit_count=3,
            analyzed_anchor_count=1,
            status_counts={"completed": 1},
            cache_bypass_enabled=False,
            cache_refresh_count=0,
            cache_hit_count=0,
            cache_miss_count=1,
            completion_mode_counts={"first_pass_completed": 1},
            cross_anchor_merge_count=0,
            context_request_count=0,
            candidate_event_count=1,
            results_summary=[],
            results=[{"anchor_unit": {"anchor_unit_id": "oc_1:om_1"}, "analysis": {"anchor_status": "completed"}}],
            error_summary="",
        )

    buffer: list[str] = []

    def write_output(text: str) -> int:
        buffer.append(text)
        return len(text)

    exit_code = _main_impl(
        ["--date", "2026-06-23", "--limit", "1"],
        write_output,
        config=RuntimeConfig(),
        preflight_func=fake_preflight,
        run_func=fake_run,
    )
    payload = json.loads("".join(buffer))

    assert exit_code == 0
    assert payload["status"] == DailyRunStatus.SUCCESS.value
    assert payload["analyzed_anchor_count"] == 1
    assert payload["candidate_event_count"] == 1
    assert payload["cache_miss_count"] == 1


def test_anchor_experiment_main_passes_dump_dir_to_runner(tmp_path: Path) -> None:
    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(ok=True, details={"cwd": str(cwd)})

    captured: dict[str, object] = {}

    def fake_run(*, target_date, config, limit, dump_dir, ignore_cache, refresh_cache):
        captured["target_date"] = target_date
        captured["limit"] = limit
        captured["dump_dir"] = dump_dir
        return AnchorExperimentResult(
            target_date=target_date,
            status=DailyRunStatus.SUCCESS.value,
            conversation_count=0,
            message_count=0,
            anchor_unit_count=0,
            analyzed_anchor_count=0,
            status_counts={},
            cache_bypass_enabled=False,
            cache_refresh_count=0,
            cache_hit_count=0,
            cache_miss_count=0,
            completion_mode_counts={},
            cross_anchor_merge_count=0,
            context_request_count=0,
            candidate_event_count=0,
            results_summary=[],
            results=[],
            error_summary="",
        )

    buffer: list[str] = []

    def write_output(text: str) -> int:
        buffer.append(text)
        return len(text)

    dump_dir = tmp_path / "debug"
    exit_code = _main_impl(
        ["--date", "2026-06-23", "--limit", "2", "--dump-dir", str(dump_dir)],
        write_output,
        config=RuntimeConfig(),
        preflight_func=fake_preflight,
        run_func=fake_run,
    )

    assert exit_code == 0
    assert captured["target_date"] == "2026-06-23"
    assert captured["limit"] == 2
    assert captured["dump_dir"] == dump_dir


def test_anchor_experiment_main_passes_cache_flags_to_runner() -> None:
    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(ok=True, details={"cwd": str(cwd)})

    captured: dict[str, object] = {}

    def fake_run(*, target_date, config, limit, dump_dir, ignore_cache, refresh_cache):
        captured["ignore_cache"] = ignore_cache
        captured["refresh_cache"] = refresh_cache
        return AnchorExperimentResult(
            target_date=target_date,
            status=DailyRunStatus.SUCCESS.value,
            conversation_count=0,
            message_count=0,
            anchor_unit_count=0,
            analyzed_anchor_count=0,
            status_counts={},
            cache_bypass_enabled=True,
            cache_refresh_count=0,
            cache_hit_count=0,
            cache_miss_count=0,
            completion_mode_counts={},
            cross_anchor_merge_count=0,
            context_request_count=0,
            candidate_event_count=0,
            results_summary=[],
            results=[],
            error_summary="",
        )

    buffer: list[str] = []

    def write_output(text: str) -> int:
        buffer.append(text)
        return len(text)

    exit_code = _main_impl(
        ["--date", "2026-06-23", "--ignore-cache", "--refresh-cache"],
        write_output,
        config=RuntimeConfig(),
        preflight_func=fake_preflight,
        run_func=fake_run,
    )

    assert exit_code == 0
    assert captured["ignore_cache"] is True
    assert captured["refresh_cache"] is True


def test_anchor_experiment_main_summary_only_omits_results_from_output() -> None:
    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(ok=True, details={"cwd": str(cwd)})

    def fake_run(*, target_date, config, limit, dump_dir, ignore_cache, refresh_cache):
        return AnchorExperimentResult(
            target_date=target_date,
            status=DailyRunStatus.SUCCESS.value,
            conversation_count=1,
            message_count=2,
            anchor_unit_count=1,
            analyzed_anchor_count=1,
            status_counts={"completed": 1},
            cache_bypass_enabled=False,
            cache_refresh_count=0,
            cache_hit_count=0,
            cache_miss_count=1,
            completion_mode_counts={"first_pass_completed": 1},
            cross_anchor_merge_count=0,
            context_request_count=0,
            candidate_event_count=0,
            results_summary=[
                {
                    "anchor_unit_id": "oc_1:om_1",
                    "completion_mode": "first_pass_completed",
                    "cache_hit": False,
                    "pass_count": 1,
                    "anchor_status": "completed",
                    "candidate_event_count": 0,
                    "context_request_count": 0,
                    "needs_cross_anchor_merge": False,
                }
            ],
            results=[{"anchor_unit": {"anchor_unit_id": "oc_1:om_1"}, "analysis": {"anchor_status": "completed"}}],
            error_summary="",
        )

    buffer: list[str] = []

    def write_output(text: str) -> int:
        buffer.append(text)
        return len(text)

    exit_code = _main_impl(
        ["--date", "2026-06-23", "--summary-only"],
        write_output,
        config=RuntimeConfig(),
        preflight_func=fake_preflight,
        run_func=fake_run,
    )
    payload = json.loads("".join(buffer))

    assert exit_code == 0
    assert "results" not in payload
    assert payload["results_summary"][0]["anchor_unit_id"] == "oc_1:om_1"


def test_anchor_experiment_main_summary_table_outputs_plain_text() -> None:
    def fake_preflight(config, *, cwd):
        from src.worktrace.preflight import PreflightReport

        return PreflightReport(ok=True, details={"cwd": str(cwd)})

    def fake_run(*, target_date, config, limit, dump_dir, ignore_cache, refresh_cache):
        return AnchorExperimentResult(
            target_date=target_date,
            status=DailyRunStatus.SUCCESS.value,
            conversation_count=1,
            message_count=2,
            anchor_unit_count=1,
            analyzed_anchor_count=1,
            status_counts={"completed": 1},
            cache_bypass_enabled=False,
            cache_refresh_count=0,
            cache_hit_count=0,
            cache_miss_count=1,
            completion_mode_counts={"first_pass_completed": 1},
            cross_anchor_merge_count=0,
            context_request_count=0,
            candidate_event_count=0,
            results_summary=[
                {
                    "anchor_unit_id": "oc_1:om_1",
                    "completion_mode": "first_pass_completed",
                    "cache_hit": False,
                    "pass_count": 1,
                    "anchor_status": "completed",
                    "candidate_event_count": 0,
                    "context_request_count": 0,
                    "needs_cross_anchor_merge": False,
                }
            ],
            results=[],
            error_summary="",
        )

    buffer: list[str] = []

    def write_output(text: str) -> int:
        buffer.append(text)
        return len(text)

    exit_code = _main_impl(
        ["--date", "2026-06-23", "--summary-table"],
        write_output,
        config=RuntimeConfig(),
        preflight_func=fake_preflight,
        run_func=fake_run,
    )
    output = "".join(buffer)

    assert exit_code == 0
    assert "anchor_unit_id" in output
    assert "oc_1:om_1" in output
    assert "first_pass_completed" in output


def test_run_anchor_experiment_retries_anchor_with_more_context(tmp_path: Path) -> None:
    from src.worktrace.anchor_experiment import run_anchor_experiment
    from src.worktrace.factories import RuntimeDependencies
    from src.worktrace.models import (
        AttachmentMeta,
        ContextRequest,
        ConversationRef,
        NormalizedMessage,
        SelfIdentity,
    )
    from src.worktrace.constants import ContextRequestType

    class FakeSource:
        def get_self_identity(self):
            return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

        def list_target_conversations(self, target_date, self_identity):
            return [ConversationRef(conversation_id="oc_1", conversation_name="项目群")]

        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_1",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-23T10:00:00+08:00",
                    message_type="audio",
                    text="<audio key=\"att_1\" duration=\"8s\"/>",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[
                        AttachmentMeta(
                            attachment_id="att_1",
                            file_name="voice.m4a",
                            mime_type="audio/*",
                            file_size=1,
                        )
                    ],
                    is_system=False,
                )
            ]

        def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_2",
                    sender_open_id="ou_other",
                    sender_name="Bob",
                    send_time="2026-06-23T10:01:00+08:00",
                    message_type="text",
                    text="补充：今天中午前给结果",
                    reply_to_message_id="om_1",
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                )
            ]

    class FakeResolver:
        def to_text(self, message):
            return message.text

        def load_attachment_text_if_needed(self, message, attachment_ids, hint):
            from src.worktrace.models import AttachmentTextBlock

            return [
                AttachmentTextBlock(
                    attachment_id="att_1",
                    message_id=message.message_id,
                    file_name="voice.m4a",
                    text="语音转写：今天中午前给结果",
                )
            ]

    class FakeAnalyzer:
        def __init__(self):
            self.calls = 0

        def _invoke_codex(self, prompt):
            self.calls += 1
            if self.calls == 1:
                return {
                    "anchor_status": "needs_attachment_text",
                    "candidate_events": [],
                    "context_requests": [
                        {
                            "slice_id": "oc_1:om_1",
                            "request_type": ContextRequestType.ATTACHMENT_TEXT.value,
                            "target_message_ids": ["om_1"],
                            "target_attachment_ids": ["att_1"],
                            "reason": "需要语音转写",
                            "limit": 1,
                        }
                    ],
                    "needs_cross_anchor_merge": False,
                }
            return {
                "anchor_status": "completed",
                "candidate_events": [
                    {
                        "draft_id": "evt_1",
                        "date": "2026-06-23",
                        "topic": "语音事项确认",
                        "content": "补齐语音转写后确认中午前给结果",
                        "result": "",
                        "source_message_ids": ["om_1"],
                        "source_conversation_id": "oc_1",
                        "source_slice_id": "oc_1:om_1",
                        "confidence": 0.9,
                    }
                ],
                "context_requests": [],
                "needs_cross_anchor_merge": False,
            }

    fake_runtime = RuntimeDependencies(
        chat_source=FakeSource(),
        content_resolver=FakeResolver(),
        analyzer=FakeAnalyzer(),
        event_store=None,  # type: ignore[arg-type]
    )

    result = run_anchor_experiment(
        target_date="2026-06-23",
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            cache_root=tmp_path / "cache",
            anchor_retry_limit=3,
        ),
        limit=1,
        dump_dir=tmp_path / "debug",
        runtime=fake_runtime,
    )

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.status_counts == {"completed": 1}
    assert result.candidate_event_count == 1
    assert result.cache_bypass_enabled is False
    assert result.cache_refresh_count == 0
    assert result.cache_hit_count == 0
    assert result.cache_miss_count == 1
    assert result.completion_mode_counts == {"multi_pass_completed": 1}
    assert result.results_summary == [
        {
            "anchor_unit_id": "oc_1:om_1",
            "completion_mode": "multi_pass_completed",
            "cache_hit": False,
            "pass_count": 2,
            "anchor_status": "completed",
            "candidate_event_count": 1,
            "context_request_count": 0,
            "needs_cross_anchor_merge": False,
        }
    ]


def test_run_anchor_experiment_reuses_cached_anchor_result(tmp_path: Path) -> None:
    from src.worktrace.anchor_experiment import run_anchor_experiment
    from src.worktrace.factories import RuntimeDependencies
    from src.worktrace.models import ConversationRef, NormalizedMessage, SelfIdentity

    class FakeSource:
        def get_self_identity(self):
            return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

        def list_target_conversations(self, target_date, self_identity):
            return [ConversationRef(conversation_id="oc_1", conversation_name="项目群")]

        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_1",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-23T10:00:00+08:00",
                    message_type="text",
                    text="今天中午前给你结果",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                )
            ]

        def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
            return []

    class FakeResolver:
        def to_text(self, message):
            return message.text

        def load_attachment_text_if_needed(self, message, attachment_ids, hint):
            return None

    class FakeAnalyzer:
        def __init__(self):
            self.calls = 0

        def _invoke_codex(self, prompt):
            self.calls += 1
            return {
                "anchor_status": "completed",
                "candidate_events": [
                    {
                        "draft_id": "evt_1",
                        "date": "2026-06-23",
                        "topic": "结果确认",
                        "content": "中午前给结果",
                        "result": "",
                        "source_message_ids": ["om_1"],
                        "source_conversation_id": "oc_1",
                        "source_slice_id": "oc_1:om_1",
                        "confidence": 0.9,
                    }
                ],
                "context_requests": [],
                "needs_cross_anchor_merge": False,
            }

    analyzer = FakeAnalyzer()
    fake_runtime = RuntimeDependencies(
        chat_source=FakeSource(),
        content_resolver=FakeResolver(),
        analyzer=analyzer,
        event_store=None,  # type: ignore[arg-type]
    )
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        cache_root=tmp_path / "cache",
        anchor_retry_limit=2,
    )

    first = run_anchor_experiment(
        target_date="2026-06-23",
        config=config,
        limit=1,
        runtime=fake_runtime,
    )
    second = run_anchor_experiment(
        target_date="2026-06-23",
        config=config,
        limit=1,
        runtime=fake_runtime,
    )

    assert first.cache_hit_count == 0
    assert first.cache_miss_count == 1
    assert first.completion_mode_counts == {"first_pass_completed": 1}
    assert first.results_summary[0]["completion_mode"] == "first_pass_completed"
    assert second.cache_hit_count == 1
    assert second.cache_miss_count == 0
    assert second.completion_mode_counts == {"cache_hit": 1}
    assert second.results_summary[0]["completion_mode"] == "cache_hit"
    assert analyzer.calls == 1


def test_run_anchor_experiment_ignore_cache_still_executes_analyzer(tmp_path: Path) -> None:
    from src.worktrace.anchor_experiment import run_anchor_experiment
    from src.worktrace.factories import RuntimeDependencies
    from src.worktrace.models import ConversationRef, NormalizedMessage, SelfIdentity

    class FakeSource:
        def get_self_identity(self):
            return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

        def list_target_conversations(self, target_date, self_identity):
            return [ConversationRef(conversation_id="oc_1", conversation_name="项目群")]

        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_1",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-23T10:00:00+08:00",
                    message_type="text",
                    text="今天中午前给你结果",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                )
            ]

        def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
            return []

    class FakeResolver:
        def to_text(self, message):
            return message.text

        def load_attachment_text_if_needed(self, message, attachment_ids, hint):
            return None

    class FakeAnalyzer:
        def __init__(self):
            self.calls = 0

        def _invoke_codex(self, prompt):
            self.calls += 1
            return {
                "anchor_status": "completed",
                "candidate_events": [],
                "context_requests": [],
                "needs_cross_anchor_merge": False,
            }

    analyzer = FakeAnalyzer()
    fake_runtime = RuntimeDependencies(
        chat_source=FakeSource(),
        content_resolver=FakeResolver(),
        analyzer=analyzer,
        event_store=None,  # type: ignore[arg-type]
    )
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        cache_root=tmp_path / "cache",
        anchor_retry_limit=2,
    )

    run_anchor_experiment(
        target_date="2026-06-23",
        config=config,
        limit=1,
        runtime=fake_runtime,
    )
    result = run_anchor_experiment(
        target_date="2026-06-23",
        config=config,
        limit=1,
        ignore_cache=True,
        runtime=fake_runtime,
    )

    assert result.cache_bypass_enabled is True
    assert result.cache_hit_count == 0
    assert result.cache_miss_count == 1
    assert result.completion_mode_counts == {"first_pass_completed": 1}
    assert result.results_summary[0]["cache_hit"] is False
    assert analyzer.calls == 2


def test_run_anchor_experiment_refresh_cache_invalidates_day(tmp_path: Path) -> None:
    from src.worktrace.anchor_experiment import run_anchor_experiment
    from src.worktrace.factories import RuntimeDependencies
    from src.worktrace.models import ConversationRef, NormalizedMessage, SelfIdentity

    class FakeSource:
        def get_self_identity(self):
            return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

        def list_target_conversations(self, target_date, self_identity):
            return [ConversationRef(conversation_id="oc_1", conversation_name="项目群")]

        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_1",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-23T10:00:00+08:00",
                    message_type="text",
                    text="今天中午前给你结果",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                )
            ]

        def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
            return []

    class FakeResolver:
        def to_text(self, message):
            return message.text

        def load_attachment_text_if_needed(self, message, attachment_ids, hint):
            return None

    class FakeAnalyzer:
        def __init__(self):
            self.calls = 0

        def _invoke_codex(self, prompt):
            self.calls += 1
            return {
                "anchor_status": "completed",
                "candidate_events": [],
                "context_requests": [],
                "needs_cross_anchor_merge": False,
            }

    analyzer = FakeAnalyzer()
    fake_runtime = RuntimeDependencies(
        chat_source=FakeSource(),
        content_resolver=FakeResolver(),
        analyzer=analyzer,
        event_store=None,  # type: ignore[arg-type]
    )
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        cache_root=tmp_path / "cache",
        anchor_retry_limit=2,
    )

    run_anchor_experiment(
        target_date="2026-06-23",
        config=config,
        limit=1,
        runtime=fake_runtime,
    )
    result = run_anchor_experiment(
        target_date="2026-06-23",
        config=config,
        limit=1,
        refresh_cache=True,
        runtime=fake_runtime,
    )

    assert result.cache_bypass_enabled is True
    assert result.cache_refresh_count == 1
    assert result.cache_hit_count == 0
    assert result.cache_miss_count == 1
    assert result.completion_mode_counts == {"first_pass_completed": 1}
    assert result.results_summary[0]["pass_count"] == 1
    assert analyzer.calls == 2


def test_run_anchor_experiment_supports_hook_style_analyzer(tmp_path: Path) -> None:
    from src.worktrace.anchor_experiment import run_anchor_experiment
    from src.worktrace.factories import RuntimeDependencies
    from src.worktrace.models import ConversationRef, NormalizedMessage, SelfIdentity

    class FakeSource:
        def get_self_identity(self):
            return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

        def list_target_conversations(self, target_date, self_identity):
            return [ConversationRef(conversation_id="oc_1", conversation_name="项目群")]

        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_1",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-23T10:00:00+08:00",
                    message_type="text",
                    text="今天中午前给你结果",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                )
            ]

        def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
            return []

    class FakeResolver:
        def to_text(self, message):
            return message.text

        def load_attachment_text_if_needed(self, message, attachment_ids, hint):
            return None

    class FakeHookAnalyzer:
        def __init__(self):
            self.calls = 0

        def _invoke_hook(self, prompt):
            self.calls += 1
            return {
                "anchor_status": "completed",
                "candidate_events": [],
                "context_requests": [],
                "needs_cross_anchor_merge": False,
            }

    analyzer = FakeHookAnalyzer()
    fake_runtime = RuntimeDependencies(
        chat_source=FakeSource(),
        content_resolver=FakeResolver(),
        analyzer=analyzer,
        event_store=None,  # type: ignore[arg-type]
    )

    result = run_anchor_experiment(
        target_date="2026-06-23",
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            cache_root=tmp_path / "cache",
            anchor_retry_limit=2,
        ),
        limit=1,
        runtime=fake_runtime,
    )

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.cache_hit_count == 0
    assert result.cache_miss_count == 1
    assert result.completion_mode_counts == {"first_pass_completed": 1}
    assert analyzer.calls == 1


def test_run_anchor_experiment_batches_first_pass_anchors(tmp_path: Path) -> None:
    from src.worktrace.anchor_experiment import run_anchor_experiment
    from src.worktrace.factories import RuntimeDependencies
    from src.worktrace.models import ConversationRef, NormalizedMessage, SelfIdentity

    class FakeSource:
        def get_self_identity(self):
            return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

        def list_target_conversations(self, target_date, self_identity):
            return [ConversationRef(conversation_id="oc_1", conversation_name="项目群")]

        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_1",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-23T10:00:00+08:00",
                    message_type="text",
                    text="事项一",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                ),
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_2",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-23T10:10:00+08:00",
                    message_type="text",
                    text="事项二",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                ),
            ]

        def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
            return []

    class FakeResolver:
        def to_text(self, message):
            return message.text

        def load_attachment_text_if_needed(self, message, attachment_ids, hint):
            return None

    class FakeAnalyzer:
        def __init__(self):
            self.batch_calls = 0
            self.single_calls = 0

        def analyze_anchor_batch(self, target_date, anchor_units):
            self.batch_calls += 1
            return BatchAnchorAnalysisResult(
                results=[
                    BatchAnchorAnalysisItem(
                        anchor_unit_id=anchor.anchor_unit_id,
                        analysis=AnchorAnalysisResult(
                            anchor_status="completed",
                            candidate_events=[],
                            context_requests=[],
                            needs_cross_anchor_merge=False,
                        ),
                    )
                    for anchor in anchor_units
                ]
            )

        def _invoke_hook(self, prompt):
            self.single_calls += 1
            return {
                "anchor_status": "completed",
                "candidate_events": [],
                "context_requests": [],
                "needs_cross_anchor_merge": False,
            }

    analyzer = FakeAnalyzer()
    fake_runtime = RuntimeDependencies(
        chat_source=FakeSource(),
        content_resolver=FakeResolver(),
        analyzer=analyzer,
        event_store=None,  # type: ignore[arg-type]
    )

    result = run_anchor_experiment(
        target_date="2026-06-23",
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            cache_root=tmp_path / "cache",
            anchor_batch_size=3,
        ),
        runtime=fake_runtime,
    )

    assert result.status == DailyRunStatus.SUCCESS.value
    assert analyzer.batch_calls == 1
    assert analyzer.single_calls == 0


def test_run_anchor_experiment_falls_back_to_single_anchor_when_batch_fails(tmp_path: Path) -> None:
    from src.worktrace.anchor_experiment import run_anchor_experiment
    from src.worktrace.errors import AnalyzerProtocolError
    from src.worktrace.factories import RuntimeDependencies
    from src.worktrace.models import ConversationRef, NormalizedMessage, SelfIdentity

    class FakeSource:
        def get_self_identity(self):
            return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

        def list_target_conversations(self, target_date, self_identity):
            return [ConversationRef(conversation_id="oc_1", conversation_name="项目群")]

        def fetch_conversation_messages(self, target_date, conversation_ids):
            return [
                NormalizedMessage(
                    conversation_id="oc_1",
                    conversation_name="项目群",
                    message_id="om_1",
                    sender_open_id="ou_self",
                    sender_name="Me",
                    send_time="2026-06-23T10:00:00+08:00",
                    message_type="text",
                    text="事项一",
                    reply_to_message_id=None,
                    quote_message_id=None,
                    links=[],
                    attachments=[],
                    is_system=False,
                )
            ]

        def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
            return []

    class FakeResolver:
        def to_text(self, message):
            return message.text

        def load_attachment_text_if_needed(self, message, attachment_ids, hint):
            return None

    class FakeAnalyzer:
        def __init__(self):
            self.batch_calls = 0
            self.single_calls = 0

        def analyze_anchor_batch(self, target_date, anchor_units):
            self.batch_calls += 1
            raise AnalyzerProtocolError("batch failed")

        def _invoke_hook(self, prompt):
            self.single_calls += 1
            return {
                "anchor_status": "completed",
                "candidate_events": [],
                "context_requests": [],
                "needs_cross_anchor_merge": False,
            }

    analyzer = FakeAnalyzer()
    fake_runtime = RuntimeDependencies(
        chat_source=FakeSource(),
        content_resolver=FakeResolver(),
        analyzer=analyzer,
        event_store=None,  # type: ignore[arg-type]
    )

    result = run_anchor_experiment(
        target_date="2026-06-23",
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            cache_root=tmp_path / "cache",
            anchor_batch_size=3,
        ),
        runtime=fake_runtime,
    )

    assert result.status == DailyRunStatus.SUCCESS.value
    assert analyzer.batch_calls == 1
    assert analyzer.single_calls == 1
