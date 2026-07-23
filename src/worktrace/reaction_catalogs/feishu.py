from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urljoin

import httpx

from ..analyzers.online import OnlineLLMAnalyzer
from ..analyzers.function_calls import function_call_spec
from ..config import RuntimeConfig
from ..reaction_catalog import (
    ReactionCatalog,
    ReactionCatalogError,
    ReactionCatalogStore,
    ReactionMetadata,
    reaction_catalog_payload,
)
from ..utils.json_io import dump_json
from .base import ReactionCatalogProvider, ReactionCatalogSyncResult


FEISHU_REACTION_EMOJIS_DOCUMENT_URL = (
    "https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/"
    "im-v1/message-reaction/emojis-introduce.md"
)
_REACTION_ROW_RE = re.compile(
    r"!\[[^\]]*\]\((?P<image_url>[^)]+)\)\s*\|\s*"
    r"(?P<emoji_type>[A-Za-z0-9_]+)\b"
)
_SAFE_EMOJI_TYPE_RE = re.compile(r"^[A-Za-z0-9_]+$")
_METADATA_BATCH_SIZE = 10


@dataclass(frozen=True)
class RemoteReactionEmoji:
    emoji_type: str
    image_url: str


class ReactionMetadataEnricher(Protocol):
    def enrich(self, emoji_types: list[str]) -> list[ReactionMetadata]: ...


@dataclass
class OnlineReactionMetadataEnricher:
    config: RuntimeConfig
    cwd: Path

    def enrich(self, emoji_types: list[str]) -> list[ReactionMetadata]:
        if not emoji_types:
            return []
        analyzer = OnlineLLMAnalyzer(
            config=self.config,
            cwd=self.cwd,
        )
        payload = analyzer.request_function(
            _build_metadata_prompt(emoji_types),
            function_spec=function_call_spec(
                "reaction_metadata",
                _metadata_output_schema(emoji_types),
            ),
        )
        return _parse_enriched_metadata(payload, expected_types=emoji_types)


@dataclass
class FeishuReactionCatalogProvider(ReactionCatalogProvider):
    config: RuntimeConfig
    cwd: Path
    http_get: Callable[[str], bytes] | None = None
    metadata_enricher: ReactionMetadataEnricher | None = None

    source_id = "feishu"

    def __post_init__(self) -> None:
        if self.http_get is None:
            self.http_get = _download_bytes
        if self.metadata_enricher is None:
            self.metadata_enricher = OnlineReactionMetadataEnricher(self.config, self.cwd)

    @property
    def catalog_path(self) -> Path:
        return self.cwd / self.config.reaction_catalogs_root / f"{self.source_id}.json"

    @property
    def asset_dir(self) -> Path:
        return self.cwd / "config" / "assets" / "reactions" / self.source_id

    def build_catalog(self) -> ReactionCatalog:
        return self._build_catalog(self._fetch_remote_entries())

    def synchronize(self) -> ReactionCatalogSyncResult:
        remote_entries = self._fetch_remote_entries()
        catalog = self._build_catalog(remote_entries)
        self._stage_and_commit(catalog, remote_entries)
        return ReactionCatalogSyncResult(
            source_id=self.source_id,
            entry_count=len(catalog.entries),
            catalog_path=self.catalog_path,
            asset_dir=self.asset_dir,
        )

    def _build_catalog(self, remote_entries: list[RemoteReactionEmoji]) -> ReactionCatalog:
        metadata_by_type: dict[str, ReactionMetadata] = {}
        assert self.metadata_enricher is not None
        for start in range(0, len(remote_entries), _METADATA_BATCH_SIZE):
            chunk = remote_entries[start : start + _METADATA_BATCH_SIZE]
            enriched = self.metadata_enricher.enrich([item.emoji_type for item in chunk])
            for item in enriched:
                if item.emoji_type in metadata_by_type:
                    raise ReactionCatalogError(
                        f"Feishu reaction metadata contains duplicate {item.emoji_type}."
                    )
                metadata_by_type[item.emoji_type] = item
        expected_types = {item.emoji_type for item in remote_entries}
        if set(metadata_by_type) != expected_types:
            raise ReactionCatalogError("Feishu reaction metadata does not match the official type list.")
        fallback = ReactionCatalogStore.from_config(self.config, cwd=self.cwd).load_defaults()
        return ReactionCatalog(
            source_id=self.source_id,
            entries=tuple(
                ReactionMetadata(
                    emoji_type=item.emoji_type,
                    name=metadata_by_type[item.emoji_type].name,
                    description=metadata_by_type[item.emoji_type].description,
                    semantic=metadata_by_type[item.emoji_type].semantic,
                    image_path=str(
                        Path("config")
                        / "assets"
                        / "reactions"
                        / self.source_id
                        / f"{item.emoji_type}.png"
                    ),
                )
                for item in remote_entries
            ),
            fallback=fallback,
        )

    def _fetch_remote_entries(self) -> list[RemoteReactionEmoji]:
        assert self.http_get is not None
        return parse_feishu_reaction_markdown(
            self.http_get(FEISHU_REACTION_EMOJIS_DOCUMENT_URL).decode("utf-8")
        )

    def _stage_and_commit(
        self,
        catalog: ReactionCatalog,
        remote_entries: list[RemoteReactionEmoji],
    ) -> None:
        assert self.http_get is not None
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.asset_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.asset_dir.parent, prefix=f".{self.source_id}-") as temp:
            temp_root = Path(temp)
            staged_assets = temp_root / self.source_id
            staged_assets.mkdir()
            for item in remote_entries:
                image_path = staged_assets / f"{item.emoji_type}.png"
                image_path.write_bytes(self.http_get(item.image_url))
            _validate_staged_assets(catalog, staged_assets)
            staged_catalog = temp_root / f"{self.source_id}.json"
            staged_catalog.write_text(dump_json(reaction_catalog_payload(catalog), pretty=True) + "\n", encoding="utf-8")
            _commit_staged_catalog(
                staged_catalog=staged_catalog,
                catalog_path=self.catalog_path,
                staged_assets=staged_assets,
                asset_dir=self.asset_dir,
            )


def parse_feishu_reaction_markdown(markdown: str) -> list[RemoteReactionEmoji]:
    entries: list[RemoteReactionEmoji] = []
    seen: set[str] = set()
    for match in _REACTION_ROW_RE.finditer(markdown):
        emoji_type = match.group("emoji_type")
        if emoji_type in seen:
            continue
        if not _SAFE_EMOJI_TYPE_RE.fullmatch(emoji_type):
            raise ReactionCatalogError(f"Invalid Feishu reaction emoji type: {emoji_type}")
        image_url = urljoin(FEISHU_REACTION_EMOJIS_DOCUMENT_URL, match.group("image_url"))
        entries.append(RemoteReactionEmoji(emoji_type=emoji_type, image_url=image_url))
        seen.add(emoji_type)
    if not entries:
        raise ReactionCatalogError("Could not find Feishu reaction emojis in the official document.")
    return entries


def _download_bytes(url: str) -> bytes:
    response = httpx.get(url, follow_redirects=True, timeout=30)
    response.raise_for_status()
    if not response.content:
        raise ReactionCatalogError(f"Downloaded empty reaction image: {url}")
    return response.content


def _build_metadata_prompt(emoji_types: list[str]) -> str:
    return (
        "为飞书消息表情生成中文元数据。只调用指定 Function 一次。"
        "每个 emoji_type 恰好输出一次；name 为简短中文名称，description 为不超过 30 字的中文含义，"
        "semantic 为英文小写短语，描述工作沟通中的主要反应语义。\n"
        f"emoji_types: {emoji_types}\n/no_think"
    )


def _metadata_output_schema(emoji_types: list[str] | None = None) -> dict[str, object]:
    allowed_types = list(dict.fromkeys(emoji_types or []))
    emoji_type_schema: dict[str, object] = {"type": "string"}
    if allowed_types:
        emoji_type_schema["enum"] = allowed_types
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                **(
                    {"minItems": len(allowed_types), "maxItems": len(allowed_types)}
                    if allowed_types
                    else {}
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["emoji_type", "name", "description", "semantic"],
                    "properties": {
                        "emoji_type": emoji_type_schema,
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "semantic": {"type": "string"},
                    },
                },
            }
        },
    }


def _parse_enriched_metadata(payload: object, *, expected_types: list[str]) -> list[ReactionMetadata]:
    raw_items = payload.get("items") if isinstance(payload, dict) and "items" in payload else payload
    if isinstance(raw_items, dict):
        raw_items = [
            {"emoji_type": emoji_type, **metadata}
            for emoji_type, metadata in raw_items.items()
            if isinstance(metadata, dict)
        ]
    if not isinstance(raw_items, list):
        raise ReactionCatalogError("Reaction metadata LLM response must contain an items list.")
    parsed: list[ReactionMetadata] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise ReactionCatalogError("Reaction metadata LLM items must be objects.")
        fields = {key: item.get(key) for key in ("emoji_type", "name", "description", "semantic")}
        if any(not isinstance(value, str) or not value.strip() for value in fields.values()):
            raise ReactionCatalogError("Reaction metadata LLM items must contain non-empty strings.")
        parsed.append(ReactionMetadata(**fields))
    received_types = [item.emoji_type for item in parsed]
    if len(received_types) != len(set(received_types)) or set(received_types) != set(expected_types):
        raise ReactionCatalogError("Reaction metadata LLM response does not match requested emoji types.")
    return parsed


def _validate_staged_assets(catalog: ReactionCatalog, asset_dir: Path) -> None:
    expected = {f"{entry.emoji_type}.png" for entry in catalog.entries}
    actual = {path.name for path in asset_dir.iterdir() if path.is_file() and path.stat().st_size > 0}
    if actual != expected:
        raise ReactionCatalogError("Staged reaction image files do not match the catalog entries.")


def _commit_staged_catalog(
    *,
    staged_catalog: Path,
    catalog_path: Path,
    staged_assets: Path,
    asset_dir: Path,
) -> None:
    catalog_backup = catalog_path.with_name(f".{catalog_path.name}.backup")
    assets_backup = asset_dir.with_name(f".{asset_dir.name}.backup")
    for backup in (catalog_backup, assets_backup):
        if backup.exists():
            if backup.is_dir():
                shutil.rmtree(backup)
            else:
                backup.unlink()
    had_catalog = catalog_path.exists()
    had_assets = asset_dir.exists()
    try:
        if had_catalog:
            os.replace(catalog_path, catalog_backup)
        if had_assets:
            os.replace(asset_dir, assets_backup)
        os.replace(staged_catalog, catalog_path)
        os.replace(staged_assets, asset_dir)
    except OSError:
        if catalog_path.exists():
            catalog_path.unlink()
        if asset_dir.exists():
            shutil.rmtree(asset_dir)
        if had_catalog and catalog_backup.exists():
            os.replace(catalog_backup, catalog_path)
        if had_assets and assets_backup.exists():
            os.replace(assets_backup, asset_dir)
        raise
    else:
        if catalog_backup.exists():
            catalog_backup.unlink()
        if assets_backup.exists():
            shutil.rmtree(assets_backup)
