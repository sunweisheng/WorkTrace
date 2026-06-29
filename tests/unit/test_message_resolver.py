from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

from src.worktrace.config import RuntimeConfig
from src.worktrace.models import AttachmentMeta, LinkMeta, NormalizedMessage
from src.worktrace.resolvers.feishu_message import FeishuMessageContentResolver


def test_message_resolver_extracts_text_and_links(tmp_path: Path) -> None:
    resolver = FeishuMessageContentResolver(config=RuntimeConfig(data_root=tmp_path / "data"))
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="请看文档 https://foo.feishu.cn/docx/abc",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[LinkMeta(url="https://foo.feishu.cn/docx/abc", title="方案", link_type="feishu_doc")],
        attachments=[AttachmentMeta(attachment_id="att_1", file_name="a.txt", mime_type="text/plain", file_size=1)],
        is_system=False,
    )

    text = resolver.to_text(message)
    loaded = resolver.load_attachment_text_if_needed(message, ["att_1"], "补充正文")

    assert "方案: https://foo.feishu.cn/docx/abc" in text
    assert loaded is not None
    assert loaded[0].attachment_id == "att_1"


def test_message_resolver_fetches_doc_title_for_bare_feishu_url(tmp_path: Path) -> None:
    call_count = 0

    def fake_runner(args):
        nonlocal call_count
        call_count += 1
        return CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "document": {
                            "content": "<title>竞品逾期调研</title><p>内容</p>"
                        }
                    }
                }
            ),
            stderr="",
        )

    resolver = FeishuMessageContentResolver(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        command_runner=fake_runner,
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="文档在这里 https://ipadnexsg1.feishu.cn/docx/H5gCdcJUWotOm1xUAEkc51Dxnff?from=from",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )

    links = resolver.extract_links(message)
    links_again = resolver.extract_links(message)

    assert links[0].title == "竞品逾期调研"
    assert links_again[0].title == "竞品逾期调研"
    assert call_count == 1
