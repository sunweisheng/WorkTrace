from __future__ import annotations

from types import SimpleNamespace

from src.worktrace.config import OnlineLLMSettings, RuntimeConfig
from src.worktrace.vision import ImageSummarySettings, OnlineImageSummarizer


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
        lambda config: OnlineLLMSettings(
            base_url="https://example.test/v1",
            model="test-model",
            api_key="test-key",
            timeout_seconds=1,
            stream_first_response_timeout_seconds=1,
            stream_enabled=False,
            tls_verify=True,
            sleep_min_seconds=0,
            sleep_max_seconds=0,
            reasoning_effort="none",
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
