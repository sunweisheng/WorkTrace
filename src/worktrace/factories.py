from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import RuntimeConfig, load_online_llm_settings
from .analyzers.base import Analyzer
from .resolvers.base import ContentResolver
from .sources.base import ChatSource
from .delivery.base import DeliveryChannel
from .stores.base import EventStore
from .llm_usage import LLMUsageRecorder


@dataclass(frozen=True)
class RuntimeDependencies:
    chat_source: ChatSource
    content_resolver: ContentResolver
    analyzer: Analyzer
    delivery_channel: DeliveryChannel
    event_store: EventStore
    llm_usage_recorder: LLMUsageRecorder = field(default_factory=LLMUsageRecorder)


class ChatSourceFactory:
    @staticmethod
    def create_default(config: RuntimeConfig) -> ChatSource:
        from .sources.feishu_cli import FeishuCliChatSource

        return FeishuCliChatSource(config=config)


class ReactionCatalogProviderFactory:
    @staticmethod
    def create(source_id: str, config: RuntimeConfig, *, cwd=None):
        if source_id == "feishu":
            from .reaction_catalogs.feishu import FeishuReactionCatalogProvider

            return FeishuReactionCatalogProvider(config=config, cwd=cwd or Path.cwd())
        raise ValueError(f"Unsupported reaction catalog source: {source_id}.")


class ContentResolverFactory:
    @staticmethod
    def create_default(
        config: RuntimeConfig,
        *,
        usage_recorder: LLMUsageRecorder | None = None,
    ) -> ContentResolver:
        from .attachments import TextAttachmentExtractor
        from .resolvers.feishu_message import FeishuMessageContentResolver
        from .analyzers.codex import CodexAnalyzer
        from .vision import CodexFirstImageSummarizer, ImageSummarySettings, OnlineImageSummarizer

        recorder = usage_recorder or LLMUsageRecorder()
        settings = ImageSummarySettings.load(config)
        online_fallback = None
        try:
            load_online_llm_settings(config)
        except ValueError:
            pass
        else:
            online_fallback = OnlineImageSummarizer(
                config=config,
                settings=settings,
                usage_recorder=recorder,
            )

        return FeishuMessageContentResolver(
            config=config,
            image_summarizer=CodexFirstImageSummarizer(
                config=config,
                settings=settings,
                codex=CodexAnalyzer(
                    config=config,
                    usage_recorder=recorder,
                ),
                online_fallback=online_fallback,
                usage_recorder=recorder,
            ),
            text_attachment_extractor=TextAttachmentExtractor(config=config),
        )


class AnalyzerFactory:
    @staticmethod
    def create_default(
        config: RuntimeConfig,
        *,
        usage_recorder: LLMUsageRecorder | None = None,
        cwd: Path | None = None,
    ) -> Analyzer:
        recorder = usage_recorder or LLMUsageRecorder()
        base_dir = cwd or Path.cwd()
        from .analyzers.codex import CodexAnalyzer
        from .analyzers.failover import FailoverAnalyzer

        fallback = None
        try:
            load_online_llm_settings(config, cwd=base_dir)
        except ValueError:
            pass
        else:
            from .analyzers.online import OnlineLLMAnalyzer

            fallback = OnlineLLMAnalyzer(
                config=config,
                cwd=base_dir,
                usage_recorder=recorder,
            )
        return FailoverAnalyzer(
            primary=CodexAnalyzer(
                config=config,
                cwd=base_dir,
                usage_recorder=recorder,
            ),
            fallback=fallback,
            usage_recorder=recorder,
            primary_backend="codex",
            fallback_backend="online",
            primary_request_retry_limit=config.primary_request_retry_limit,
        )


class StorageFactory:
    @staticmethod
    def create_default(config: RuntimeConfig) -> EventStore:
        from .stores.markdown import MarkdownEventStore

        return MarkdownEventStore(config=config)


class DeliveryFactory:
    @staticmethod
    def create_default(config: RuntimeConfig) -> DeliveryChannel:
        from .delivery.feishu_cli import FeishuCliSelfDelivery

        return FeishuCliSelfDelivery()


def build_runtime_dependencies(config: RuntimeConfig) -> RuntimeDependencies:
    usage_recorder = LLMUsageRecorder()
    return RuntimeDependencies(
        chat_source=ChatSourceFactory.create_default(config),
        content_resolver=ContentResolverFactory.create_default(
            config,
            usage_recorder=usage_recorder,
        ),
        analyzer=AnalyzerFactory.create_default(config, usage_recorder=usage_recorder),
        delivery_channel=DeliveryFactory.create_default(config),
        event_store=StorageFactory.create_default(config),
        llm_usage_recorder=usage_recorder,
    )
