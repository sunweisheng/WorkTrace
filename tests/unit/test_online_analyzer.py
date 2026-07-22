from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from time import monotonic

import httpx
import pytest

from src.worktrace.analyzers.output_schemas import (
    personal_fact_review_output_schema,
    retention_review_output_schema,
)
from src.worktrace.analyzers.online import (
    OnlineLLMAnalyzer,
    _FirstStreamEventTimeoutError,
    _apply_soft_no_think,
    _build_http_client,
    _build_responses_request_body,
    _extract_text_from_responses_payload,
    _extract_text_from_responses_stream_event,
    _close_global_client,
    _read_first_stream_event,
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
    NormalizedMessage,
    PersonalFactReviewBatch,
    PersonalFactReviewCandidate,
    RetentionReviewBatch,
    RetentionReviewCandidate,
    SourceBackedEventDraft,
)
from src.worktrace.utils.token_estimation import estimate_model_input_tokens


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

    def fake_invoke(prompt, *, output_schema, request_kind):
        captured.update(
            prompt=prompt,
            output_schema=output_schema,
            request_kind=request_kind,
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

    result = analyzer.review_retention_candidates(sample_retention_review_batch())

    assert result.results[0].draft_id == "draft-1"
    assert captured["request_kind"] == "retention_review"
    assert "不要决定保留或删除" in str(captured["prompt"])
    assert captured["output_schema"] == retention_review_output_schema(config)


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

    def fake_invoke(prompt, *, output_schema, request_kind):
        captured.update(
            prompt=prompt,
            output_schema=output_schema,
            request_kind=request_kind,
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
                        "workstream_key": {"text": "", "evidence_message_ids": []},
                    },
                    "removed_claims": ["原候选没有消息证据"],
                }
            ]
        }

    monkeypatch.setattr(analyzer, "_invoke_online", fake_invoke)

    result = analyzer.review_personal_event_facts(sample_personal_fact_review_batch())

    assert result.results[0].supported is False
    assert captured["request_kind"] == "personal_fact_review"
    assert "messages 才是事实来源" in str(captured["prompt"])
    assert captured["output_schema"] == personal_fact_review_output_schema(
        sample_personal_fact_review_batch()
    )


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


def test_build_responses_request_body_includes_stream_schema_and_reasoning() -> None:
    body = _build_responses_request_body(
        "prompt",
        settings=build_settings(stream_enabled=True, reasoning_effort="none"),
        schema={"type": "object"},
    )

    assert body["model"] == "provider-model"
    assert body["input"] == "prompt\n/no_think"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["reasoning"] == {"effort": "none"}
    assert body["text"]["format"]["schema"] == {"type": "object"}


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
        request_kind="segment_batch_analysis",
        allow_oversized_input=True,
    ) == {}
    assert captured["oversized_singleton"] is True
    assert captured["estimated_input_tokens"] > captured["input_target_tokens"]


def test_online_analyzer_counts_output_schema_before_request(tmp_path: Path) -> None:
    prompt = "short prompt"
    output_schema = {
        "type": "object",
        "description": "x" * 600,
        "additionalProperties": False,
    }
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
        analyzer.request_json(prompt, output_schema=output_schema)

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


def test_extract_text_from_responses_payload() -> None:
    assert _extract_text_from_responses_payload(
        {"output_text": '{"candidate_events":[],"context_requests":[]}'}
    ) == '{"candidate_events":[],"context_requests":[]}'


def test_extract_text_from_responses_stream_event() -> None:
    assert _extract_text_from_responses_stream_event(
        {"type": "response.output_text.delta", "delta": '{"candidate_events":[]'}
    ) == '{"candidate_events":[]'


def test_online_analyzer_reuses_global_singleton_until_settings_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _close_global_client()
    settings_queue = [
        build_settings(model="model-a"),
        build_settings(model="model-a"),
        build_settings(model="model-b"),
    ]
    created_clients: list[object] = []

    class FakeResponse:
        def model_dump(self):
            return {"output_text": '{"candidate_events":[],"context_requests":[]}'}

    class FakeResponses:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        def __init__(self):
            self.responses = FakeResponses()

        def close(self):
            return None

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

    assert len(created_clients) == 2


def test_online_analyzer_parses_non_stream_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _close_global_client()
    settings = build_settings()

    class FakeResponse:
        def model_dump(self):
            return {
                "output_text": '{"candidate_events":[],"context_requests":[]}',
                "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
            }

    class FakeResponses:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        def __init__(self):
            self.responses = FakeResponses()

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
    _close_global_client()
    settings = build_settings(stream_enabled=True)

    class FakeEvent:
        def __init__(self, content: str, usage: dict[str, int] | None = None):
            self._content = content
            self._usage = usage

        def model_dump(self):
            payload = {"type": "response.output_text.delta", "delta": self._content}
            if self._usage is not None:
                payload["response"] = {"usage": self._usage}
            return payload

    class FakeStream:
        def __enter__(self):
            return iter(
                [
                    FakeEvent('{"candidate_events":[],'),
                    FakeEvent('"context_requests":[]}', {"output_tokens": 5}),
                ]
            )

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeResponses:
        def __init__(self):
            pass

        def create(self, **kwargs):
            return iter(
                [
                    FakeEvent('{"candidate_events":[],'),
                    FakeEvent('"context_requests":[]}', {"output_tokens": 5}),
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

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
    _close_global_client()
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
        def __init__(self, content: str):
            self.content = content

        def model_dump(self):
            return {"type": "response.output_text.delta", "delta": self.content}

    class DelayedSecondEventStream:
        response = httpx.Response(200, request=request)

        def __iter__(self):
            assert request.extensions["timeout"]["read"] == 1
            yield FakeEvent('{"candidate_events":[],')
            Event().wait(timeout=0.15)
            yield FakeEvent('"context_requests":[]}')

        def close(self):
            return None

    class FakeResponses:
        def create(self, **kwargs):
            return DelayedSecondEventStream()

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

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
    _close_global_client()
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
    _close_global_client()
    settings = build_settings()

    class FakeClient:
        def __init__(self, **kwargs):
            class FakeResponses:
                def create(self, **kwargs):
                    from openai import APITimeoutError
                    import httpx

                    raise APITimeoutError(request=httpx.Request("POST", "https://llm.example/v1/responses"))

            self.responses = FakeResponses()

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

    def raise_first_event_timeout(settings, body):
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
        _close_global_client()

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
