from __future__ import annotations

import json
from types import SimpleNamespace

from src.worktrace.config import RuntimeConfig
from src.worktrace.models import SelfIdentity
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


def test_parse_content_extracts_inline_image_keys() -> None:
    source = FeishuCliChatSource(config=RuntimeConfig())

    parsed = source._parse_content(  # noqa: SLF001 - unit test on parser contract
        "[Image: img_v3_first] ![Image](img_v3_second)"
    )

    assert {item["attachment_id"] for item in parsed["attachments"]} == {
        "img_v3_first",
        "img_v3_second",
    }


def test_normalize_message_extracts_image_key_from_raw_text() -> None:
    source = FeishuCliChatSource(config=RuntimeConfig())

    message = source._normalize_message(  # noqa: SLF001 - parser contract
        {
            "chat_id": "oc_1",
            "message_id": "om_1",
            "sender_open_id": "ou_self",
            "send_time": "2026-07-10T09:00:00+08:00",
            "msg_type": "image",
            "text": "[Image: img_v3_raw]",
        }
    )

    assert [item.attachment_id for item in message.attachments] == ["img_v3_raw"]


def test_normalize_message_extracts_mention_and_nested_reaction_details() -> None:
    source = FeishuCliChatSource(config=RuntimeConfig())

    message = source._normalize_message(  # noqa: SLF001 - parser contract
        {
            "chat_id": "oc_1",
            "chat_name": "项目群",
            "message_id": "om_1",
            "sender_open_id": "ou_ding",
            "send_time": "2026-07-10T09:00:00+08:00",
            "content": {
                "tag": "at",
                "user": {"open_id": "ou_yuhuan"},
                "text": "张玉环",
            },
            "reactions": [
                {
                    "reaction_type": {"emoji_type": "THUMBSUP"},
                    "details": [
                        {
                            "message_reaction_id": "reaction-1",
                            "operator": {"open_id": "ou_self"},
                            "create_time": "1783645260000",
                        }
                    ],
                }
            ],
        }
    )

    assert message.mentioned_open_ids == ["ou_yuhuan"]
    assert [item.reaction_id for item in message.reactions] == ["reaction-1"]
    assert [item.operator_open_id for item in message.reactions] == ["ou_self"]
    assert [item.emoji_type for item in message.reactions] == ["THUMBSUP"]


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


def test_normalize_message_corrects_sentence_final_ma() -> None:
    source = FeishuCliChatSource(config=RuntimeConfig())

    message = source._normalize_message(  # noqa: SLF001 - unit test on parser contract
        {
            "chat_id": "oc_1",
            "chat_name": "项目群",
            "message_id": "om_1",
            "sender_open_id": "ou_self",
            "send_time": "2026-06-29T09:00:00+08:00",
            "text": "今天能发版妈？",
        }
    )

    assert message.text == "今天能发版吗？"


def test_list_target_conversations_skips_blacklisted_conversation_ids() -> None:
    def fake_runner(args):
        assert args[2] == "+messages-search"
        payload = {
            "items": [
                {
                    "chat_id": "oc_blocked",
                    "chat_name": "黑名单会话",
                    "message_id": "om_1",
                    "sender_open_id": "ou_self",
                    "send_time": "2026-06-29T09:00:00+08:00",
                    "text": "同步一下",
                },
                {
                    "chat_id": "oc_allowed",
                    "chat_name": "正常会话",
                    "message_id": "om_2",
                    "sender_open_id": "ou_self",
                    "send_time": "2026-06-29T10:00:00+08:00",
                    "text": "继续推进",
                },
            ]
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    source = FeishuCliChatSource(
        config=RuntimeConfig(excluded_conversation_ids=("oc_blocked",)),
        command_runner=fake_runner,
    )

    results = source.list_target_conversations(
        "2026-06-29",
        SelfIdentity(open_id="ou_self", display_name="self", source="test"),
    )

    assert [item.conversation_id for item in results] == ["oc_allowed"]


def test_list_target_conversations_includes_reaction_only_conversation() -> None:
    def fake_runner(args):
        if "--sender" in args:
            payload = {"items": []}
        else:
            payload = {
                "items": [
                    {
                        "chat_id": "oc_reaction",
                        "chat_name": "表情会话",
                        "message_id": "om_1",
                        "sender_open_id": "ou_other",
                        "send_time": "2026-06-29T09:00:00+08:00",
                        "text": "请确认发布方案",
                        "reactions": [
                            {
                                "reaction_id": "reaction-1",
                                "operator_open_id": "ou_self",
                                "emoji_type": "THUMBSUP",
                                "action_time": "2026-06-29T09:01:00+08:00",
                            }
                        ],
                    }
                ]
            }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    source = FeishuCliChatSource(config=RuntimeConfig(), command_runner=fake_runner)

    results = source.list_target_conversations(
        "2026-06-29",
        SelfIdentity(open_id="ou_self", display_name="self", source="test"),
    )

    assert [item.conversation_id for item in results] == ["oc_reaction"]


def test_fetch_conversation_messages_skips_blacklisted_conversation_ids() -> None:
    calls: list[tuple[str, ...]] = []

    def fake_runner(args):
        calls.append(tuple(args))
        chat_id = args[args.index("--chat-id") + 1]
        payload = {
            "items": [
                {
                    "chat_id": chat_id,
                    "chat_name": f"name-{chat_id}",
                    "message_id": f"om-{chat_id}",
                    "sender_open_id": "ou_self",
                    "send_time": "2026-06-29T11:00:00+08:00",
                    "text": "消息正文",
                }
            ]
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    source = FeishuCliChatSource(
        config=RuntimeConfig(excluded_conversation_ids=("oc_blocked",)),
        command_runner=fake_runner,
    )

    results = source.fetch_conversation_messages(
        "2026-06-29",
        ["oc_blocked", "oc_allowed"],
    )

    assert [item.conversation_id for item in results] == ["oc_allowed"]
    assert len(calls) == 1
    assert "--chat-id" in calls[0]
    assert calls[0][calls[0].index("--chat-id") + 1] == "oc_allowed"
