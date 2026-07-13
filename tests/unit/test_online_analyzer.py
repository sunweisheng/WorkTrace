from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.worktrace.analyzers.online import (
    OnlineLLMAnalyzer,
    _apply_soft_no_think,
    _build_responses_request_body,
    _extract_text_from_responses_payload,
    _extract_text_from_responses_stream_event,
    _close_global_client,
)
from src.worktrace.config import OnlineLLMSettings, RuntimeConfig
from src.worktrace.errors import AnalyzerProtocolError
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
        sleep_func=lambda seconds: None,
    )

    result = analyzer.analyze_batch("2026-06-23", sample_batch())

    assert result.candidate_events == []
    assert result.context_requests == []


def test_online_analyzer_parses_stream_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _close_global_client()
    settings = build_settings(stream_enabled=True)

    class FakeEvent:
        def __init__(self, content: str):
            self._content = content

        def model_dump(self):
            return {"type": "response.output_text.delta", "delta": self._content}

    class FakeStream:
        def __enter__(self):
            return iter(
                [
                    FakeEvent('{"candidate_events":[],'),
                    FakeEvent('"context_requests":[]}'),
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
                    FakeEvent('"context_requests":[]}'),
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

    with pytest.raises(AnalyzerProtocolError) as exc_info:
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

    with pytest.raises(AnalyzerProtocolError) as exc_info:
        analyzer.analyze_batch("2026-06-23", sample_batch())

    assert "timed out" in str(exc_info.value).lower()
