from __future__ import annotations

from pathlib import Path

from src.worktrace.analyzers.base import Analyzer
from src.worktrace.config import RuntimeConfig
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
    assert isinstance(runtime.event_store, EventStore)


def test_runtime_config_defaults_to_hook_backend() -> None:
    config = RuntimeConfig()

    assert config.analyzer_backend == "hook"
    assert (
        config.hook_command
        == "python3 -m src.worktrace.hook_runner --mode chat-completions-http"
    )


def test_build_runtime_dependencies_supports_hook_analyzer(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        analyzer_backend="hook",
        hook_command="mock-hook",
    )
    runtime = build_runtime_dependencies(config)

    assert isinstance(runtime.analyzer, Analyzer)
