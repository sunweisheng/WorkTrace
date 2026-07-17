from __future__ import annotations

from pathlib import Path

from src.worktrace.analyzers.base import Analyzer
from src.worktrace.config import RuntimeConfig
from src.worktrace.delivery.base import DeliveryChannel
from src.worktrace.factories import build_runtime_dependencies
from src.worktrace.resolvers.base import ContentResolver
from src.worktrace.sources.base import ChatSource
from src.worktrace.stores.base import EventStore


def test_build_runtime_dependencies_returns_interface_instances(tmp_path: Path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    runtime = build_runtime_dependencies(config)

    assert isinstance(runtime.chat_source, ChatSource)
    assert isinstance(runtime.content_resolver, ContentResolver)
    assert isinstance(runtime.analyzer, Analyzer)
    assert isinstance(runtime.delivery_channel, DeliveryChannel)
    assert isinstance(runtime.event_store, EventStore)


def test_runtime_config_defaults_to_online_backend() -> None:
    config = RuntimeConfig()

    assert config.analyzer_backend == "online"
    assert config.llm_tls_verify is False
    assert config.llm_sleep_min_seconds == 0.0
    assert config.llm_sleep_max_seconds == 0.0


def test_build_runtime_dependencies_supports_online_analyzer(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        analyzer_backend="online",
    )
    runtime = build_runtime_dependencies(config)

    assert isinstance(runtime.analyzer, Analyzer)
