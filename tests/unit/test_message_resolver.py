from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

from src.worktrace.config import RuntimeConfig
from src.worktrace.models import AttachmentMeta, LinkMeta, NormalizedMessage
from src.worktrace.resolvers.feishu_message import FeishuMessageContentResolver


def test_message_resolver_extracts_text_and_links(tmp_path: Path) -> None:
    class FakeExtractor:
        def is_supported(self, file_name: str) -> bool:
            return file_name == "a.txt"

        def extract(self, path: Path, *, file_name: str) -> str:
            return path.read_text(encoding="utf-8")

    def fake_download(message, attachment_id):
        assert message.message_id == "om_1"
        assert attachment_id == "att_1"
        path = tmp_path / "a.txt"
        path.write_text("附件里的业务结论", encoding="utf-8")
        return path

    resolver = FeishuMessageContentResolver(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        text_attachment_extractor=FakeExtractor(),
        attachment_downloader=fake_download,
    )
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
    assert loaded[0].text == "附件正文：附件里的业务结论"


def test_message_resolver_skips_unsupported_attachment_without_download(tmp_path: Path) -> None:
    class FakeExtractor:
        def is_supported(self, file_name: str) -> bool:
            return False

    resolver = FeishuMessageContentResolver(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        text_attachment_extractor=FakeExtractor(),
        attachment_downloader=lambda *_: (_ for _ in ()).throw(AssertionError("must not download")),
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_file",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="file",
        text='<file key="file_1" name="方案.pdf"/>',
        reply_to_message_id=None,
        quote_message_id=None,
        attachments=[AttachmentMeta("file_1", "方案.pdf", "application/pdf", 1)],
    )

    assert resolver.load_attachment_text_if_needed(message, ["file_1"], "需要正文") is None


def test_message_resolver_summarizes_image_with_transient_download(tmp_path: Path) -> None:
    class FakeImageSummarizer:
        def summarize(self, image_path: Path) -> str:
            assert image_path.read_bytes() == b"image-bytes"
            return "图片中标注了两家代理商。"

    def fake_download(message, attachment_id):
        assert message.message_id == "om_image"
        assert attachment_id == "img_1"
        path = tmp_path / "image.png"
        path.write_bytes(b"image-bytes")
        return path

    resolver = FeishuMessageContentResolver(
        config=RuntimeConfig(data_root=tmp_path / "data"),
        image_summarizer=FakeImageSummarizer(),
        image_downloader=fake_download,
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_image",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="image",
        text="[Image: img_1]",
        reply_to_message_id=None,
        quote_message_id=None,
        attachments=[
            AttachmentMeta(
                attachment_id="img_1",
                file_name="image.png",
                mime_type="image/png",
                file_size=11,
            )
        ],
    )

    summaries = resolver.summarize_images([message])

    assert len(summaries) == 1
    assert summaries[0].message_id == "om_image"
    assert summaries[0].text == "图片内容摘要：图片中标注了两家代理商。"


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


def test_message_resolver_loads_feishu_doc_link_text(tmp_path: Path) -> None:
    def fake_runner(args):
        return CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "document": {
                            "content": "<title>竞品逾期调研</title><p>第一段</p><p>第二段</p>"
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
        text="看这里 https://foo.feishu.cn/docx/abc",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[LinkMeta(url="https://foo.feishu.cn/docx/abc", title="方案", link_type="feishu_doc")],
        attachments=[],
        is_system=False,
    )

    loaded = resolver.load_link_text_if_needed(message, ["om_1#link1"], "需要正文")

    assert loaded is not None
    assert loaded[0].link_id == "om_1#link1"
    assert "竞品逾期调研" in loaded[0].text
    assert "第一段第二段" in loaded[0].text


def test_message_resolver_does_not_load_plain_url_as_link_text(tmp_path: Path) -> None:
    resolver = FeishuMessageContentResolver(config=RuntimeConfig(data_root=tmp_path / "data"))
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="https://example.com/report",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )

    loaded = resolver.load_link_text_if_needed(message, ["om_1#link1"], "需要正文")

    assert loaded is None
