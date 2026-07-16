from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.worktrace.analyzers.online import (
    OnlineLLMAnalyzer,
    _apply_soft_no_think,
    _build_http_client,
    _build_responses_request_body,
    _extract_text_from_responses_payload,
    _extract_text_from_responses_stream_event,
    _close_global_client,
)
from src.worktrace.config import OnlineLLMSettings, RuntimeConfig
from src.worktrace.errors import (
    AnalyzerProtocolError,
    RetryableAnalyzerProtocolError,
)
from src.worktrace.models import AnalysisBatch, ConversationSlice, NormalizedMessage


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


def build_settings(**overrides: object) -> OnlineLLMSettings:
    base = OnlineLLMSettings(
        base_url="https://llm.example/v1",
        model="provider-model",
        api_key="secret",
        timeout_seconds=30,
        stream_first_response_timeout_seconds=60,
        stream_enabled=False,
        tls_verify=False,
        sleep_min_seconds=1.0,
        sleep_max_seconds=2.0,
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


def test_streaming_client_uses_first_response_timeout_for_reads() -> None:
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


def test_extract_text_from_responses_payload() -> None:
    assert _extract_text_from_responses_payload(
        {"output_text": '{"candidate_events":[],"context_requests":[]}'}
    ) == '{"candidate_events":[],"context_requests":[]}'


def test_extract_text_from_responses_stream_event() -> None:
    assert _extract_text_from_responses_stream_event(
        {"type": "response.output_text.delta", "delta": '{"candidate_events":[]'}
    ) == '{"candidate_events":[]'


def test_online_analyzer_sleeps_after_first_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _close_global_client()
    sleep_calls: list[float] = []
    settings = build_settings()

    class FakeResponse:
        def model_dump(self):
            return {"output_text": '{"candidate_events":[],"context_requests":[]}'}

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
        sleep_func=lambda seconds: sleep_calls.append(seconds),
        random_uniform=lambda start, end: 1.5,
    )

    analyzer.analyze_batch("2026-06-23", sample_batch())
    analyzer.analyze_batch("2026-06-23", sample_batch())

    assert sleep_calls == [1.5]


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
        sleep_func=lambda seconds: None,
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
        sleep_func=lambda seconds: None,
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
        sleep_func=lambda seconds: None,
    )

    result = analyzer.analyze_batch("2026-06-23", sample_batch())

    assert result.candidate_events == []
    assert result.context_requests == []
    assert analyzer.usage_recorder is not None
    assert analyzer.usage_recorder.summary()["output_tokens"] == 5


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
        sleep_func=lambda seconds: None,
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
        sleep_func=lambda seconds: None,
    )

    with pytest.raises(RetryableAnalyzerProtocolError) as exc_info:
        analyzer.analyze_batch("2026-06-23", sample_batch())

    assert "timed out" in str(exc_info.value).lower()


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
            AnalyzerProtocolError,
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
            sleep_func=lambda seconds: None,
        )

        with pytest.raises(AnalyzerProtocolError) as exc_info:
            analyzer.analyze_batch("2026-06-23", sample_batch())

        assert type(exc_info.value) is expected_type
