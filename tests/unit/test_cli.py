from __future__ import annotations

from pathlib import Path

from src.worktrace.cli import execute
from src.worktrace.config import RuntimeConfig
from src.worktrace.reaction_catalogs.base import ReactionCatalogSyncResult


def test_sync_reaction_catalog_dispatches_without_date() -> None:
    calls: list[tuple[str, Path]] = []

    def sync(*, source_id: str, config: RuntimeConfig, cwd: Path) -> ReactionCatalogSyncResult:
        calls.append((source_id, cwd))
        return ReactionCatalogSyncResult(
            source_id=source_id,
            entry_count=2,
            catalog_path=Path("config/reaction_catalogs/feishu.json"),
            asset_dir=Path("config/assets/reactions/feishu"),
        )

    result, exit_code = execute(
        ["sync-reaction-catalog", "--source", "feishu"],
        sync_reaction_catalog_func=sync,
    )

    assert exit_code == 0
    assert result.entry_count == 2
    assert calls[0][0] == "feishu"
