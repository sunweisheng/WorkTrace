from __future__ import annotations

from dataclasses import dataclass

from .config import RuntimeConfig
from .analyzers.base import Analyzer
from .resolvers.base import ContentResolver
from .sources.base import ChatSource
from .stores.base import EventStore


@dataclass(frozen=True)
class RuntimeDependencies:
    chat_source: ChatSource
    content_resolver: ContentResolver
    analyzer: Analyzer
    event_store: EventStore


class ChatSourceFactory:
    @staticmethod
    def create_default(config: RuntimeConfig) -> ChatSource:
        from .sources.feishu_cli import FeishuCliChatSource

        return FeishuCliChatSource(config=config)


class ContentResolverFactory:
    @staticmethod
    def create_default(config: RuntimeConfig) -> ContentResolver:
        from .resolvers.feishu_message import FeishuMessageContentResolver

        return FeishuMessageContentResolver(config=config)


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


def build_runtime_dependencies(config: RuntimeConfig) -> RuntimeDependencies:
    return RuntimeDependencies(
        chat_source=ChatSourceFactory.create_default(config),
        content_resolver=ContentResolverFactory.create_default(config),
        analyzer=AnalyzerFactory.create_default(config),
        event_store=StorageFactory.create_default(config),
    )
