from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.worktrace.config import OnlineLLMSettings, RuntimeConfig
from src.worktrace.errors import AnalyzerProtocolError, CodexProtocolViolationError
from src.worktrace.llm_usage import LLMUsageRecorder
from src.worktrace.vision import (
    _build_image_request_body,
    CodexFirstImageSummarizer,
    ImageSummarySettings,
    OnlineImageSummarizer,
)


def test_build_chat_image_request_uses_chat_content_and_disables_thinking() -> None:
    body = _build_image_request_body(
        prompt="摘要",
        image_url="data:image/png;base64,aW1hZ2U=",
        model="test-model",
        stream_enabled=False,
        reasoning_effort="none",
        wire_api="chat_completions",
    )

    assert "input" not in body
    assert "reasoning" not in body
    assert body["extra_body"] == {"thinking": {"type": "disabled"}}
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "摘要\n/no_think"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64,aW1hZ2U=",
            "detail": "low",
        },
    }


def test_online_image_summary_uses_chat_completions(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "required.png"
    image_path.write_bytes(b"image")
    requests: list[dict[str, object]] = []

    class _Completions:
        def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                model_dump=lambda: {
                    "choices": [{"message": {"content": "图片摘要"}}],
                    "usage": {
                        "prompt_tokens": 8,
                        "completion_tokens": 2,
                        "total_tokens": 10,
                    },
                }
            )

    monkeypatch.setattr(
        "src.worktrace.vision.load_online_llm_settings",
        lambda config, **kwargs: OnlineLLMSettings(
            base_url="https://example.test/v1",
            model="test-model",
            api_key="test-key",
            timeout_seconds=1,
            stream_first_response_timeout_seconds=1,
            stream_enabled=False,
            tls_verify=True,
            reasoning_effort="none",
            wire_api="chat_completions",
        ),
    )
    summarizer = OnlineImageSummarizer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        settings=ImageSummarySettings(True, "摘要", 1, 1024),
        client=SimpleNamespace(chat=SimpleNamespace(completions=_Completions())),
    )

    assert summarizer.summarize(image_path) == "图片摘要"
    assert len(requests) == 1
    assert requests[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning" not in requests[0]


def test_required_image_summary_bypasses_optional_image_limit(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "required.png"
    image_path.write_bytes(b"image")
    requests: list[dict[str, object]] = []

    class _Responses:
        def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(output_text="图片摘要")

    monkeypatch.setattr(
        "src.worktrace.vision.load_online_llm_settings",
        lambda config, **kwargs: OnlineLLMSettings(
            base_url="https://example.test/v1",
            model="test-model",
            api_key="test-key",
            timeout_seconds=1,
            stream_first_response_timeout_seconds=1,
            stream_enabled=False,
            tls_verify=True,
            reasoning_effort="none",
            wire_api="responses",
        ),
    )
    summarizer = OnlineImageSummarizer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        settings=ImageSummarySettings(True, "摘要", 0, 1024),
        client=SimpleNamespace(responses=_Responses()),
    )

    assert summarizer.summarize(image_path) == ""
    assert summarizer.summarize(image_path, required=True) == "图片摘要"
    assert len(requests) == 1


def test_image_summary_settings_loads_unrecognized_response_markers(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "image_summary.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "prompt": "摘要",
                "max_images_per_run": 3,
                "max_image_bytes": 1024,
                "unrecognized_response_markers": ["无法查看图片"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    settings = ImageSummarySettings.load(
        RuntimeConfig(data_root=tmp_path / "data"),
        cwd=tmp_path,
    )

    assert settings.unrecognized_response_markers == ("无法查看图片",)


def test_image_summary_settings_rejects_invalid_unrecognized_markers(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "image_summary.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "prompt": "摘要",
                "max_images_per_run": 3,
                "max_image_bytes": 1024,
                "unrecognized_response_markers": [""],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unrecognized_response_markers"):
        ImageSummarySettings.load(
            RuntimeConfig(data_root=tmp_path / "data"),
            cwd=tmp_path,
        )


def test_codex_image_summary_success_does_not_use_online_fallback(tmp_path) -> None:
    image_path = tmp_path / "ok.png"
    image_path.write_bytes(b"image")
    recorder = LLMUsageRecorder()

    class Codex:
        usage_recorder = recorder
        calls = 0

        def request_text(self, prompt, *, image_path):
            self.calls += 1
            recorder.record("image_summary", {}, backend="codex")
            return "主线路图片摘要"

    class Online:
        calls = 0

        def summarize(self, image_path, *, required=False):
            self.calls += 1
            raise AssertionError("Online must not run")

    codex = Codex()
    online = Online()
    summarizer = CodexFirstImageSummarizer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        settings=ImageSummarySettings(True, "摘要", 12, 1024),
        codex=codex,
        online_fallback=online,
        usage_recorder=recorder,
    )

    assert summarizer.summarize(image_path) == "主线路图片摘要"
    assert codex.calls == 1
    assert online.calls == 0
    assert recorder.summary()["fallback_count"] == 0


def test_codex_tool_protocol_violation_uses_online_once_and_records_fallback(
    tmp_path,
) -> None:
    image_path = tmp_path / "fallback.png"
    image_path.write_bytes(b"image")
    recorder = LLMUsageRecorder()

    class Codex:
        usage_recorder = recorder
        calls = 0

        def request_text(self, prompt, *, image_path):
            self.calls += 1
            recorder.record(
                "image_summary",
                {},
                backend="codex",
                status="failed",
                error_category="protocol_violation",
            )
            raise CodexProtocolViolationError(
                "Codex protocol violation: tool or unsupported item was emitted "
                "(item_type=command_execution)."
            )

    class Online:
        calls = 0

        def summarize(self, image_path, *, required=False):
            self.calls += 1
            assert required is True
            recorder.record("image_summary", {}, backend="online")
            return "备用线路图片摘要"

    codex = Codex()
    online = Online()
    summarizer = CodexFirstImageSummarizer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        settings=ImageSummarySettings(True, "摘要", 12, 1024),
        codex=codex,
        online_fallback=online,
        usage_recorder=recorder,
    )

    assert summarizer.summarize(image_path) == "备用线路图片摘要"
    assert codex.calls == 1
    assert online.calls == 1
    primary_record, fallback_record = recorder.records()
    assert primary_record["fallback_from"] == "codex"
    assert primary_record["fallback_to"] == "online"
    assert primary_record["error_category"] == "protocol_violation"
    assert fallback_record["backend"] == "online"
    assert primary_record["request_context_id"] == fallback_record["request_context_id"]
    assert recorder.summary()["fallback_count"] == 1


def test_codex_unrecognized_image_response_uses_configured_online_fallback(
    tmp_path,
) -> None:
    image_path = tmp_path / "unrecognized.png"
    image_path.write_bytes(b"image")
    recorder = LLMUsageRecorder()

    class Codex:
        usage_recorder = recorder

        def request_text(self, prompt, *, image_path):
            recorder.record("image_summary", {}, backend="codex")
            return "当前无法查看图片，请重新提供。"

    class Online:
        def summarize(self, image_path, *, required=False):
            recorder.record("image_summary", {}, backend="online")
            return "备用线路识别成功"

    summarizer = CodexFirstImageSummarizer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        settings=ImageSummarySettings(
            True,
            "摘要",
            12,
            1024,
            ("无法查看图片",),
        ),
        codex=Codex(),
        online_fallback=Online(),
        usage_recorder=recorder,
    )

    assert summarizer.summarize(image_path) == "备用线路识别成功"
    primary_record = recorder.records()[0]
    assert primary_record["status"] == "failed"
    assert primary_record["error_category"] == "image_unrecognized"
    assert primary_record["fallback_to"] == "online"


def test_only_failed_images_use_online_fallback(tmp_path) -> None:
    good_image = tmp_path / "good.png"
    failed_image = tmp_path / "failed.png"
    another_good_image = tmp_path / "another-good.png"
    for path in (good_image, failed_image, another_good_image):
        path.write_bytes(b"image")
    fallback_paths = []

    class Codex:
        def request_text(self, prompt, *, image_path):
            if image_path.name == "failed.png":
                raise CodexProtocolViolationError("unsupported image result")
            return f"摘要：{image_path.name}"

    class Online:
        def summarize(self, image_path, *, required=False):
            fallback_paths.append(image_path)
            return "备用摘要"

    summarizer = CodexFirstImageSummarizer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        settings=ImageSummarySettings(True, "摘要", 12, 1024),
        codex=Codex(),
        online_fallback=Online(),
    )

    assert summarizer.summarize(good_image) == "摘要：good.png"
    assert summarizer.summarize(failed_image) == "备用摘要"
    assert summarizer.summarize(another_good_image) == "摘要：another-good.png"
    assert fallback_paths == [failed_image]


def test_image_protocol_violation_without_online_config_still_fails_safely(
    tmp_path,
) -> None:
    image_path = tmp_path / "failed.png"
    image_path.write_bytes(b"image")

    class Codex:
        def request_text(self, prompt, *, image_path):
            raise CodexProtocolViolationError("unsupported image result")

    summarizer = CodexFirstImageSummarizer(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        settings=ImageSummarySettings(True, "摘要", 12, 1024),
        codex=Codex(),
    )

    with pytest.raises(CodexProtocolViolationError):
        summarizer.summarize(image_path)
