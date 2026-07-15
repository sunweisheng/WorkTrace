from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.models import NormalizedMessage
from src.worktrace.pipeline.initial_windows import build_initial_anchor_windows


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a read-only preview of deterministic conversation windows."
    )
    parser.add_argument("--date", required=True, help="Target date in YYYY-MM-DD format.")
    parser.add_argument("--trace-root", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument(
        "--resolve-chat-modes",
        action="store_true",
        help="Read current Feishu chat modes so historical debug input can distinguish p2p chats.",
    )
    return parser.parse_args(argv)


def _load_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _load_self_open_id(trace_root: Path) -> str:
    summary = _load_object(trace_root / "summary.json")
    timing = summary.get("timing_summary", {})
    for event in timing.get("events", []) if isinstance(timing, dict) else []:
        if not isinstance(event, dict):
            continue
        matched = re.search(r'open_id="([^"]+)"', str(event.get("raw_line", "")))
        if matched:
            return matched.group(1)
    raise ValueError("Could not find the current user identity in replay summary.")


def _load_messages(debug_root: Path) -> list[NormalizedMessage]:
    by_conversation: dict[str, dict[str, NormalizedMessage]] = defaultdict(dict)
    for path in debug_root.glob("_segment_batches/**/segmentation_input.json"):
        payload = _load_object(path)
        conversation_id = str(payload.get("conversation_id", ""))
        raw_messages = payload.get("messages", [])
        if not conversation_id or not isinstance(raw_messages, list):
            continue
        for raw_message in raw_messages:
            if isinstance(raw_message, dict):
                message = NormalizedMessage.from_dict(raw_message)
                if message.message_id:
                    by_conversation[conversation_id][message.message_id] = message
    return [
        message
        for conversation_id in sorted(by_conversation)
        for message in sorted(
            by_conversation[conversation_id].values(),
            key=lambda item: (item.send_time, item.message_id),
        )
    ]


def _resolve_chat_modes(messages: list[NormalizedMessage], target_date: str) -> list[NormalizedMessage]:
    modes: dict[str, str] = {}
    for conversation_id in sorted({message.conversation_id for message in messages}):
        completed = subprocess.run(
            [
                "lark-cli",
                "im",
                "+messages-search",
                "--as",
                "user",
                "--chat-id",
                conversation_id,
                "--start",
                f"{target_date}T00:00:00+08:00",
                "--end",
                f"{target_date}T23:59:59+08:00",
                "--page-size",
                "1",
                "--no-reactions",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            continue
        try:
            payload = json.loads(completed.stdout)
            raw_messages = payload.get("data", {}).get("messages", [])
            raw_mode = raw_messages[0].get("chat_type", "") if raw_messages else ""
        except (json.JSONDecodeError, AttributeError, IndexError):
            continue
        mode = str(raw_mode).strip().lower()
        if mode in {"p2p", "group", "topic"}:
            modes[conversation_id] = mode
    return [replace(message, conversation_mode=modes.get(message.conversation_id, message.conversation_mode)) for message in messages]


def _render_message(message: NormalizedMessage) -> list[str]:
    text = message.text or "[无文本内容]"
    return [
        f"- {message.sender_name or message.sender_open_id or '未知'}：{text}",
        "",
    ]


def render_preview(target_date: str, windows, config: RuntimeConfig) -> str:
    lines = [
        f"# {target_date} 初步窗口内容预览",
        "",
        "- 本报告只读取已有调试聊天输入，不调用模型，不生成日报，也不发送消息。",
        f"- 初步窗口数：{len(windows)}",
        "- 规则参数："
        f"间隔超过 {config.max_anchor_gap_minutes} 分钟切窗；"
        f"中间无关他人消息超过 {config.max_unrelated_intervening_messages} 条切窗；"
        f"模型按需补充每个方向 {config.context_expansion_messages_per_direction} 条、最多 {config.context_expansion_round_limit} 轮（本报告未执行）。",
        "",
    ]
    for index, window in enumerate(windows, start=1):
        main_ids = set(window.base_message_ids)
        relation_ids = set(window.relation_context_message_ids)
        timeline_context_ids = set(window.timeline_context_message_ids)
        messages = sorted(window.messages, key=lambda item: (item.send_time, item.message_id))
        main_messages = [message for message in messages if message.message_id in main_ids]
        first_time = main_messages[0].send_time if main_messages else ""
        last_time = main_messages[-1].send_time if main_messages else ""
        conversation_mode = "私聊" if any(
            message.conversation_mode == "p2p" for message in messages
        ) else "群聊"
        lines.extend(
            [
                f"## 窗口 {index:03d}",
                "",
                f"- 会话类型：{conversation_mode}",
                f"- 主消息时间范围：{first_time} 至 {last_time}",
                f"- 窗口主消息数：{len(main_ids)}",
                f"- 关系上下文数：{len(relation_ids)}",
                f"- 自动前文数：{len(timeline_context_ids)}",
                f"- 锚点消息数：{len(window.anchor_message_ids)}",
                "",
            ]
        )
        for message in messages:
            lines.extend(_render_message(message))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    trace_root = Path(args.trace_root) if args.trace_root else Path("data") / "replay-trace" / args.date
    debug_root = trace_root / "conversation_debug" / args.date
    config = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())
    messages = _load_messages(debug_root)
    if args.resolve_chat_modes:
        messages = _resolve_chat_modes(messages, args.date)
    windows = build_initial_anchor_windows(
        messages,
        _load_self_open_id(trace_root),
        max_anchor_gap_minutes=config.max_anchor_gap_minutes,
        max_unrelated_intervening_messages=config.max_unrelated_intervening_messages,
        initial_context_messages_before=config.initial_context_messages_before,
    )
    output = Path(args.output) if args.output else trace_root / "initial-windows-preview.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_preview(args.date, windows, config), encoding="utf-8")
    print(json.dumps({"output_path": str(output.resolve()), "window_count": len(windows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
