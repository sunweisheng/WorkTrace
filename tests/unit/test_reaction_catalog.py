from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.worktrace.config import RuntimeConfig
from src.worktrace.analyzers.prompts import build_conversation_segmentation_prompt
from src.worktrace.models import MessageReaction, NormalizedMessage
from src.worktrace.pipeline.conversation_segments import build_response_signals
from src.worktrace.reaction_catalog import (
    ReactionCatalogError,
    ReactionCatalogStore,
    enrich_message_reactions,
)


def _write_catalog(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "feishu.json").write_text(
        json.dumps(
            {
                "source_id": "feishu",
                "entry_count": 1,
                "fallback": {
                    "name": "未收录",
                    "description_template": "类型 {emoji_type}",
                    "semantic": "other",
                },
                "entries": [
                    {
                        "emoji_type": "THUMBSUP",
                        "name": "点赞",
                        "description": "表示认可。",
                        "semantic": "affirmative",
                        "image_path": "config/assets/reactions/feishu/THUMBSUP.png",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_catalog_loads_source_specific_metadata_and_fallback(tmp_path: Path) -> None:
    root = tmp_path / "config" / "reaction_catalogs"
    _write_catalog(root)

    catalog = ReactionCatalogStore(root).load("feishu")

    assert catalog.lookup("THUMBSUP").name == "点赞"
    assert catalog.lookup("CUSTOM").description == "类型 CUSTOM"
    assert catalog.lookup("CUSTOM").semantic == "other"


def test_catalog_rejects_duplicate_emoji_type(tmp_path: Path) -> None:
    root = tmp_path / "config" / "reaction_catalogs"
    _write_catalog(root)
    path = root / "feishu.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entries"].append(payload["entries"][0])
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReactionCatalogError, match="duplicate"):
        ReactionCatalogStore(root).load("feishu")


def test_response_signals_include_catalog_metadata_and_configured_fallback(tmp_path: Path) -> None:
    root = tmp_path / "config" / "reaction_catalogs"
    _write_catalog(root)
    catalog = ReactionCatalogStore(root).load("feishu")
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_other",
        sender_name="Other",
        send_time="2026-07-12T10:00:00+08:00",
        message_type="text",
        text="请确认",
        reply_to_message_id=None,
        quote_message_id=None,
        reactions=[
            MessageReaction("r1", "ou_self", "THUMBSUP", "2026-07-12T10:01:00+08:00"),
            MessageReaction("r2", "ou_other", "CUSTOM", "2026-07-12T10:02:00+08:00"),
        ],
    )
    messages = enrich_message_reactions([message], catalog)

    signals = build_response_signals(messages, self_open_id="ou_self", reaction_catalog=catalog)

    assert [(item.emoji_name, item.semantic) for item in signals] == [("点赞", "affirmative")]

    prompt = build_conversation_segmentation_prompt(
        target_date="2026-07-12",
        conversation_id="oc_1",
        conversation_name="项目群",
        messages=messages,
        self_open_id="ou_self",
        self_display_name="Self",
        response_signals=signals,
        hard_boundary_before_ids=set(),
    )
    payload = json.loads(prompt)
    assert payload["input"]["response_signals"][0]["emoji_name"] == "点赞"
    assert payload["input"]["response_signals"][0]["emoji_description"] == "表示认可。"
    assert payload["input"]["messages"][0]["reactions"] == [
        {
            "emoji_type": "THUMBSUP",
            "name": "点赞",
            "description": "表示认可。",
            "semantic": "affirmative",
        },
        {
            "emoji_type": "CUSTOM",
            "name": "未收录",
            "description": "类型 CUSTOM",
            "semantic": "other",
        },
    ]


def test_repository_feishu_catalog_and_images_are_complete() -> None:
    catalog = ReactionCatalogStore(Path("config/reaction_catalogs")).load("feishu")
    asset_paths = list(Path("config/assets/reactions/feishu").glob("*.png"))

    assert len(catalog.entries) > 0
    assert len({item.emoji_type for item in catalog.entries}) == len(catalog.entries)
    assert len(asset_paths) == len(catalog.entries)
    assert all(Path(item.image_path).is_file() for item in catalog.entries)
