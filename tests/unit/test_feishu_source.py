from __future__ import annotations

from src.worktrace.config import RuntimeConfig
from src.worktrace.sources.feishu_cli import FeishuCliChatSource


def test_parse_content_extracts_media_attachments() -> None:
    source = FeishuCliChatSource(config=RuntimeConfig())

    parsed = source._parse_content(  # noqa: SLF001 - unit test on parser contract
        {
            "image_key": "img_1",
            "audio_key": "audio_1",
            "video_key": "video_1",
            "file_name": "voice-note.m4a",
        }
    )

    attachment_ids = {item["attachment_id"] for item in parsed["attachments"]}

    assert "img_1" in attachment_ids
    assert "audio_1" in attachment_ids
    assert "video_1" in attachment_ids
