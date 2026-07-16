from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CallInputRecord:
    category: str
    purpose: str
    source_path: Path
    item_count: int
    time_range: str
    content_summary: str


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--date", required=True, help="Target date in YYYY-MM-DD format.")
    parser.add_argument("--trace-root", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--max-message-excerpts", type=int, default=6)
    parser.add_argument("--max-excerpt-chars", type=int, default=120)
    return parser.parse_args(argv)


def _load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected an object in {path}")
    return payload


def _truncate(text: object, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."


def _unique_messages(payload: dict[str, object]) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    raw_segments = payload.get("segments", [])
    if not isinstance(raw_segments, list):
        return messages
    for segment in raw_segments:
        if not isinstance(segment, dict):
            continue
        raw_messages = segment.get("messages", [])
        if not isinstance(raw_messages, list):
            continue
        for message in raw_messages:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("message_id", ""))
            if message_id and message_id in seen_ids:
                continue
            if message_id:
                seen_ids.add(message_id)
            messages.append(message)
    return messages


def _message_summary(
    messages: list[dict[str, object]],
    *,
    max_excerpts: int,
    max_chars: int,
) -> tuple[str, str]:
    if not messages:
        return "-", "无消息正文。"
    ordered = sorted(messages, key=lambda item: str(item.get("send_time", "")))
    timestamps = [str(item.get("send_time", "")) for item in ordered if item.get("send_time")]
    time_range = "-" if not timestamps else f"{timestamps[0]} 至 {timestamps[-1]}"
    excerpts: list[str] = []
    for message in ordered:
        text = _truncate(message.get("text"), max_chars)
        if not text or text == "[Sticker]":
            continue
        time_value = str(message.get("send_time", ""))[11:16]
        sender = _truncate(message.get("sender_name"), 24) or "未知发送者"
        excerpts.append(f"{time_value} {sender}: {text}")
        if len(excerpts) >= max_excerpts:
            break
    return time_range, "；".join(excerpts) if excerpts else "仅包含表情、图片或空文本。"


def _candidate_summary(
    payload: dict[str, object],
    *,
    max_excerpts: int,
    max_chars: int,
) -> tuple[int, str]:
    candidates = payload.get("candidates", payload.get("unassigned_candidates", []))
    if not isinstance(candidates, list):
        return 0, "无候选事项正文。"
    excerpts: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        before = candidate.get("before", candidate)
        source = before if isinstance(before, dict) else candidate
        topic = _truncate(source.get("topic") or source.get("title"), 40)
        content = _truncate(
            source.get("content") or source.get("retention_detail"),
            max_chars,
        )
        excerpts.append(f"{topic}: {content}" if topic else content)
        if len(excerpts) >= max_excerpts:
            break
    return len(candidates), "；".join(excerpts) if excerpts else "无可显示的候选事项正文。"


def _segmentation_records(
    debug_root: Path,
    *,
    max_excerpts: int,
    max_chars: int,
) -> list[CallInputRecord]:
    records: list[CallInputRecord] = []
    for path in sorted(debug_root.glob("_segment_batches/**/segmentation_input.json")):
        payload = _load_json(path)
        raw_messages = payload.get("messages", [])
        messages = [item for item in raw_messages if isinstance(item, dict)] if isinstance(raw_messages, list) else []
        time_range, content_summary = _message_summary(
            messages,
            max_excerpts=max_excerpts,
            max_chars=max_chars,
        )
        records.append(
            CallInputRecord(
                category="会话切分",
                purpose="判断锚点窗口内哪些消息应作为独立工作事项的起点。",
                source_path=path,
                item_count=len(messages),
                time_range=time_range,
                content_summary=content_summary,
            )
        )
    return records


def _analysis_records(
    debug_root: Path,
    *,
    max_excerpts: int,
    max_chars: int,
) -> list[CallInputRecord]:
    records: list[CallInputRecord] = []
    for path in sorted(debug_root.glob("_segment_batches/**/analysis-*/input.json")):
        messages = _unique_messages(_load_json(path))
        time_range, content_summary = _message_summary(
            messages,
            max_excerpts=max_excerpts,
            max_chars=max_chars,
        )
        records.append(
            CallInputRecord(
                category="事件提炼",
                purpose="从已切分的消息片段中提炼候选工作事件和本人参与依据。",
                source_path=path,
                item_count=len(messages),
                time_range=time_range,
                content_summary=content_summary,
            )
        )
    return records


def _review_records(
    debug_root: Path,
    *,
    max_excerpts: int,
    max_chars: int,
) -> list[CallInputRecord]:
    definitions = [
        (
            "retention_review.json",
            "临时协作复核",
            "判断边界候选的原聊天包含临时协作信号还是实质工作信号。",
        ),
        (
            "personal_fact_review.json",
            "个人事实复核",
            "核对候选事件各字段是否得到原聊天消息支持。",
        ),
    ]
    records: list[CallInputRecord] = []
    for filename, category, purpose in definitions:
        path = debug_root / filename
        if not path.exists():
            continue
        payload = _load_json(path)
        batches = payload.get("batches", [])
        if not isinstance(batches, list):
            continue
        for batch in batches:
            if not isinstance(batch, dict):
                continue
            raw_candidates = batch.get("candidates", [])
            candidates = (
                [item for item in raw_candidates if isinstance(item, dict)]
                if isinstance(raw_candidates, list)
                else []
            )
            count, content_summary = _candidate_summary(
                {"candidates": candidates},
                max_excerpts=max_excerpts,
                max_chars=max_chars,
            )
            if not candidates:
                draft_ids = batch.get("draft_ids", [])
                if isinstance(draft_ids, list):
                    count = len(draft_ids)
                    content_summary = f"候选 ID：{', '.join(str(item) for item in draft_ids)}"
            attempt = int(batch.get("attempt", 0)) + 1
            status = str(batch.get("status", "unknown"))
            records.append(
                CallInputRecord(
                    category=f"{category}（第 {attempt} 次，{status}）",
                    purpose=purpose,
                    source_path=path,
                    item_count=count,
                    time_range="候选对应原聊天",
                    content_summary=content_summary,
                )
            )
    return records


def _merge_records(
    debug_root: Path,
    *,
    max_excerpts: int,
    max_chars: int,
) -> list[CallInputRecord]:
    merge_root = debug_root / "_merge_day_candidates"
    definitions = [
        ("input.json", "全日事件合并", "将不同会话的候选事件合并为同一工作事项。"),
        ("workstream_resolution_input.json", "工作流归属", "为候选事件确定所属工作流和父子关系。"),
        ("workstream_resolution_followup_input.json", "未归属事项复核", "为第一次未归属的候选事件补充既有工作流归属。"),
    ]
    records: list[CallInputRecord] = []
    for filename, category, purpose in definitions:
        path = merge_root / filename
        if not path.exists():
            continue
        count, content_summary = _candidate_summary(
            _load_json(path),
            max_excerpts=max_excerpts,
            max_chars=max_chars,
        )
        records.append(
            CallInputRecord(
                category=category,
                purpose=purpose,
                source_path=path,
                item_count=count,
                time_range="全天候选事项",
                content_summary=content_summary,
            )
        )
    return records


def _read_expected_call_count(trace_root: Path) -> int:
    summary = _load_json(trace_root / "summary.json")
    timing = summary.get("timing_summary", {})
    events = timing.get("events", []) if isinstance(timing, dict) else []
    if not isinstance(events, list):
        return 0
    return sum(
        1
        for item in events
        if isinstance(item, dict) and item.get("event") == "online_llm.request.completed"
    )


def _render_report(
    *,
    target_date: str,
    trace_root: Path,
    records: list[CallInputRecord],
    expected_call_count: int,
) -> str:
    lines = [
        f"# {target_date} 模型调用输入明细",
        "",
        f"- 计时日志中的文字模型请求数：{expected_call_count}",
        f"- 可从调试文件还原的调用输入数：{len(records)}",
        "- 统计口径：每行对应一份实际发送给在线模型的文字输入；内容摘录仅展示该输入内最早的若干条非空消息。",
        "",
        "| 序号 | 调用类别 | 调用目的 | 涉及数量 | 时间范围 | 内容摘录 | 调试输入 |",
        "| --- | --- | --- | ---: | --- | --- | --- |",
    ]
    for index, record in enumerate(records, start=1):
        relative_path = record.source_path.relative_to(trace_root)
        values = [
            str(index),
            record.category,
            record.purpose,
            str(record.item_count),
            record.time_range,
            record.content_summary,
            str(relative_path),
        ]
        lines.append("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.max_message_excerpts <= 0 or args.max_excerpt_chars <= 0:
        raise SystemExit("Excerpt limits must be positive.")
    trace_root = Path(args.trace_root) if args.trace_root else Path("data") / "replay-trace" / args.date
    debug_root = trace_root / "conversation_debug" / args.date
    if not debug_root.exists():
        raise SystemExit(f"Missing conversation debug directory: {debug_root}")

    records = [
        *_segmentation_records(
            debug_root,
            max_excerpts=args.max_message_excerpts,
            max_chars=args.max_excerpt_chars,
        ),
        *_analysis_records(
            debug_root,
            max_excerpts=args.max_message_excerpts,
            max_chars=args.max_excerpt_chars,
        ),
        *_review_records(
            debug_root,
            max_excerpts=args.max_message_excerpts,
            max_chars=args.max_excerpt_chars,
        ),
        *_merge_records(
            debug_root,
            max_excerpts=args.max_message_excerpts,
            max_chars=args.max_excerpt_chars,
        ),
    ]
    output_path = Path(args.output) if args.output else trace_root / "call-input-report.md"
    output_path.write_text(
        _render_report(
            target_date=args.date,
            trace_root=trace_root,
            records=records,
            expected_call_count=_read_expected_call_count(trace_root),
        ),
        encoding="utf-8",
    )
    print(output_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
