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


def test_parse_content_extracts_post_link_title() -> None:
    source = FeishuCliChatSource(config=RuntimeConfig())

    parsed = source._parse_content(  # noqa: SLF001 - unit test on parser contract
        {
            "zh_cn": {
                "title": "日报",
                "content": [
                    [
                        {
                            "tag": "text",
                            "text": "涉及文件链接：",
                        }
                    ],
                    [
                        {
                            "tag": "a",
                            "text": "需求评审纪要",
                            "href": "https://ipadnexsg1.feishu.cn/docx/H5gCdcJUWotOm1xUAEkc51Dxnff",
                        }
                    ],
                ],
            }
        }
    )

    assert parsed["links"] == [
        {
            "url": "https://ipadnexsg1.feishu.cn/docx/H5gCdcJUWotOm1xUAEkc51Dxnff",
            "title": "需求评审纪要",
            "link_type": "feishu_doc",
        }
    ]


def test_parse_content_prefers_named_link_when_url_repeats() -> None:
    source = FeishuCliChatSource(config=RuntimeConfig())

    parsed = source._parse_content(  # noqa: SLF001 - unit test on parser contract
        {
            "text": "https://foo.feishu.cn/docx/abc",
            "lines": [
                [
                    {
                        "href": "https://foo.feishu.cn/docx/abc",
                        "text": "",
                    }
                ],
                [
                    {
                        "href": "https://foo.feishu.cn/docx/abc",
                        "text": "发布方案",
                    }
                ],
            ],
        }
    )

    assert parsed["links"] == [
        {
            "url": "https://foo.feishu.cn/docx/abc",
            "title": "发布方案",
            "link_type": "feishu_doc",
        }
    ]
