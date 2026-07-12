from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from .config import RuntimeConfig
from .models import MessageReaction, NormalizedMessage


class ReactionCatalogError(ValueError):
    """Raised when a reaction catalog cannot be loaded or validated."""


@dataclass(frozen=True)
class ReactionMetadata:
    emoji_type: str
    name: str
    description: str
    semantic: str
    image_path: str = ""


@dataclass(frozen=True)
class ReactionFallback:
    name: str
    description_template: str
    semantic: str

    def metadata_for(self, emoji_type: str) -> ReactionMetadata:
        return ReactionMetadata(
            emoji_type=emoji_type,
            name=self.name,
            description=self.description_template.format(emoji_type=emoji_type),
            semantic=self.semantic,
        )


@dataclass(frozen=True)
class ReactionCatalog:
    source_id: str
    entries: tuple[ReactionMetadata, ...]
    fallback: ReactionFallback

    def lookup(self, emoji_type: str) -> ReactionMetadata:
        normalized = emoji_type.strip()
        for entry in self.entries:
            if entry.emoji_type == normalized:
                return entry
        return self.fallback.metadata_for(normalized)

    @classmethod
    def empty(cls, source_id: str) -> ReactionCatalog:
        return cls(
            source_id=source_id,
            entries=(),
            fallback=ReactionFallback(
                name="",
                description_template="{emoji_type}",
                semantic="unknown",
            ),
        )


class ReactionCatalogStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def from_config(cls, config: RuntimeConfig, *, cwd: Path | None = None) -> ReactionCatalogStore:
        return cls((cwd or Path.cwd()) / config.reaction_catalogs_root)

    def load(self, source_id: str) -> ReactionCatalog:
        path = self.root / f"{source_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ReactionCatalog.empty(source_id)
        except json.JSONDecodeError as exc:
            raise ReactionCatalogError(f"Invalid reaction catalog: {path}") from exc
        return parse_reaction_catalog(payload, source_id=source_id, path=path)

    def load_defaults(self) -> ReactionFallback:
        path = self.root / "defaults.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ReactionCatalogError(f"Missing reaction catalog defaults: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ReactionCatalogError(f"Invalid reaction catalog defaults: {path}") from exc
        return _parse_fallback(payload.get("fallback"), path=path)


def enrich_message_reactions(
    messages: list[NormalizedMessage],
    catalog: ReactionCatalog,
) -> list[NormalizedMessage]:
    """Attach catalog metadata to every reaction without retaining operator identifiers in prompts."""
    enriched_messages: list[NormalizedMessage] = []
    for message in messages:
        reactions = [
            _enrich_reaction(reaction, catalog.lookup(reaction.emoji_type))
            for reaction in message.reactions
        ]
        enriched_messages.append(replace(message, reactions=reactions))
    return enriched_messages


def _enrich_reaction(
    reaction: MessageReaction,
    metadata: ReactionMetadata,
) -> MessageReaction:
    return replace(
        reaction,
        emoji_name=metadata.name,
        emoji_description=metadata.description,
        semantic=metadata.semantic,
    )


def parse_reaction_catalog(
    payload: object,
    *,
    source_id: str,
    path: Path,
) -> ReactionCatalog:
    if not isinstance(payload, dict):
        raise ReactionCatalogError(f"Invalid reaction catalog: {path} must contain an object.")
    configured_source = _required_string(payload.get("source_id"), "source_id", path)
    if configured_source != source_id:
        raise ReactionCatalogError(
            f"Invalid reaction catalog: {path} source_id does not match {source_id}."
        )
    fallback = _parse_fallback(payload.get("fallback"), path=path)
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ReactionCatalogError(f"Invalid reaction catalog: {path} entries must be a list.")
    entry_count = payload.get("entry_count")
    if not isinstance(entry_count, int) or entry_count < 0:
        raise ReactionCatalogError(f"Invalid reaction catalog: {path} entry_count must be non-negative.")
    if entry_count != len(raw_entries):
        raise ReactionCatalogError(f"Invalid reaction catalog: {path} entry_count does not match entries.")

    entries: list[ReactionMetadata] = []
    seen: set[str] = set()
    for item in raw_entries:
        if not isinstance(item, dict):
            raise ReactionCatalogError(f"Invalid reaction catalog: {path} entries must be objects.")
        emoji_type = _required_string(item.get("emoji_type"), "emoji_type", path)
        if emoji_type in seen:
            raise ReactionCatalogError(
                f"Invalid reaction catalog: {path} contains duplicate {emoji_type}."
            )
        seen.add(emoji_type)
        entries.append(
            ReactionMetadata(
                emoji_type=emoji_type,
                name=_required_string(item.get("name"), "name", path),
                description=_required_string(item.get("description"), "description", path),
                semantic=_required_string(item.get("semantic"), "semantic", path),
                image_path=_optional_string(item.get("image_path"), "image_path", path),
            )
        )
    return ReactionCatalog(source_id=source_id, entries=tuple(entries), fallback=fallback)


def reaction_catalog_payload(catalog: ReactionCatalog) -> dict[str, object]:
    return {
        "source_id": catalog.source_id,
        "entry_count": len(catalog.entries),
        "fallback": {
            "name": catalog.fallback.name,
            "description_template": catalog.fallback.description_template,
            "semantic": catalog.fallback.semantic,
        },
        "entries": [
            {
                "emoji_type": entry.emoji_type,
                "name": entry.name,
                "description": entry.description,
                "semantic": entry.semantic,
                "image_path": entry.image_path,
            }
            for entry in catalog.entries
        ],
    }


def _parse_fallback(value: object, *, path: Path) -> ReactionFallback:
    if not isinstance(value, dict):
        raise ReactionCatalogError(f"Invalid reaction catalog: {path} fallback must be an object.")
    template = _required_string(value.get("description_template"), "description_template", path)
    if "{emoji_type}" not in template:
        raise ReactionCatalogError(
            f"Invalid reaction catalog: {path} fallback description_template must include {{emoji_type}}."
        )
    return ReactionFallback(
        name=_required_string(value.get("name"), "name", path),
        description_template=template,
        semantic=_required_string(value.get("semantic"), "semantic", path),
    )


def _required_string(value: object, field_name: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReactionCatalogError(f"Invalid reaction catalog: {path} field {field_name} must be non-empty.")
    return value.strip()


def _optional_string(value: object, field_name: str, path: Path) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReactionCatalogError(f"Invalid reaction catalog: {path} field {field_name} must be a string.")
    return value.strip()
