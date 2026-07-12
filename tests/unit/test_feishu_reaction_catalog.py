from __future__ import annotations

from pathlib import Path

import pytest

from src.worktrace.config import RuntimeConfig
from src.worktrace.reaction_catalog import ReactionCatalogError, ReactionMetadata
from src.worktrace.reaction_catalogs.feishu import (
    FEISHU_REACTION_EMOJIS_DOCUMENT_URL,
    FeishuReactionCatalogProvider,
    parse_feishu_reaction_markdown,
)


class FakeEnricher:
    def enrich(self, emoji_types: list[str]) -> list[ReactionMetadata]:
        return [
            ReactionMetadata(item, f"名称{item}", f"解释{item}", "acknowledgement")
            for item in emoji_types
        ]


def _prepare_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config" / "reaction_catalogs"
    path.mkdir(parents=True)
    (path / "defaults.json").write_text(
        '{"fallback":{"name":"未收录","description_template":"类型 {emoji_type}","semantic":"other"}}',
        encoding="utf-8",
    )


def test_parse_feishu_reaction_markdown_extracts_type_and_image_url() -> None:
    entries = parse_feishu_reaction_markdown(
        "![点赞](https://example.test/thumb.png) | THUMBSUP | &nbsp;"
    )

    assert [(item.emoji_type, item.image_url) for item in entries] == [
        ("THUMBSUP", "https://example.test/thumb.png")
    ]


def test_sync_writes_catalog_and_images_only_after_complete_validation(tmp_path: Path) -> None:
    _prepare_defaults(tmp_path)
    markdown = (
        "![点赞](https://example.test/thumb.png) | THUMBSUP | "
        "![好的](https://example.test/ok.png) | OK |"
    )

    def fake_get(url: str) -> bytes:
        return markdown.encode("utf-8") if url == FEISHU_REACTION_EMOJIS_DOCUMENT_URL else b"png"

    result = FeishuReactionCatalogProvider(
        config=RuntimeConfig(),
        cwd=tmp_path,
        http_get=fake_get,
        metadata_enricher=FakeEnricher(),
    ).synchronize()

    assert result.entry_count == 2
    assert (tmp_path / "config" / "reaction_catalogs" / "feishu.json").is_file()
    assert (tmp_path / "config" / "assets" / "reactions" / "feishu" / "THUMBSUP.png").read_bytes() == b"png"


def test_sync_does_not_write_when_metadata_does_not_match_types(tmp_path: Path) -> None:
    _prepare_defaults(tmp_path)

    class InvalidEnricher:
        def enrich(self, emoji_types: list[str]) -> list[ReactionMetadata]:
            return [ReactionMetadata("OTHER", "名称", "解释", "other")]

    provider = FeishuReactionCatalogProvider(
        config=RuntimeConfig(),
        cwd=tmp_path,
        http_get=lambda _: "![点赞](https://example.test/thumb.png) | THUMBSUP |".encode("utf-8"),
        metadata_enricher=InvalidEnricher(),
    )

    with pytest.raises(ReactionCatalogError, match="does not match"):
        provider.synchronize()
    assert not (tmp_path / "config" / "reaction_catalogs" / "feishu.json").exists()
