from __future__ import annotations

from pathlib import Path

from src.worktrace.config import RuntimeConfig
from src.worktrace.constants import DailyRunStatus
from src.worktrace.factories import RuntimeDependencies
from src.worktrace.models import (
    BatchAnalysisResult,
    ConversationRef,
    LinkMeta,
    NormalizedMessage,
    SelfIdentity,
    SourceBackedEventDraft,
)
from src.worktrace.runner import DailyTraceRunner
from src.worktrace.stores.markdown import MarkdownEventStore


class LinkSource:
    def get_self_identity(self):
        return SelfIdentity(open_id="ou_self", display_name="Me", source="fake")

    def list_target_conversations(self, target_date, self_identity):
        return [ConversationRef(conversation_id="oc_1", conversation_name="项目群")]

    def fetch_conversation_messages(self, target_date, conversation_ids):
        return [
            NormalizedMessage(
                conversation_id="oc_1",
                conversation_name="项目群",
                message_id="om_1",
                sender_open_id="ou_self",
                sender_name="Me",
                send_time="2026-06-22T10:00:00+08:00",
                message_type="text",
                text="请看文档 https://foo.feishu.cn/docx/abc",
                reply_to_message_id=None,
                quote_message_id=None,
                links=[
                    LinkMeta(
                        url="https://foo.feishu.cn/docx/abc",
                        title="发布方案",
                        link_type="feishu_doc",
                    )
                ],
                attachments=[],
                is_system=False,
            )
        ]

    def fetch_related_messages(self, conversation_id, target_message_ids, direction, limit):
        return []


class LinkResolver:
    def to_text(self, message):
        return message.text

    def extract_links(self, message):
        return list(message.links)

    def load_attachment_text_if_needed(self, message, attachment_ids, hint):
        return None


class LinkAnalyzer:
    def build_batch_prompt(self, batch_input):
        return "prompt"

    def analyze_batch(self, target_date, batch_input):
        return BatchAnalysisResult(
            candidate_events=[
                SourceBackedEventDraft(
                    draft_id="draft-1",
                    date="2026-06-22",
                    topic="发布推进",
                    content="完成发布沟通",
                    source_message_ids=["om_1"],
                    source_conversation_id="oc_1",
                    source_slice_id=batch_input.slices[0].slice_id,
                    confidence=0.9,
                )
            ],
            context_requests=[],
        )

    def merge_day_candidates(self, target_date, candidates):
        raise AssertionError("Should not group when there is only one candidate")


class LinkDelivery:
    def deliver_to_self(self, *, self_identity, markdown_path):
        return ("success", self_identity.open_id)


def test_runner_attaches_file_links_from_source_messages(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    runner = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=LinkSource(),
            content_resolver=LinkResolver(),
            analyzer=LinkAnalyzer(),
            delivery_channel=LinkDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    )

    result = runner.run("2026-06-22")

    assert result.status == DailyRunStatus.SUCCESS.value
    assert result.output_path is not None

    content = Path(result.output_path).read_text(encoding="utf-8")
    assert "[发布方案](https://foo.feishu.cn/docx/abc)" in content
