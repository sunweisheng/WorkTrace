from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from threading import Event
from time import monotonic

import httpx
import pytest

from src.worktrace.analyzers.output_schemas import (
    batch_output_schema,
    personal_fact_review_output_schema,
    retention_review_output_schema,
)
from src.worktrace.analyzers.function_calls import function_call_spec
from src.worktrace.analyzers.online import (
    _NonStreamRequestTimeoutError,
    OnlineLLMAnalyzer,
    _FirstStreamEventTimeoutError,
    _apply_soft_no_think,
    _build_http_client,
    _build_responses_request_body,
    _extract_text_from_responses_payload,
    _extract_text_from_responses_stream_event,
    _extract_function_arguments_from_responses_payload,
    _extract_stream_function_arguments,
    _parse_function_arguments_with_diagnostics,
    _read_first_stream_event,
    _read_non_stream_response,
)
from src.worktrace.config import (
    OnlineLLMSettings,
    RuntimeConfig,
    load_runtime_config_overrides,
)
from src.worktrace.errors import (
    AnalyzerProtocolError,
    ModelInputLimitError,
    ModelInputRejectedError,
    RetryableAnalyzerProtocolError,
)
from src.worktrace.models import (
    AnalysisBatch,
    ConversationSlice,
    CollectedSourceEvent,
    NormalizedMessage,
    PersonalFactReviewBatch,
    PersonalFactReviewCandidate,
    RetentionReviewBatch,
    RetentionReviewCandidate,
    SourceBackedEventDraft,
    WorkEvent,
)
from src.worktrace.utils.token_estimation import (
    estimate_model_input_tokens,
    estimate_structured_input_tokens,
)


def sample_function_spec():
    return function_call_spec(
        "batch_analysis",
        batch_output_schema(RuntimeConfig()),
        typical_arguments={"candidate_events": [], "context_requests": []},
    )


def function_response(arguments: dict[str, object], *, usage=None) -> dict[str, object]:
    payload: dict[str, object] = {
        "output": [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "submit_batch_analysis",
                "arguments": json.dumps(arguments),
            }
        ]
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def sample_batch() -> AnalysisBatch:
    return AnalysisBatch(
        target_date="2026-06-23",
        batch_id="batch-001",
        retry_round=0,
        estimated_tokens=123,
        slices=[
            ConversationSlice(
                slice_id="slice-1",
                conversation_id="oc_1",
                conversation_name="项目群",
                anchor_message_ids=["om_1"],
                in_day_message_ids=["om_1"],
                messages=[
                    NormalizedMessage(
                        conversation_id="oc_1",
                        conversation_name="项目群",
                        message_id="om_1",
                        sender_open_id="ou_1",
                        sender_name="Alice",
                        send_time="2026-06-23T10:00:00+08:00",
                        message_type="text",
                        text="推进发布",
                        reply_to_message_id=None,
                        quote_message_id=None,
                    )
                ],
            )
        ],
    )


def sample_retention_review_batch() -> RetentionReviewBatch:
    message = sample_batch().slices[0].messages[0]
    candidate = SourceBackedEventDraft(
        draft_id="draft-1",
        date="2026-06-23",
        topic="临时协作复核",
        content="复核原聊天是否形成实质工作。",
        source_message_ids=[message.message_id],
        source_conversation_id=message.conversation_id,
        source_slice_id="slice-1",
        confidence=0.8,
        action_label="确认",
        object_hint="协作事项",
        retention_reason="follow_up_assigned",
        retention_detail="原聊天存在待复核的后续动作。",
    )
    return RetentionReviewBatch(
        target_date="2026-06-23",
        batch_id="retention-review-001",
        candidates=[
            RetentionReviewCandidate(
                candidate=candidate,
                messages=[message],
                allowed_evidence_message_ids=[message.message_id],
            )
        ],
    )


def sample_personal_fact_review_batch() -> PersonalFactReviewBatch:
    retention_batch = sample_retention_review_batch()
    item = retention_batch.candidates[0]
    return PersonalFactReviewBatch(
        target_date=retention_batch.target_date,
        batch_id="personal-fact-review-001",
        candidates=[
            PersonalFactReviewCandidate(
                candidate=item.candidate,
                messages=item.messages,
                allowed_evidence_message_ids=item.allowed_evidence_message_ids,
                review_reasons=["missing_or_incomplete_fact_evidence"],
            )
        ],
    )


def test_online_analyzer_uses_retention_review_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_runtime_config_overrides(
        RuntimeConfig(data_root=tmp_path / "data"),
        cwd=Path.cwd(),
    )
    analyzer = OnlineLLMAnalyzer(config=config, cwd=tmp_path)
    captured: dict[str, object] = {}

    def fake_invoke(prompt, *, function_spec, allow_oversized_input=False):
        captured.update(
            prompt=prompt,
            function_spec=function_spec,
            allow_oversized_input=allow_oversized_input,
        )
        return {
            "results": [
                {
                    "draft_id": "draft-1",
                    "routine_signals": [],
                    "substantive_signals": [
                        {
                            "type": "explicit_business_follow_up",
                            "evidence_message_ids": ["om_1"],
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(analyzer, "_invoke_online", fake_invoke)

    result = analyzer.review_retention_candidates(
        replace(
            sample_retention_review_batch(),
            retry_feedback="证据消息不属于当前候选。",
            oversized_retry=True,
        )
    )

    assert result.results[0].draft_id == "draft-1"
    assert captured["function_spec"].request_kind == "retention_review"
    assert "不要决定保留或删除" in str(captured["prompt"])
    assert "证据消息不属于当前候选。" in str(captured["prompt"])
    assert captured["allow_oversized_input"] is True
    assert captured["function_spec"].parameters != retention_review_output_schema(config)
    assert captured["function_spec"].parameters["properties"]["results"]["minItems"] == 1


def test_online_day_grouping_allows_retry_feedback_to_cross_input_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_runtime_config_overrides(
        RuntimeConfig(data_root=tmp_path / "data"),
        cwd=Path.cwd(),
    )
    analyzer = OnlineLLMAnalyzer(config=config, cwd=tmp_path)
    captured: dict[str, object] = {}
    candidate = sample_retention_review_batch().candidates[0].candidate

    def fake_invoke(prompt, *, function_spec, allow_oversized_input=False):
        captured.update(
            prompt=prompt,
            function_spec=function_spec,
            allow_oversized_input=allow_oversized_input,
        )
        return {
            "merged_groups": [],
            "singleton_draft_ids": [candidate.draft_id],
        }

    monkeypatch.setattr(analyzer, "_invoke_online", fake_invoke)

    result = analyzer.merge_day_candidates(
        candidate.date,
        [candidate],
        validation_feedback="member_connection_evidence_invalid",
    )

    assert result.groups[0].draft_ids == [candidate.draft_id]
    assert captured["allow_oversized_input"] is True
    assert "member_connection_evidence_invalid" in str(captured["prompt"])


def test_online_collected_grouping_adds_assignment_only_on_validation_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_runtime_config_overrides(
        RuntimeConfig(data_root=tmp_path / "data"),
        cwd=Path.cwd(),
    )
    analyzer = OnlineLLMAnalyzer(config=config, cwd=tmp_path)
    captured: list[dict[str, object]] = []
    events = [
        CollectedSourceEvent(
            draft_id,
            draft_id,
            f"{draft_id}.md",
            WorkEvent("2026-07-22", draft_id, draft_id, f"处理 {draft_id}"),
        )
        for draft_id in ("d1", "d2")
    ]
    previous = {
        "merged_groups": [],
        "singleton_draft_ids": ["d1"],
    }

    def fake_invoke(prompt, *, function_spec, allow_oversized_input=False):
        captured.append(
            {
                "prompt": prompt,
                "function_spec": function_spec,
                "allow_oversized_input": allow_oversized_input,
            }
        )
        return {
            "merged_groups": [],
            "singleton_draft_ids": ["d1", "d2"],
        }

    monkeypatch.setattr(analyzer, "_invoke_online", fake_invoke)

    analyzer.group_collected_events("2026-07-22", events, [])
    analyzer.group_collected_events(
        "2026-07-22",
        events,
        [],
        validation_feedback="duplicate_draft_id",
        previous_invalid_assignment=previous,
    )

    initial_prompt = json.loads(str(captured[0]["prompt"]))
    retry_prompt = json.loads(str(captured[1]["prompt"]))
    assert "previous_invalid_assignment" not in initial_prompt
    assert captured[0]["function_spec"].final_parameter_checks
    assert retry_prompt["previous_invalid_assignment"] == previous
    assert (
        captured[1]["function_spec"].final_parameter_checks
        == captured[0]["function_spec"].final_parameter_checks
    )
    assert captured[1]["allow_oversized_input"] is True


def test_online_analyzer_uses_personal_fact_review_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_runtime_config_overrides(
        RuntimeConfig(data_root=tmp_path / "data"),
        cwd=Path.cwd(),
    )
    analyzer = OnlineLLMAnalyzer(config=config, cwd=tmp_path)
    captured: dict[str, object] = {}

    def fake_invoke(prompt, *, function_spec, allow_oversized_input=False):
        captured.update(
            prompt=prompt,
            function_spec=function_spec,
        )
        return {
            "results": [
                {
                    "draft_id": "draft-1",
                    "supported": False,
                    "fact_items": {
                        "topic": {"text": "", "evidence_message_ids": []},
                        "content": [],
                        "action_label": {"text": "", "evidence_message_ids": []},
                        "object_hint": {"text": "", "evidence_message_ids": []},
                        "retention_detail": {"text": "", "evidence_message_ids": []},
                    },
                    "removed_claims": ["原候选没有消息证据"],
                }
            ]
        }

    monkeypatch.setattr(analyzer, "_invoke_online", fake_invoke)

    batch = sample_personal_fact_review_batch()
    candidate = batch.candidates[0]
    context_message = replace(
        candidate.messages[0],
        message_id="context-only",
        text="仅用于理解前后关系",
    )
    batch = replace(
        batch,
        candidates=[
            replace(
                candidate,
                messages=[context_message, *candidate.messages],
            )
        ],
    )

    result = analyzer.review_personal_event_facts(batch)

    assert result.results[0].supported is False
    assert captured["function_spec"].request_kind == "personal_fact_review"
    assert "messages 才是事实来源" in str(captured["prompt"])
    parameters = captured["function_spec"].parameters
    assert parameters["properties"]["results"]["maxItems"] == 1
    evidence_items = parameters["properties"]["results"]["items"]["properties"][
        "fact_items"
    ]["properties"]["topic"]["properties"]["evidence_message_ids"]["items"]
    assert evidence_items["enum"] == candidate.allowed_evidence_message_ids

    prompt_messages = json.loads(captured["prompt"])["input"]["candidates"][0][
        "messages"
    ]
    assert [item["role"] for item in prompt_messages] == ["context", "evidence"]


def build_settings(**overrides: object) -> OnlineLLMSettings:
    base = OnlineLLMSettings(
        base_url="https://llm.example/v1",
        model="provider-model",
        api_key="secret",
        timeout_seconds=30,
        stream_first_response_timeout_seconds=60,
        stream_enabled=False,
        tls_verify=False,
        reasoning_effort=None,
    )
    return OnlineLLMSettings(**(base.__dict__ | overrides))


def test_build_responses_request_body_includes_function_and_reasoning() -> None:
    function_spec = sample_function_spec()
    body = _build_responses_request_body(
        "prompt",
        settings=build_settings(stream_enabled=True, reasoning_effort="none"),
        function_spec=function_spec,
    )

    assert body["model"] == "provider-model"
    assert body["input"].startswith("prompt\n\n典型 Function 参数示例：")
    assert body["input"].endswith("/no_think")
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["reasoning"] == {"effort": "none"}
    assert body["tools"] == [function_spec.tool()]
    assert body["tool_choice"] == function_spec.tool_choice()
    assert body["parallel_tool_calls"] is False
    assert body["tools"][0]["strict"] is True
    assert "text" not in body


def test_apply_soft_no_think_deduplicates_marker() -> None:
    assert _apply_soft_no_think("prompt") == "prompt\n/no_think"
    assert _apply_soft_no_think("prompt\n/no_think") == "prompt\n/no_think"


def test_online_analyzer_rejects_oversized_prompt_before_request(
    tmp_path: Path,
) -> None:
    settings_calls = []
    analyzer = OnlineLLMAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            model_input_batch_target_tokens=1,
        ),
        cwd=tmp_path,
        settings_loader=lambda *args, **kwargs: settings_calls.append(True),
    )

    with pytest.raises(AnalyzerProtocolError, match="model_input_batch_target_tokens"):
        analyzer.analyze_batch("2026-06-23", sample_batch())

    assert settings_calls == []


def test_online_analyzer_allows_marked_indivisible_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = OnlineLLMAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            model_input_batch_target_tokens=1,
        ),
        cwd=tmp_path,
    )
    captured: dict[str, object] = {}

    def fake_prepared(prompt, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(analyzer, "_invoke_online_prepared", fake_prepared)

    assert analyzer._invoke_online(
        "oversized",
        function_spec=sample_function_spec(),
        allow_oversized_input=True,
    ) == {}
    assert captured["oversized_singleton"] is True
    assert captured["estimated_input_tokens"] > captured["input_target_tokens"]


def test_online_analyzer_counts_function_contract_before_request(tmp_path: Path) -> None:
    prompt = "short prompt"
    output_schema = {
        "type": "object",
        "description": "x" * 600,
        "additionalProperties": False,
    }
    function_spec = function_call_spec(
        "batch_analysis",
        output_schema,
        typical_arguments={},
    )
    prompt_only_tokens = estimate_model_input_tokens(
        prompt,
        append_no_think=True,
    )
    settings_calls = []
    analyzer = OnlineLLMAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            model_input_batch_target_tokens=prompt_only_tokens,
        ),
        cwd=tmp_path,
        settings_loader=lambda *args, **kwargs: settings_calls.append(True),
    )

    with pytest.raises(ModelInputLimitError, match="model_input_batch_target_tokens"):
        analyzer.request_function(prompt, function_spec=function_spec)

    assert settings_calls == []


def test_streaming_client_uses_first_response_timeout_before_body() -> None:
    client = _build_http_client(
        build_settings(
            timeout_seconds=1200,
            stream_enabled=True,
            stream_first_response_timeout_seconds=60,
        )
    )
    try:
        assert client.timeout.read == 60
        assert client.timeout.write == 1200
    finally:
        client.close()


def test_first_stream_event_timeout_closes_the_pending_stream() -> None:
    closed = Event()
    observed_read_timeouts: list[float] = []
    request = httpx.Request(
        "POST",
        "https://llm.example/v1/responses",
        extensions={"timeout": {"read": 0.01}},
    )

    class BlockingStream:
        response = httpx.Response(200, request=request)

        def __iter__(self):
            observed_read_timeouts.append(request.extensions["timeout"]["read"])
            closed.wait(timeout=1)
            return iter(())

        def close(self):
            closed.set()

    class FakeResponses:
        def create(self, **kwargs):
            return BlockingStream()

    class FakeClient:
        responses = FakeResponses()

    started_at = monotonic()
    with pytest.raises(_FirstStreamEventTimeoutError):
        _read_first_stream_event(
            FakeClient(),
            {"stream": True},
            first_response_timeout_seconds=0.05,
            subsequent_read_timeout_seconds=1.0,
        )

    assert monotonic() - started_at < 0.5
    assert closed.is_set()
    assert observed_read_timeouts == [1.0]


def test_non_stream_request_timeout_is_a_total_deadline() -> None:
    closed = Event()

    class FakeResponse:
        def model_dump(self):
            return function_response(
                {"candidate_events": [], "context_requests": []}
            )

    class BlockingResponses:
        def create(self, **kwargs):
            closed.wait(timeout=1)
            return FakeResponse()

    class FakeClient:
        responses = BlockingResponses()

        def close(self):
            closed.set()

    started_at = monotonic()
    with pytest.raises(_NonStreamRequestTimeoutError):
        _read_non_stream_response(
            FakeClient(),
            {"stream": False},
            timeout_seconds=0.05,
        )

    assert monotonic() - started_at < 0.5
    assert closed.is_set()


def test_extract_text_from_responses_payload() -> None:
    assert _extract_text_from_responses_payload(
        {"output_text": '{"candidate_events":[],"context_requests":[]}'}
    ) == '{"candidate_events":[],"context_requests":[]}'


def test_extract_text_from_responses_stream_event() -> None:
    assert _extract_text_from_responses_stream_event(
        {"type": "response.output_text.delta", "delta": '{"candidate_events":[]'}
    ) == '{"candidate_events":[]'


def test_non_stream_response_rejects_multiple_function_calls() -> None:
    response = function_response({"candidate_events": [], "context_requests": []})
    response["output"].append(
        {
            "type": "function_call",
            "id": "fc_2",
            "name": "submit_batch_analysis",
            "arguments": '{"candidate_events":[],"context_requests":[]}',
        }
    )

    with pytest.raises(RetryableAnalyzerProtocolError, match="exactly one"):
        _extract_function_arguments_from_responses_payload(
            response,
            expected_name="submit_batch_analysis",
        )


def test_function_arguments_repairs_one_duplicate_comma_outside_string() -> None:
    parsed = _parse_function_arguments_with_diagnostics(
        '{"merged_groups": []\n,, "singleton_draft_ids": ["d1"]}'
    )

    assert parsed.payload == {
        "merged_groups": [],
        "singleton_draft_ids": ["d1"],
    }
    assert parsed.repair is not None
    assert parsed.repair.kind == "single_duplicate_comma_outside_string"
    assert parsed.repair.count == 1
    assert parsed.repair.json_error_line == 2
    assert parsed.repair.json_error_column == 2
    assert len(parsed.repair.arguments_sha256) == 64


def test_function_arguments_repairs_one_trailing_comma_outside_string() -> None:
    parsed = _parse_function_arguments_with_diagnostics(
        '{"merged_groups": [], "singleton_draft_ids": ["d1"],\n}'
    )

    assert parsed.payload == {
        "merged_groups": [],
        "singleton_draft_ids": ["d1"],
    }
    assert parsed.repair is not None
    assert parsed.repair.kind == "single_trailing_comma_outside_string"
    assert parsed.repair.count == 1
    assert parsed.repair.json_error_line == 1
    assert parsed.repair.json_error_column > 0


def test_function_arguments_does_not_repair_commas_inside_string() -> None:
    parsed = _parse_function_arguments_with_diagnostics(
        '{"reason_detail": "逗号,,属于正文", "singleton_draft_ids": []}'
    )

    assert parsed.payload == {
        "reason_detail": "逗号,,属于正文",
        "singleton_draft_ids": [],
    }
    assert parsed.repair is None


@pytest.mark.parametrize(
    "arguments",
    [
        '{"a": 1,, "b": 2,, "c": 3}',
        '{"a": [1,], "b": 2,}',
        '{"a": 1 "b": 2}',
    ],
)
def test_function_arguments_rejects_ambiguous_or_unrelated_json_errors(
    arguments: str,
) -> None:
    with pytest.raises(RetryableAnalyzerProtocolError, match="not valid JSON"):
        _parse_function_arguments_with_diagnostics(arguments)


def test_online_analyzer_records_function_arguments_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = OnlineLLMAnalyzer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        settings_loader=lambda *args, **kwargs: build_settings(),
    )
    parsed = _parse_function_arguments_with_diagnostics(
        '{"candidate_events": [],, "context_requests": []}'
    )

    def fake_invoke(settings, body, *, function_spec):
        return (
            parsed.payload,
            {"usage": {"input_tokens": 10, "output_tokens": 5}},
            parsed.repair,
        )

    monkeypatch.setattr(analyzer, "_invoke_via_sdk", fake_invoke)

    result = analyzer.analyze_batch("2026-06-23", sample_batch())

    assert result.candidate_events == []
    record = analyzer.usage_recorder.records()[0]
    assert (
        record["function_arguments_repair_kind"]
        == "single_duplicate_comma_outside_string"
    )
    assert record["function_arguments_repair_count"] == 1
    assert record["function_arguments_json_error_line"] == 1
    assert record["function_arguments_json_error_column"] > 0
    assert len(str(record["function_arguments_sha256"])) == 64
    assert analyzer.usage_recorder.summary()["function_arguments_repair_count"] == 1


def test_stream_response_rejects_multiple_function_calls() -> None:
    events = [
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "name": "submit_batch_analysis",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": '{"candidate_events":[],"context_requests":[]}',
        },
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_2",
                "name": "submit_batch_analysis",
            },
        },
    ]

    with pytest.raises(RetryableAnalyzerProtocolError, match="exactly one"):
        _extract_stream_function_arguments(
            events,
            expected_name="submit_batch_analysis",
        )


def test_online_analyzer_creates_and_closes_client_for_every_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_queue = [
        build_settings(model="model-a"),
        build_settings(model="model-a"),
        build_settings(model="model-b"),
    ]
    created_clients: list[object] = []
    closed_clients: list[object] = []
    request_models: list[str] = []

    class FakeResponse:
        def model_dump(self):
            return function_response({"candidate_events": [], "context_requests": []})

    class FakeResponses:
        def create(self, **kwargs):
            request_models.append(kwargs["model"])
            return FakeResponse()

    class FakeClient:
        def __init__(self):
            self.responses = FakeResponses()

        def close(self):
            closed_clients.append(self)

    class FakeHttpClient:
        def close(self):
            return None

    def fake_openai(**kwargs):
        client = FakeClient()
        created_clients.append(client)
        return client

    monkeypatch.setattr("src.worktrace.analyzers.online._build_http_client", lambda settings: FakeHttpClient())
    monkeypatch.setattr("src.worktrace.analyzers.online.OpenAI", fake_openai)

    analyzer = OnlineLLMAnalyzer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        settings_loader=lambda *args, **kwargs: settings_queue.pop(0),
    )

    analyzer.analyze_batch("2026-06-23", sample_batch())
    analyzer.analyze_batch("2026-06-23", sample_batch())
    analyzer.analyze_batch("2026-06-23", sample_batch())

    assert len(created_clients) == 3
    assert closed_clients == created_clients
    assert request_models == ["model-a", "model-a", "model-b"]


def test_online_analyzer_parses_non_stream_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings()

    class FakeResponse:
        def model_dump(self):
            return function_response(
                {"candidate_events": [], "context_requests": []},
                usage={"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
            )

    class FakeResponses:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        def __init__(self):
            self.responses = FakeResponses()

        def close(self):
            return None

    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("src.worktrace.analyzers.online._build_http_client", lambda settings: FakeHttpClient())
    monkeypatch.setattr("src.worktrace.analyzers.online.OpenAI", lambda **kwargs: FakeClient())

    analyzer = OnlineLLMAnalyzer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        settings_loader=lambda *args, **kwargs: settings,
    )

    result = analyzer.analyze_batch("2026-06-23", sample_batch())

    assert result.candidate_events == []
    assert result.context_requests == []
    assert analyzer.usage_recorder is not None
    assert analyzer.usage_recorder.summary()["output_tokens"] == 7


def test_online_analyzer_parses_stream_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings(stream_enabled=True)

    class FakeEvent:
        def __init__(self, payload: dict[str, object]):
            self._payload = payload

        def model_dump(self):
            return self._payload

    class FakeResponses:
        def __init__(self):
            pass

        def create(self, **kwargs):
            return iter(
                [
                    FakeEvent(
                        {
                            "type": "response.output_item.added",
                            "item": {
                                "type": "function_call",
                                "id": "fc_1",
                                "name": "submit_batch_analysis",
                            },
                        }
                    ),
                    FakeEvent(
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": "fc_1",
                            "delta": '{"candidate_events":[],',
                        }
                    ),
                    FakeEvent(
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": "fc_1",
                            "delta": '"context_requests":[]}',
                        }
                    ),
                    FakeEvent(
                        {
                            "type": "response.completed",
                            "response": {"usage": {"output_tokens": 5}},
                        }
                    ),
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

        def close(self):
            return None

    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("src.worktrace.analyzers.online._build_http_client", lambda settings: FakeHttpClient())
    monkeypatch.setattr("src.worktrace.analyzers.online.OpenAI", lambda **kwargs: FakeClient())

    analyzer = OnlineLLMAnalyzer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        settings_loader=lambda *args, **kwargs: settings,
    )

    result = analyzer.analyze_batch("2026-06-23", sample_batch())

    assert result.candidate_events == []
    assert result.context_requests == []
    assert analyzer.usage_recorder is not None
    assert analyzer.usage_recorder.summary()["output_tokens"] == 5


def test_online_analyzer_does_not_reapply_first_event_timeout_after_stream_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = build_settings(
        stream_enabled=True,
        timeout_seconds=1,
        stream_first_response_timeout_seconds=0.1,
    )
    request = httpx.Request(
        "POST",
        "https://llm.example/v1/responses",
        extensions={"timeout": {"read": 0.01}},
    )

    class FakeEvent:
        def __init__(self, payload: dict[str, object]):
            self.payload = payload

        def model_dump(self):
            return self.payload

    class DelayedSecondEventStream:
        response = httpx.Response(200, request=request)

        def __iter__(self):
            assert request.extensions["timeout"]["read"] == 1
            yield FakeEvent(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "name": "submit_batch_analysis",
                    },
                }
            )
            Event().wait(timeout=0.15)
            yield FakeEvent(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "delta": '{"candidate_events":[],"context_requests":[]}',
                }
            )

        def close(self):
            return None

    class FakeResponses:
        def create(self, **kwargs):
            return DelayedSecondEventStream()

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

        def close(self):
            return None

    class FakeHttpClient:
        def close(self):
            return None

    monkeypatch.setattr(
        "src.worktrace.analyzers.online._build_http_client",
        lambda settings: FakeHttpClient(),
    )
    monkeypatch.setattr(
        "src.worktrace.analyzers.online.OpenAI",
        lambda **kwargs: FakeClient(),
    )

    analyzer = OnlineLLMAnalyzer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        settings_loader=lambda *args, **kwargs: settings,
    )

    result = analyzer.analyze_batch("2026-06-23", sample_batch())

    assert result.candidate_events == []
    assert result.context_requests == []


def test_online_analyzer_wraps_invalid_stream_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = build_settings(stream_enabled=True)

    class InvalidStream:
        def __iter__(self):
            raise json.JSONDecodeError("invalid stream event", "", 0)

    class FakeResponses:
        def create(self, **kwargs):
            return InvalidStream()

    class FakeClient:
        def __init__(self):
            self.responses = FakeResponses()

        def close(self):
            return None

    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "src.worktrace.analyzers.online._build_http_client",
        lambda settings: FakeHttpClient(),
    )
    monkeypatch.setattr(
        "src.worktrace.analyzers.online.OpenAI",
        lambda **kwargs: FakeClient(),
    )

    analyzer = OnlineLLMAnalyzer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        settings_loader=lambda *args, **kwargs: settings,
    )

    with pytest.raises(RetryableAnalyzerProtocolError) as exc_info:
        analyzer.analyze_batch("2026-06-23", sample_batch())

    assert "stream contained invalid JSON" in str(exc_info.value)


def test_online_analyzer_surfaces_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings()

    class FakeClient:
        def __init__(self, **kwargs):
            class FakeResponses:
                def create(self, **kwargs):
                    from openai import APITimeoutError
                    import httpx

                    raise APITimeoutError(request=httpx.Request("POST", "https://llm.example/v1/responses"))

            self.responses = FakeResponses()

        def close(self):
            return None

    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("src.worktrace.analyzers.online._build_http_client", lambda settings: FakeHttpClient())
    monkeypatch.setattr("src.worktrace.analyzers.online.OpenAI", lambda **kwargs: FakeClient())

    analyzer = OnlineLLMAnalyzer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        settings_loader=lambda *args, **kwargs: settings,
    )

    with pytest.raises(RetryableAnalyzerProtocolError) as exc_info:
        analyzer.analyze_batch("2026-06-23", sample_batch())

    assert "timed out" in str(exc_info.value).lower()


def test_online_analyzer_surfaces_first_stream_event_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = build_settings(stream_enabled=True)
    analyzer = OnlineLLMAnalyzer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
        settings_loader=lambda *args, **kwargs: settings,
    )

    def raise_first_event_timeout(settings, body, *, function_spec):
        raise _FirstStreamEventTimeoutError("first event timed out")

    monkeypatch.setattr(analyzer, "_invoke_via_sdk", raise_first_event_timeout)

    with pytest.raises(RetryableAnalyzerProtocolError, match="first stream event"):
        analyzer.analyze_batch("2026-06-23", sample_batch())


def test_online_analyzer_classifies_retryable_and_permanent_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from openai import (
        APIConnectionError,
        APIStatusError,
        AuthenticationError,
        BadRequestError,
        PermissionDeniedError,
        RateLimitError,
    )

    request = httpx.Request("POST", "https://llm.example/v1/responses")

    def status_error(error_type, status_code: int, message: str):
        return error_type(
            message,
            response=httpx.Response(status_code, request=request),
            body=None,
        )

    cases = [
        (
            status_error(AuthenticationError, 401, "bad auth"),
            AnalyzerProtocolError,
        ),
        (
            status_error(PermissionDeniedError, 403, "forbidden"),
            AnalyzerProtocolError,
        ),
        (
            status_error(BadRequestError, 400, "bad request"),
            ModelInputRejectedError,
        ),
        (
            status_error(APIStatusError, 500, "server error"),
            RetryableAnalyzerProtocolError,
        ),
        (
            status_error(APIStatusError, 408, "request timeout"),
            RetryableAnalyzerProtocolError,
        ),
        (
            status_error(RateLimitError, 429, "rate limited"),
            RetryableAnalyzerProtocolError,
        ),
        (
            APIConnectionError(message="connection failed", request=request),
            RetryableAnalyzerProtocolError,
        ),
        (
            APIConnectionError(message="certificate verify failed", request=request),
            AnalyzerProtocolError,
        ),
    ]

    class FakeHttpClient:
        def close(self):
            return None

    monkeypatch.setattr(
        "src.worktrace.analyzers.online._build_http_client",
        lambda settings: FakeHttpClient(),
    )

    for raised_error, expected_type in cases:
        class FakeResponses:
            def create(self, **kwargs):
                raise raised_error

        class FakeClient:
            def __init__(self):
                self.responses = FakeResponses()

            def close(self):
                return None

        monkeypatch.setattr(
            "src.worktrace.analyzers.online.OpenAI",
            lambda **kwargs: FakeClient(),
        )
        analyzer = OnlineLLMAnalyzer(
            config=RuntimeConfig(data_root=tmp_path / "data"),
            cwd=tmp_path,
            settings_loader=lambda *args, **kwargs: build_settings(),
        )

        with pytest.raises(AnalyzerProtocolError) as exc_info:
            analyzer.analyze_batch("2026-06-23", sample_batch())

        assert type(exc_info.value) is expected_type
