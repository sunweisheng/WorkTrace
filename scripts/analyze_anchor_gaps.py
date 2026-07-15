from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--date", required=True, help="Target date in YYYY-MM-DD format.")
    parser.add_argument("--trace-root", default=None)
    parser.add_argument("--output-root", default=None)
    return parser.parse_args(argv)


def _load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected an object in {path}")
    return payload


def _self_open_id(trace_root: Path) -> str:
    summary = _load_json(trace_root / "summary.json")
    events = summary.get("timing_summary", {})
    events = events.get("events", []) if isinstance(events, dict) else []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        raw_line = str(event.get("raw_line", ""))
        match = re.search(r'open_id="([^"]+)"', raw_line)
        if match:
            return match.group(1)
    raise ValueError("Could not find the current user identity in replay summary.")


def _collect_messages(debug_root: Path) -> dict[str, list[dict[str, object]]]:
    by_conversation: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for path in debug_root.glob("_segment_batches/**/segmentation_input.json"):
        payload = _load_json(path)
        conversation_id = str(payload.get("conversation_id", ""))
        raw_messages = payload.get("messages", [])
        if not conversation_id or not isinstance(raw_messages, list):
            continue
        for message in raw_messages:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("message_id", ""))
            if message_id:
                by_conversation[conversation_id][message_id] = message
    return {
        conversation_id: sorted(
            messages.values(),
            key=lambda item: (str(item.get("send_time", "")), str(item.get("message_id", ""))),
        )
        for conversation_id, messages in by_conversation.items()
    }


def _event_membership(final_events_path: Path) -> dict[str, set[str]]:
    payload = _load_json(final_events_path)
    memberships: dict[str, set[str]] = defaultdict(set)
    raw_events = payload.get("events", [])
    if not isinstance(raw_events, list):
        return memberships
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("event_id", ""))
        raw_message_ids = event.get("source_message_ids", [])
        if not event_id or not isinstance(raw_message_ids, list):
            continue
        for message_id in raw_message_ids:
            memberships[str(message_id)].add(event_id)
    return memberships


def _anchor_clusters(
    messages: list[dict[str, object]],
    *,
    self_open_id: str,
) -> list[dict[str, object]]:
    clusters: list[dict[str, object]] = []
    current: list[tuple[int, dict[str, object]]] = []
    for index, message in enumerate(messages):
        if str(message.get("sender_open_id", "")) == self_open_id:
            current.append((index, message))
            continue
        if current:
            clusters.append(_build_cluster(current))
            current = []
    if current:
        clusters.append(_build_cluster(current))
    return clusters


def _build_cluster(items: list[tuple[int, dict[str, object]]]) -> dict[str, object]:
    first_index, first_message = items[0]
    last_index, last_message = items[-1]
    return {
        "start_index": first_index,
        "end_index": last_index,
        "start_time": str(first_message.get("send_time", "")),
        "end_time": str(last_message.get("send_time", "")),
        "message_ids": [str(message.get("message_id", "")) for _, message in items],
    }


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _quantiles(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "p75": None, "max": None}
    ordered = sorted(values)
    p75_index = round((len(ordered) - 1) * 0.75)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "median": round(median(ordered), 3),
        "p75": round(ordered[p75_index], 3),
        "max": round(ordered[-1], 3),
    }


def _pair_rows(
    conversations: dict[str, list[dict[str, object]]],
    memberships: dict[str, set[str]],
    *,
    self_open_id: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for conversation_id, messages in conversations.items():
        clusters = _anchor_clusters(messages, self_open_id=self_open_id)
        for previous, current in zip(clusters, clusters[1:]):
            previous_events = set().union(*(memberships[item] for item in previous["message_ids"]))
            current_events = set().union(*(memberships[item] for item in current["message_ids"]))
            if previous_events and current_events:
                relation = "same_event" if previous_events & current_events else "different_event"
            else:
                relation = "unclassified"
            gap_minutes = (
                _parse_time(str(current["start_time"]))
                - _parse_time(str(previous["end_time"]))
            ).total_seconds() / 60
            rows.append(
                {
                    "conversation_id": conversation_id,
                    "relation": relation,
                    "gap_minutes": round(gap_minutes, 3),
                    "intervening_message_count": int(current["start_index"]) - int(previous["end_index"]) - 1,
                    "previous_event_ids": sorted(previous_events),
                    "current_event_ids": sorted(current_events),
                }
            )
    return rows


def _build_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    by_relation: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_relation[str(row["relation"])].append(row)
    return {
        "adjacent_anchor_pair_count": len(rows),
        "same_event_pairs": {
            "gap_minutes": _quantiles([float(row["gap_minutes"]) for row in by_relation["same_event"]]),
            "intervening_message_count": _quantiles([float(row["intervening_message_count"]) for row in by_relation["same_event"]]),
        },
        "different_event_pairs": {
            "gap_minutes": _quantiles([float(row["gap_minutes"]) for row in by_relation["different_event"]]),
            "intervening_message_count": _quantiles([float(row["intervening_message_count"]) for row in by_relation["different_event"]]),
        },
        "unclassified_pair_count": len(by_relation["unclassified"]),
    }


def _render_markdown(target_date: str, summary: dict[str, object]) -> str:
    same = summary["same_event_pairs"]
    different = summary["different_event_pairs"]
    lines = [
        f"# {target_date} 本人发言间隔分析",
        "",
        "- 数据来源：当天调试输入中的去重消息，以及最终事件的来源消息。",
        "- 口径：连续本人发言合并为一个发言段；仅当相邻两段都关联到最终事件时，才能判断为同一事项或不同事项。",
        f"- 相邻发言段对数：{summary['adjacent_anchor_pair_count']}；无法分类：{summary['unclassified_pair_count']}。",
        "",
        "| 关系 | 样本数 | 时间间隔中位数（分钟） | 时间间隔 P75（分钟） | 中间消息数中位数 | 中间消息数 P75 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, values in (("同一事项", same), ("不同事项", different)):
        gap = values["gap_minutes"]
        messages = values["intervening_message_count"]
        lines.append(
            f"| {label} | {gap['count']} | {gap['median']} | {gap['p75']} | {messages['median']} | {messages['p75']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    trace_root = Path(args.trace_root) if args.trace_root else Path("data") / "replay-trace" / args.date
    output_root = Path(args.output_root) if args.output_root else trace_root
    debug_root = trace_root / "conversation_debug" / args.date
    conversations = _collect_messages(debug_root)
    memberships = _event_membership(debug_root / "final_events.json")
    rows = _pair_rows(conversations, memberships, self_open_id=_self_open_id(trace_root))
    summary = _build_summary(rows)
    payload = {
        "target_date": args.date,
        "source": {
            "conversation_debug_root": str(debug_root.resolve()),
            "final_events_path": str((debug_root / "final_events.json").resolve()),
        },
        "summary": summary,
        "pairs": rows,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    json_path = output_root / "anchor-gap-analysis.json"
    markdown_path = output_root / "anchor-gap-analysis.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(_render_markdown(args.date, summary), encoding="utf-8")
    print(json.dumps({"json_path": str(json_path.resolve()), "markdown_path": str(markdown_path.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
