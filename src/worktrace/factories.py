from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import RuntimeConfig
from .analyzers.base import Analyzer
from .resolvers.base import ContentResolver
from .sources.base import ChatSource
from .delivery.base import DeliveryChannel
from .stores.base import EventStore


@dataclass(frozen=True)
class RuntimeDependencies:
    chat_source: ChatSource
    content_resolver: ContentResolver
    analyzer: Analyzer
    delivery_channel: DeliveryChannel
    event_store: EventStore


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
    def create_default(config: RuntimeConfig) -> ContentResolver:
        from .attachments import TextAttachmentExtractor
        from .resolvers.feishu_message import FeishuMessageContentResolver
        from .vision import OnlineImageSummarizer

        return FeishuMessageContentResolver(
            config=config,
            image_summarizer=OnlineImageSummarizer(config=config),
            text_attachment_extractor=TextAttachmentExtractor(config=config),
        )


class AnalyzerFactory:
    @staticmethod
    def create_default(config: RuntimeConfig) -> Analyzer:
        if config.analyzer_backend == "online":
            from .analyzers.online import OnlineLLMAnalyzer

            return OnlineLLMAnalyzer(config=config)

        from .analyzers.codex import CodexAnalyzer

        return CodexAnalyzer(config=config)


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
    return RuntimeDependencies(
        chat_source=ChatSourceFactory.create_default(config),
        content_resolver=ContentResolverFactory.create_default(config),
        analyzer=AnalyzerFactory.create_default(config),
        delivery_channel=DeliveryFactory.create_default(config),
        event_store=StorageFactory.create_default(config),
    )
