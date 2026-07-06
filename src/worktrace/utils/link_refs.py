from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlparse

from ..constants import LinkType
from ..models import LinkMeta, NormalizedMessage
from .text import extract_urls

_LINK_ID_RE = re.compile(r"^(?P<message_id>.+)#link(?P<index>\d+)$")
_URL_TRAILING_HTML_RE = re.compile(r"(?:</?[A-Za-z][^>]*)+$")


@dataclass(frozen=True)
class MessageLinkCandidate:
    link_id: str
    message_id: str
    url: str
    title: str
    link_type: str


def build_message_link_id(message_id: str, index: int) -> str:
    return f"{message_id}#link{index}"


def parse_message_link_id(link_id: str) -> tuple[str, int] | None:
    match = _LINK_ID_RE.match(link_id.strip())
    if not match:
        return None
    return match.group("message_id"), int(match.group("index"))


def collect_message_links(message: NormalizedMessage) -> list[LinkMeta]:
    explicit = list(message.links)
    known_urls = {item.url for item in explicit}

    for url in extract_urls(message.text):
        url = _normalize_extracted_url(url)
        if not url:
            continue
        if url in known_urls:
            continue
        explicit.append(
            LinkMeta(
                url=url,
                title="",
                link_type=classify_link_type(url),
            )
        )
    return explicit


def build_message_link_candidates(message: NormalizedMessage) -> list[MessageLinkCandidate]:
    return [
        MessageLinkCandidate(
            link_id=build_message_link_id(message.message_id, index),
            message_id=message.message_id,
            url=link.url,
            title=link.title,
            link_type=link.link_type,
        )
        for index, link in enumerate(collect_message_links(message), start=1)
    ]


def classify_link_type(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "feishu" in host or "larksuite" in host:
        return LinkType.FEISHU_DOC.value
    return LinkType.NORMAL.value


def _normalize_extracted_url(url: str) -> str:
    cleaned = url.strip()
    cleaned = _URL_TRAILING_HTML_RE.sub("", cleaned)
    return cleaned.rstrip(")>]}\"'，。！？、；;")


def sort_referenced_link_ids(
    link_ids: list[str],
    *,
    message_order: list[str],
) -> list[str]:
    order_map = {message_id: index for index, message_id in enumerate(message_order)}
    deduped = list(dict.fromkeys(link_id.strip() for link_id in link_ids if link_id.strip()))

    def _sort_key(link_id: str) -> tuple[int, int, str]:
        parsed = parse_message_link_id(link_id)
        if parsed is None:
            return (len(order_map), 10**9, link_id)
        message_id, index = parsed
        return (order_map.get(message_id, len(order_map)), index, link_id)

    return sorted(deduped, key=_sort_key)
