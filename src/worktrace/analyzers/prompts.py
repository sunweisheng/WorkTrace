from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import re

from ..config import RuntimeConfig
from ..constants import AnchorStatus
from ..constants import LinkType
from ..models import (
    AnalysisBatch,
    AnchorAnalysisResult,
    AnchorUnit,
    AttachmentTextBlock,
    CollectedSourceEvent,
    ConversationSegmentUnit,
    ConversationSegmentationResult,
    ConversationSlice,
    ContextRequest,
    LinkedFileTextBlock,
    NormalizedMessage,
    ResponseSignal,
    SegmentAnalysisBatch,
    SourceBackedEventDraft,
)
from ..utils.link_refs import build_message_link_candidates
from ..utils.json_io import dump_json
from ..utils.text import clean_text

_AUDIO_TAG_RE = re.compile(r"<audio\b[^>]*duration=\"([^\"]+)\"[^>]*/?>", re.IGNORECASE)
_VIDEO_TAG_RE = re.compile(r"<video\b[^>]*duration=\"([^\"]+)\"[^>]*/?>", re.IGNORECASE)
_IMAGE_TAG_RE = re.compile(r"^\[Image:\s*[^\]]+\]$", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"</?(?:p|div|span|strong|b|i|u|ol|ul|li|br)[^>]*>", re.IGNORECASE)
_AT_TAG_RE = re.compile(r"<at\b[^>]*>(.*?)</at>|<at\b[^>]*/>", re.IGNORECASE)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_URL_RE = re.compile(r"https?://[^\s)\]<>]+")
_EMOJI_TOKEN_RE = re.compile(r":[A-Za-z0-9_+-]{3,}:")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINE_RE = re.compile(r"\n{3,}")


LOW_RETENTION_EVENT_RULES = [
    "私人饭局、约饭、离职告别聚餐、同事口碑评价、人际寒暄，不要提炼为事项。",
    "个人请假、家庭原因、孩子学校证明、个人行程报备，不要提炼为工作事件。",
    "加班、请假、补卡、考勤、调休、外出报备等行政流程审批，不要提炼为工作事项。",
    "泛泛完成审核/审批/工作审核/审核任务但没有具体业务对象、审批结论、问题、风险、金额、客户、项目、文档或后续动作，不要提炼为事项。",
    "反例：产品同事评价不错，今晚在公司旁边吃牛蛙火锅，饭后回去准备述职材料，不要输出 candidate_event。",
    "反例：本人明天晚到，需去学校为孩子开证明，不要输出 candidate_event。",
    "反例：完成了郭海提交的工作审核，并同步审核结果，不要输出 candidate_event。",
    "正例：审核客户合同并反馈付款条款问题，可以输出 candidate_event。",
]

RETENTION_COMPLETENESS_RULE = (
    "只有同时具备具体对象、保留理由、保留依据的工作事件才输出；"
    "缺少任一项时，不要输出 candidate_event。"
)

RETENTION_DETAIL_EVIDENCE_RULE = (
    "retention_detail 表示保留依据/来源证据，用一句话写清楚来源会话、"
    "发起人或确认人、关键动作或结论；不要只写泛泛的价值判断，"
    "不要写 message id、open_id、conversation_id 或 om_/ou_/oc_ 等内部标识。"
)


def build_batch_analysis_prompt(
    batch: AnalysisBatch,
    *,
    config: RuntimeConfig | None = None,
) -> str:
    runtime_config = config or RuntimeConfig()
    protocol = {
        "instruction": (
            "按会话切片提炼当天讨论过的工作事项摘要。"
            "只返回一个 JSON 对象，包含 candidate_events 和 context_requests。"
            "请给我简洁的答案，不要推理，跳过思考步骤。"
            "直接作答，不要展示你的推理过程。"
        ),
        "rules": [
            "只提炼工作事项。",
            "只提炼与本人直接相关的工作事项。",
            "本人信息见 input.self；只有事项明确由本人发起、本人负责、本人审批、本人催办、本人汇报、本人跟进，或他人明确要求本人推进/处理时，才提炼。",
            "如果事项主体明显是他人的工作、他人的进展、他人的承诺，而本人只是参与了会话或说过别的话，不要提炼。",
            "如果只是同群讨论背景信息、但没有明确落到本人，也不要提炼。",
            "咨询类事件、流程审核类事件、团建活动组织类事件、技能培训类事件，默认不要提炼；这类事项对后续公司级长期事件沉淀价值较低。",
            *LOW_RETENTION_EVENT_RULES,
            RETENTION_COMPLETENESS_RULE,
            _build_sensitive_rule(runtime_config),
            "一件事写一条；如果有多件事就拆开。",
            "topic 写短标题，content 写完整事项；如果有明确结果，直接融入 content，不要单独返回 result。",
            "action_label 只写主要动作标签，例如：回复、审批、催办、撰写、核对、跟进、同步、确认。",
            "object_hint 只写该事项的核心对象或主题，例如：提前付款、优惠券配置、汇报文档、上海点位签约方案。",
            (
                "retention_reason 必须从以下枚举选择：deliverable_updated、decision_made、"
                "issue_or_risk_found、follow_up_assigned、external_business_progress、substantive_approval。"
            ),
            RETENTION_DETAIL_EVIDENCE_RULE,
            "普通约时间、确认开会、互通信息、泛泛完成审核/审批但没有具体对象和结论的内容，不要输出 candidate_event。",
            "每条事项附上最相关的消息 id。",
            "只能使用输入里出现过的真实 message id，不要自造占位符 id。",
            "如需给事项挂涉及文件，只能从对应 source_message_ids 的 links 里选择 referenced_link_ids；拿不准就返回空数组。",
            "正例：本人要求他人汇报、本人审批、本人同步、本人催办、本人推进，都算与本人直接相关。",
            "反例：他人之间讨论自己的工作、自己的承诺、自己的处理进度，即使本人在该会话里发过言，也不算与本人直接相关。",
            "如果当前消息是在纠正、澄清或替换前文对象，topic、content、object_hint 必须以当前消息确认后的对象为准。",
            "reply_to 或 quote_to 里的内容只能作为背景，不能覆盖当前消息里更具体、更晚确认的对象。",
            "如果 reply_to 或 quote_to 指向附件、文件消息、飞书文档或 wiki，且事件判断依赖其内容，必须返回 context_requests 请求补读，不要猜。",
            "拿不准就用 context_requests，不要猜。",
        ],
        "required_output_schema": {
            "candidate_events": [
                {
                    "topic": "string",
                    "content": "string",
                    "action_label": "string",
                    "object_hint": "string",
                    "retention_reason": "deliverable_updated | decision_made | issue_or_risk_found | follow_up_assigned | external_business_progress | substantive_approval",
                    "retention_detail": "string",
                    "referenced_link_ids": ["message_id#link1"],
                    "source_message_ids": ["message_id"],
                }
            ],
            "context_requests": [
                {
                    "request_type": "earlier_messages | later_messages | attachment_text | linked_file_text",
                    "target_message_ids": ["message_id"],
                    "target_attachment_ids": ["attachment_id"],
                    "target_link_ids": ["message_id#link1"],
                }
            ],
        },
        "input": serialize_batch_for_prompt(batch, config=runtime_config),
    }
    return dump_json(protocol, pretty=True)


def build_conversation_segmentation_prompt(
    *,
    target_date: str,
    conversation_id: str,
    conversation_name: str,
    messages: list[NormalizedMessage],
    self_open_id: str,
    self_display_name: str,
    response_signals: list[ResponseSignal],
    hard_boundary_before_ids: set[str],
    config: RuntimeConfig | None = None,
) -> str:
    runtime_config = config or RuntimeConfig()
    message_lookup = {message.message_id: message for message in messages}
    message_refs = _build_segmentation_message_refs(messages)
    signal_refs = _build_segmentation_signal_refs(response_signals)
    serialized_messages: list[dict[str, object]] = []
    for message in messages:
        serialized = serialize_message_for_prompt(
            message,
            runtime_config,
            message_lookup=message_lookup,
        )
        serialized = _replace_segmentation_message_refs(serialized, message_refs)
        serialized["sent_by_self"] = message.sender_open_id == self_open_id
        serialized["mentions_self"] = self_open_id in message.mentioned_open_ids
        serialized["mentions_other"] = bool(
            set(message.mentioned_open_ids) - {self_open_id}
        )
        if message.message_id in hard_boundary_before_ids:
            serialized["hard_boundary_before"] = True
        serialized_messages.append(serialized)

    protocol = {
        "instruction": (
            "按时间线判断一个会话中每个独立会话轮次的起点。"
            "只返回 JSON 的 segment_start_message_ids，不要提炼或筛选工作事件。"
            "请直接作答，不要推理、展示思考过程或添加解释。"
        ),
        "rules": [
            "这是分段而非事项筛选：无论消息是否构成工作事件，都必须返回至少一个轮次起点。",
            "输入消息的时间顺序固定，绝不能移动、重排、归组或重复任何消息。",
            "只返回每个轮次的第一条 message_ref；Python 会按 message_refs_in_order 自动包含起点之间的所有消息。",
            "segment_start_message_ids 必须以 message_refs_in_order 的第一项开始，后续仅能选择其中按原顺序出现的消息。",
            "连续围绕同一对象的发言、回复和确认默认合并为同一轮次；不要因每次单独发送就切成单消息轮次。",
            "仅在明确换题、hard_boundary_before 或旧话题被 reply/quote 续谈时开始新轮次。",
            "标记 hard_boundary_before 的消息必须出现在 segment_start_message_ids 中。",
            "本人文本和本人表情都是参与信号，不等于同意、完成或结束；必须结合后续沟通判断是否仍为同一话题。",
            "旧事项被 reply 或 quote 续谈时，回复消息在它实际发生的时间位置开始新轮次，不能移回旧事项旁边。",
        ],
        "input": {
            "target_date": target_date,
            "conversation_id": conversation_id,
            "conversation_name": conversation_name,
            "self": {"open_id": self_open_id, "display_name": self_display_name},
            "message_refs_in_order": list(message_refs.values()),
            "messages": serialized_messages,
            "response_signals": [
                {
                    **item.to_dict(),
                    "signal_id": signal_refs[item.signal_id],
                    "message_id": message_refs.get(item.message_id, item.message_id),
                }
                for item in response_signals
            ],
        },
        "required_output_schema": {
            "segment_start_message_ids": ["message_ref"]
        },
    }
    return dump_json(protocol, pretty=True)


def restore_conversation_segmentation_references(
    result: ConversationSegmentationResult,
    *,
    messages: list[NormalizedMessage],
    response_signals: list[ResponseSignal],
) -> ConversationSegmentationResult:
    """Restore internal prompt references before strict message ownership checks."""
    message_ref_to_id = {
        ref: message_id
        for message_id, ref in _build_segmentation_message_refs(messages).items()
    }
    return ConversationSegmentationResult(
        segment_start_message_ids=[
            message_ref_to_id.get(item, item)
            for item in result.segment_start_message_ids
        ],
        segments=[
            replace(
                segment,
                primary_message_ids=[
                    message_ref_to_id.get(item, item)
                    for item in segment.primary_message_ids
                ],
            )
            for segment in result.segments
        ]
    )


def _build_segmentation_message_refs(
    messages: list[NormalizedMessage],
) -> dict[str, str]:
    return {
        message.message_id: f"m{index:03d}"
        for index, message in enumerate(messages, start=1)
    }


def _build_segmentation_signal_refs(
    response_signals: list[ResponseSignal],
) -> dict[str, str]:
    return {
        signal.signal_id: f"r{index:03d}"
        for index, signal in enumerate(response_signals, start=1)
    }


def _replace_segmentation_message_refs(
    value: object,
    message_refs: dict[str, str],
) -> object:
    if isinstance(value, str):
        return message_refs.get(value, value)
    if isinstance(value, list):
        return [
            _replace_segmentation_message_refs(item, message_refs) for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _replace_segmentation_message_refs(item, message_refs)
            for key, item in value.items()
        }
    return value


def build_segment_batch_analysis_prompt(
    batch: SegmentAnalysisBatch,
    *,
    config: RuntimeConfig | None = None,
) -> str:
    runtime_config = config or RuntimeConfig()
    protocol = {
        "instruction": (
            "一次提炼多个彼此隔离的本人会话轮次。"
            "每个 result 只能使用同一 segment_id 的消息与上下文。"
            "只返回 JSON results，不要推理、展示思考过程或添加解释。"
        ),
        "rules": [
            "每个 segment 独立判断，禁止从其它 segment 借用事实、对象、结论或来源。",
            "每个输入 segment_id 必须且只能返回一个 result。",
            "candidate_events 的 source_message_ids 只能使用该 segment 的 primary_message_ids，不能使用 context_message_ids。",
            "每条 candidate 至少引用一条本人参与证据，或引用该 segment 的本人回应 signal。",
            "本人提出的问题、风险和待确认事项本身可以提炼，不要求已有处理结果。",
            "表情是本人回复证据，但不能单凭表情描述事项已完成、已同意或已拒绝。",
            "普通约时间、确认开会、互通信息、泛泛审核/审批且无具体对象和结论，不要输出 candidate_event。",
            RETENTION_COMPLETENESS_RULE,
            "上下文消息只用于理解当前主消息，不得作为当前事件来源。",
        ],
        "input": {
            "target_date": batch.target_date,
            "conversation_id": batch.conversation_id,
            "conversation_name": batch.conversation_name,
            "self": {
                "open_id": batch.self_open_id,
                "display_name": batch.self_display_name,
            },
            "segments": [
                _serialize_segment_unit_for_prompt(item, runtime_config)
                for item in batch.segments
            ],
        },
        "required_output_schema": {
            "results": [
                {
                    "segment_id": "segment_id",
                    "analysis": {
                        "candidate_events": [
                            {
                                "topic": "string",
                                "content": "string",
                                "action_label": "string",
                                "object_hint": "string",
                                "retention_reason": "deliverable_updated | decision_made | issue_or_risk_found | follow_up_assigned | external_business_progress | substantive_approval",
                                "retention_detail": "string",
                                "referenced_link_ids": ["message_id#link1"],
                                "source_message_ids": ["primary_message_id"],
                            }
                        ],
                        "context_requests": [],
                    },
                }
            ]
        },
    }
    return dump_json(protocol, pretty=True)


def build_merge_prompt(target_date: str, candidates: list[SourceBackedEventDraft]) -> str:
    protocol = {
        "instruction": (
            "按是否描述同一真实工作事件，对同一天的候选事项做跨会话分组。"
            "只返回一个 JSON 对象，包含 groups。"
            "请给我简洁的答案，不要推理，跳过思考步骤。"
            "直接作答，不要展示你的推理过程。"
        ),
        "rules": [
            "只把明显属于同一真实事件的事项分到一起。",
            "如果拿不准，宁可分开。",
            "背景相同不等于同一事件。",
            "动作类型不同通常不是同一事件。",
            "同步/通知 和 核对/执行/跟进，通常不是同一事件。",
            "每个 draft_id 必须且只能出现在一个 group 里。",
            "禁止漏掉任何 draft_id。输出前必须逐个核对 candidates 里的全部 draft_id 都已被返回一次且仅一次。",
            "错误示例：candidates 有 [d1, d2, d3]，但只返回 [['d1', 'd2']]，漏掉 d3，这是错误的。",
            "正确示例：candidates 有 [d1, d2, d3]，若 d3 无法与其他事项合并，也必须返回 [['d1', 'd2'], ['d3']]。",
        ],
        "required_output_schema": {
            "groups": [
                {
                    "group_id": "string",
                    "draft_ids": ["draft_id"],
                }
            ]
        },
        "target_date": target_date,
        "candidates": [
            {
                "draft_id": candidate.draft_id,
                "action_label": candidate.action_label,
                "object_hint": candidate.object_hint,
                "source_conversation_id": candidate.source_conversation_id,
                "topic": candidate.topic,
                "content": candidate.content,
            }
            for candidate in candidates
        ],
    }
    return dump_json(protocol, pretty=True)


def build_collected_merge_prompt(
    target_date: str,
    events: list[CollectedSourceEvent],
    deterministic_groups: list[list[str]],
    *,
    config: RuntimeConfig | None = None,
) -> str:
    runtime_config = config or RuntimeConfig()
    deterministic_ids = {
        draft_id for group in deterministic_groups for draft_id in group
    }
    merge_owner_person = next(
        (
            item.person_name
            for item in events
            if item.is_merge_owner_source and item.person_name.strip()
        ),
        "",
    )
    protocol = {
        "instruction": (
            "面向管理人员合并多人 WorkTrace 日报事件。"
            "只返回一个 JSON 对象，包含 groups。"
            "请给我简洁的答案，不要推理，跳过思考步骤。"
            "直接作答，不要展示你的推理过程。"
        ),
        "rules": [
            "每个输出 group 表示一个真实工作事件。",
            "deterministic_groups 是 Python 已按相同原始 event_id 确定的合并组，必须原样保留，不要拆分，也不要把组内 draft_id 改到别的组。",
            "remaining_events 中只有明显属于同一真实事件的事项才合并；拿不准就分开。",
            "每个输入 draft_id 必须且只能出现在一个 group 里。",
            "每个 group 必须返回管理人员可读的 title 和 content。",
            "title 要短，content 要综合保留来源中的关键事实、进展、结果和未决事项。",
            "不要编造输入中没有的信息，不要丢失关键事实。",
            "是否属于同一真实事件仍由你判断，不要依赖 Python 预先给出同题结论。",
            (
                "如果某个 group 中包含 is_merge_owner_source=true 的来源事件，"
                "最终 title、content、object_hint、retention_reason、retention_detail "
                "都必须以该来源事件为主，其他来源只能补充不冲突的信息，"
                "不能覆盖其中已明确写出的版本号、结论、进展、结果或待办指向。"
            ),
            (
                "反例：普通员工写 WorkTrace 技能升级到 1.0.4，"
                "合并人来源写升级到 1.0.5；如果你判断它们属于同一真实事件，"
                "最终 group 必须以 1.0.5 为主事实，不能改回 1.0.4。"
            ),
            (
                "每个 group 必须返回 object_hint、retention_reason、retention_detail；"
                "retention_reason 只能是 deliverable_updated、decision_made、issue_or_risk_found、"
                "follow_up_assigned、external_business_progress、substantive_approval。"
            ),
            (
                "retention_detail 不能为空，必须说明为什么这个事件值得保留；"
                "不能只照抄 title、content 或 object_hint，也不能只写已确认、已同步、已处理。"
            ),
            "如果来源事件只是普通约时间、互通信息、泛泛完成审核/审批且无具体对象和结论，不要输出对应 group。",
            _build_sensitive_rule(runtime_config),
            "涉及上述敏感事项时，不要输出对应 group。",
        ],
        "required_output_schema": {
            "groups": [
                {
                    "group_id": "string",
                    "draft_ids": ["draft_id"],
                    "title": "string",
                    "content": "string",
                    "object_hint": "string",
                    "retention_reason": "deliverable_updated | decision_made | issue_or_risk_found | follow_up_assigned | external_business_progress | substantive_approval",
                    "retention_detail": "string",
                }
            ]
        },
        "target_date": target_date,
        "merge_owner_person": merge_owner_person,
        "deterministic_groups": deterministic_groups,
        "remaining_events": [
            _serialize_collected_source_event_for_prompt(item)
            for item in events
            if item.draft_id not in deterministic_ids
        ],
        "deterministic_group_events": [
            [
                _serialize_collected_source_event_for_prompt(item)
                for item in events
                if item.draft_id in set(group)
            ]
            for group in deterministic_groups
        ],
    }
    return dump_json(protocol, pretty=True)


def build_anchor_analysis_prompt(
    target_date: str,
    anchor_unit: AnchorUnit,
    *,
    pass_index: int = 1,
    config: RuntimeConfig | None = None,
) -> str:
    runtime_config = config or RuntimeConfig()
    protocol = {
        "instruction": (
            "分析一个锚点聊天窗口。"
            "只返回 JSON：anchor_status、candidate_events、context_requests、needs_cross_anchor_merge。"
            "请给我简洁的答案，不要推理，跳过思考步骤。"
            "直接作答，不要展示你的推理过程。"
        ),
        "rules": [
            (
                "anchor_status 只能是 "
                f"{AnchorStatus.COMPLETED.value}、{AnchorStatus.NEEDS_MORE_CONTEXT.value}、"
                f"{AnchorStatus.NEEDS_ATTACHMENT_TEXT.value}、{AnchorStatus.NOT_WORK_RELATED.value}、"
                f"{AnchorStatus.UNCERTAIN.value}。"
            ),
            "只抽取工作事件。",
            "咨询类事件、流程审核类事件、团建活动组织类事件、技能培训类事件，默认不要提炼；这类事项对后续公司级长期事件沉淀价值较低。",
            *LOW_RETENTION_EVENT_RULES,
            RETENTION_COMPLETENESS_RULE,
            "每个 candidate_event 只能落在当前 anchor_unit 内。",
            "每个 candidate_event 只表示一个主要动作。",
            "action_label 只写主要动作标签，例如：回复、审批、催办、撰写、核对、跟进、同步、确认。",
            "object_hint 只写该事项的核心对象或主题。",
            (
                "retention_reason 必须从以下枚举选择：deliverable_updated、decision_made、"
                "issue_or_risk_found、follow_up_assigned、external_business_progress、substantive_approval。"
            ),
            RETENTION_DETAIL_EVIDENCE_RULE,
            "普通约时间、确认开会、互通信息、泛泛完成审核/审批但没有具体对象和结论的内容，不要输出 candidate_event。",
            "如需给事项挂涉及文件，只能从对应 source_message_ids 的 links 里选择 referenced_link_ids；拿不准就返回空数组。",
            "如果窗口里有多个动作，就拆开。",
            "动作类型比共享名词更重要。",
            "同步/通知 与 核对/校验/执行/跟进，通常不是同一事件。",
            "content 里如果包含结果信息，也只能归属于自己的动作，不要串到别的动作上。",
            "例如：已同步给老板、老板未回复可视为已知悉，属于同步动作，不属于优惠券核对动作。",
            "如果上下文不够，就用 context_requests，不要猜。",
            "如果当前消息是在纠正、澄清或替换前文对象，topic、content、object_hint 必须以当前消息确认后的对象为准。",
            "reply_to 或 quote_to 里的内容只能作为背景，不能覆盖当前消息里更具体、更晚确认的对象。",
            "如果 reply_to 或 quote_to 指向附件、文件消息、飞书文档或 wiki，且事件判断依赖其内容，必须返回 context_requests 请求补读，不要猜。",
            "只有事件明显跨多个锚点窗口或会话时，needs_cross_anchor_merge 才设为 true。",
            "如果有明确结果，直接融入 content，不要单独返回 result。",
        ],
        "required_output_schema": {
            "anchor_status": [
                AnchorStatus.COMPLETED.value,
                AnchorStatus.NEEDS_MORE_CONTEXT.value,
                AnchorStatus.NEEDS_ATTACHMENT_TEXT.value,
                AnchorStatus.NOT_WORK_RELATED.value,
                AnchorStatus.UNCERTAIN.value,
            ],
            "candidate_events_item": {
                "topic": "string",
                "content": "string",
                "action_label": "string",
                "object_hint": "string",
                "retention_reason": "deliverable_updated | decision_made | issue_or_risk_found | follow_up_assigned | external_business_progress | substantive_approval",
                "retention_detail": "string",
                "referenced_link_ids": ["message_id#link1"],
                "source_message_ids": ["message_id"],
            },
            "context_requests_item": {
                "request_type": "earlier_messages | later_messages | attachment_text | linked_file_text",
                "target_message_ids": ["message_id"],
                "target_attachment_ids": ["attachment_id"],
                "target_link_ids": ["message_id#link1"],
            },
            "needs_cross_anchor_merge": "boolean",
        },
        "input": {
            "target_date": target_date,
            "pass_index": pass_index,
            "anchor_unit": serialize_anchor_unit_for_prompt(anchor_unit, runtime_config),
        },
    }
    return dump_json(protocol, pretty=True)


def build_anchor_batch_analysis_prompt(
    target_date: str,
    anchor_units: list[AnchorUnit],
    *,
    config: RuntimeConfig | None = None,
) -> str:
    runtime_config = config or RuntimeConfig()
    protocol = {
        "instruction": (
            "一次分析多个彼此独立的锚点聊天窗口。"
            "只返回 JSON，顶层键为 results。每个 result 必须对应一个 anchor_unit_id。"
            "请给我简洁的答案，不要推理，跳过思考步骤。"
            "直接作答，不要展示你的推理过程。"
        ),
        "rules": [
            "每个 anchor unit 独立判断，不要串信息。",
            "每个 result 必须包含 anchor_unit_id 和 analysis。",
            "只抽取工作事件。",
            "咨询类事件、流程审核类事件、团建活动组织类事件、技能培训类事件，默认不要提炼；这类事项对后续公司级长期事件沉淀价值较低。",
            *LOW_RETENTION_EVENT_RULES,
            RETENTION_COMPLETENESS_RULE,
            "每个 candidate_event 只能留在自己的 anchor_unit 内。",
            "每个 candidate_event 只表示一个主要动作。",
            "action_label 只写主要动作标签，例如：回复、审批、催办、撰写、核对、跟进、同步、确认。",
            "object_hint 只写该事项的核心对象或主题。",
            (
                "retention_reason 必须从以下枚举选择：deliverable_updated、decision_made、"
                "issue_or_risk_found、follow_up_assigned、external_business_progress、substantive_approval。"
            ),
            RETENTION_DETAIL_EVIDENCE_RULE,
            "普通约时间、确认开会、互通信息、泛泛完成审核/审批但没有具体对象和结论的内容，不要输出 candidate_event。",
            "如需给事项挂涉及文件，只能从对应 source_message_ids 的 links 里选择 referenced_link_ids；拿不准就返回空数组。",
            "如果同一窗口有多个动作，就拆开。",
            "动作类型比共享名词更重要。",
            "同步/通知 与 核对/校验/执行/跟进，通常不是同一事件。",
            "content 里如果包含结果信息，也只能归属于自己的动作，不要串到别的动作上。",
            "如果上下文不够，就用 context_requests，不要猜。",
            "如果当前消息是在纠正、澄清或替换前文对象，topic、content、object_hint 必须以当前消息确认后的对象为准。",
            "reply_to 或 quote_to 里的内容只能作为背景，不能覆盖当前消息里更具体、更晚确认的对象。",
            "如果 reply_to 或 quote_to 指向附件、文件消息、飞书文档或 wiki，且事件判断依赖其内容，必须返回 context_requests 请求补读，不要猜。",
            "只有事件明显跨多个锚点窗口或会话时，needs_cross_anchor_merge 才设为 true。",
            "如果有明确结果，直接融入 content，不要单独返回 result。",
        ],
        "required_output_schema": {
            "results": [
                {
                    "anchor_unit_id": "anchor_unit_id",
                    "analysis": {
                        "anchor_status": [
                            AnchorStatus.COMPLETED.value,
                            AnchorStatus.NEEDS_MORE_CONTEXT.value,
                            AnchorStatus.NEEDS_ATTACHMENT_TEXT.value,
                            AnchorStatus.NOT_WORK_RELATED.value,
                            AnchorStatus.UNCERTAIN.value,
                        ],
                        "candidate_events_item": {
                            "topic": "string",
                            "content": "string",
                            "action_label": "string",
                            "object_hint": "string",
                            "retention_reason": "deliverable_updated | decision_made | issue_or_risk_found | follow_up_assigned | external_business_progress | substantive_approval",
                            "retention_detail": "string",
                            "referenced_link_ids": ["message_id#link1"],
                            "source_message_ids": ["message_id"],
                        },
                        "context_requests_item": {
                            "request_type": "earlier_messages | later_messages | attachment_text | linked_file_text",
                            "target_message_ids": ["message_id"],
                            "target_attachment_ids": ["attachment_id"],
                            "target_link_ids": ["message_id#link1"],
                        },
                        "needs_cross_anchor_merge": "boolean",
                    },
                }
            ]
        },
        "input": {
            "target_date": target_date,
            "anchor_units": [
                serialize_anchor_unit_for_prompt(anchor_unit, runtime_config)
                | {"anchor_unit_id": anchor_unit.anchor_unit_id}
                for anchor_unit in anchor_units
            ],
        },
    }
    return dump_json(protocol, pretty=True)


def build_anchor_expansion_prompt(
    target_date: str,
    anchor_unit: AnchorUnit,
    previous_result: AnchorAnalysisResult,
    *,
    trigger_requests: list[ContextRequest],
    new_messages: list[NormalizedMessage],
    attachment_texts: list[AttachmentTextBlock],
    linked_file_texts: list[LinkedFileTextBlock] | None = None,
    pass_index: int = 2,
    config: RuntimeConfig | None = None,
) -> str:
    runtime_config = config or RuntimeConfig()
    protocol = {
        "instruction": (
            "在 Python 扩展上下文后，继续分析一个锚点聊天窗口。"
            "只返回一个 JSON 对象，包含 anchor_status、candidate_events、"
            "context_requests 和 needs_cross_anchor_merge。"
            "不要返回 markdown、解释或额外字段。"
            "请给我简洁的答案，不要推理，跳过思考步骤。"
            "直接作答，不要展示你的推理过程。"
        ),
        "rules": [
            (
                "anchor_status 只能是 "
                f"{AnchorStatus.COMPLETED.value}、"
                f"{AnchorStatus.NEEDS_MORE_CONTEXT.value}、"
                f"{AnchorStatus.NEEDS_ATTACHMENT_TEXT.value}、"
                f"{AnchorStatus.NOT_WORK_RELATED.value}、"
                f"{AnchorStatus.UNCERTAIN.value}。"
            ),
            "把 previous_analysis 作为先前状态；如果新上下文改变结论，必须修正。",
            "candidate_events 应表示当前 anchor_unit 的最新综合判断。",
            "每个 candidate_event 仍然只能表示一个主要动作或工作线索。",
            "每个 candidate_event 必须包含 object_hint、retention_reason 和 retention_detail。",
            RETENTION_COMPLETENESS_RULE,
            *LOW_RETENTION_EVENT_RULES,
            "泛泛完成审核/审批但没有具体业务对象、审批结论、问题、风险、金额、客户、项目、文档或后续动作，不要输出 candidate_event。",
            (
                "retention_reason 必须是 deliverable_updated、decision_made、issue_or_risk_found、"
                "follow_up_assigned、external_business_progress 或 substantive_approval。"
            ),
            RETENTION_DETAIL_EVIDENCE_RULE,
            "普通日程安排、会议时间确认、互通信息、没有具体对象和结论的泛泛审核/审批完成，不要输出 candidate_event。",
            "如需给事项挂涉及文件，只能从对应 source_message_ids 的 links 里选择 referenced_link_ids；拿不准就返回空数组。",
            (
                "如果新上下文显示某个先前 candidate_event 实际混合了多个动作，"
                "应拆成多个 candidate_events。"
            ),
            (
                "动作类型比共享背景名词更重要。通知/同步、审核/核对、执行、设计、"
                "审批、付款跟进、文档编辑通常是不同事件。"
            ),
            (
                "如果一部分主要是通知或同步信息，另一部分主要是检查、校验、执行或跟进，"
                "除非文本清楚表明它们是同一个连续动作，否则应保留为不同 candidate_events。"
            ),
            (
                "如果 content 包含结果，该结果只能归属于同一个 candidate_event 的主要动作。"
                "如果新增上下文显示结果属于另一个动作，应移动或拆分事件，不要混在一起。"
            ),
            (
                "例如：已同步给老板、老板未回复可视为已知悉，属于同步动作，"
                "不属于单独的优惠券配置核对动作。"
            ),
            "只有新增消息或附件正文仍无法解决事件判断时，才请求更多上下文。",
            "如果当前消息是在纠正、澄清或替换前文对象，topic、content、object_hint 必须以当前消息确认后的对象为准。",
            "reply_to 或 quote_to 里的内容只能作为背景，不能覆盖当前消息里更具体、更晚确认的对象。",
            "如果 reply_to 或 quote_to 指向附件、文件消息、飞书文档或 wiki，且事件判断依赖其内容，必须请求补读，不要猜。",
            "只有事件可能跨其他锚点窗口或会话时，needs_cross_anchor_merge 才设为 true。",
            "如果有明确结果，直接融入 content，不要单独返回 result。",
        ],
        "required_output_schema": {
            "anchor_status": (
                f"{AnchorStatus.COMPLETED.value} | "
                f"{AnchorStatus.NEEDS_MORE_CONTEXT.value} | "
                f"{AnchorStatus.NEEDS_ATTACHMENT_TEXT.value} | "
                f"{AnchorStatus.NOT_WORK_RELATED.value} | "
                f"{AnchorStatus.UNCERTAIN.value}"
            ),
            "candidate_events": [
                {
                    "topic": "string",
                    "content": "string",
                    "action_label": "string",
                    "object_hint": "string",
                    "retention_reason": "deliverable_updated | decision_made | issue_or_risk_found | follow_up_assigned | external_business_progress | substantive_approval",
                    "retention_detail": "string",
                    "referenced_link_ids": ["message_id#link1"],
                    "source_message_ids": ["message_id"],
                }
            ],
            "context_requests": [
                {
                    "request_type": "earlier_messages | later_messages | attachment_text | linked_file_text",
                    "target_message_ids": ["message_id"],
                    "target_attachment_ids": ["attachment_id"],
                    "target_link_ids": ["message_id#link1"],
                }
            ],
            "needs_cross_anchor_merge": True,
        },
        "input": {
            "target_date": target_date,
            "pass_index": pass_index,
            "anchor_unit": serialize_anchor_unit_for_prompt(anchor_unit, runtime_config),
            "previous_analysis": serialize_anchor_analysis_result_for_prompt(previous_result),
            "expansion": {
                "trigger_requests": [
                    serialize_context_request_for_prompt(item) for item in trigger_requests
                ],
                "new_messages": [
                    serialize_message_for_prompt(message, runtime_config)
                    for message in new_messages
                ],
                "attachment_texts": [
                    serialize_attachment_for_prompt(block, runtime_config)
                    for block in attachment_texts
                ],
                "linked_file_texts": [
                    serialize_linked_file_text_for_prompt(block, runtime_config)
                    for block in (linked_file_texts or [])
                ],
            },
        },
    }
    return dump_json(protocol, pretty=True)


def serialize_batch_for_prompt(
    batch: AnalysisBatch,
    *,
    config: RuntimeConfig | None = None,
) -> dict[str, object]:
    runtime_config = config or RuntimeConfig()
    return {
        "target_date": batch.target_date,
        "batch_id": batch.batch_id,
        "retry_round": batch.retry_round,
        "estimated_tokens": batch.estimated_tokens,
        "self": {
            "open_id": batch.self_open_id,
            "display_name": batch.self_display_name,
        },
        "slices": [
            serialize_slice_for_prompt(conversation_slice, runtime_config)
            for conversation_slice in batch.slices
        ],
    }


def serialize_slice_for_prompt(
    conversation_slice: ConversationSlice,
    config: RuntimeConfig,
) -> dict[str, object]:
    serialized_messages = _serialize_prompt_messages(
        conversation_slice,
        conversation_slice.messages,
        config,
    )
    serialized: dict[str, object] = {
        "slice_id": conversation_slice.slice_id,
        "conversation_id": conversation_slice.conversation_id,
        "messages": serialized_messages,
    }
    if conversation_slice.conversation_name:
        serialized["conversation_name"] = conversation_slice.conversation_name
    if conversation_slice.attachment_texts:
        serialized["attachment_texts"] = [
            serialize_attachment_for_prompt(block, config)
            for block in conversation_slice.attachment_texts
        ]
    if conversation_slice.linked_file_texts:
        serialized["linked_file_texts"] = [
            serialize_linked_file_text_for_prompt(block, config)
            for block in conversation_slice.linked_file_texts
        ]
    return serialized


def _serialize_segment_unit_for_prompt(
    unit: ConversationSegmentUnit,
    config: RuntimeConfig,
) -> dict[str, object]:
    message_lookup = {message.message_id: message for message in unit.messages}
    primary_ids = set(unit.primary_message_ids)
    context_ids = set(unit.context_message_ids)
    messages = [
        serialize_message_for_prompt(message, config, message_lookup=message_lookup)
        | {
            "role": "primary" if message.message_id in primary_ids else "context",
        }
        for message in unit.messages
        if message.message_id in primary_ids | context_ids
    ]
    serialized = {
        "segment_id": unit.segment_id,
        "primary_message_ids": list(unit.primary_message_ids),
        "context_message_ids": list(unit.context_message_ids),
        "self_evidence_message_ids": list(unit.self_evidence_message_ids),
        "response_signals": [item.to_dict() for item in unit.response_signals],
        "messages": messages,
    }
    if unit.attachment_texts:
        serialized["attachment_texts"] = [
            serialize_attachment_for_prompt(item, config)
            for item in unit.attachment_texts
        ]
    if unit.linked_file_texts:
        serialized["linked_file_texts"] = [
            serialize_linked_file_text_for_prompt(item, config)
            for item in unit.linked_file_texts
        ]
    return serialized


def serialize_anchor_unit_for_prompt(
    anchor_unit: AnchorUnit,
    config: RuntimeConfig,
) -> dict[str, object]:
    serialized_messages = _serialize_anchor_prompt_messages(
        anchor_unit,
        anchor_unit.messages,
        config,
    )
    serialized: dict[str, object] = {
        "messages": serialized_messages,
    }
    if anchor_unit.conversation_name:
        serialized["conversation_name"] = anchor_unit.conversation_name
    if anchor_unit.attachment_refs:
        serialized["attachment_refs"] = [
            {
                "id": item.attachment_id,
                "name": item.file_name,
                "mime": item.mime_type,
            }
            for item in anchor_unit.attachment_refs
        ]
    if anchor_unit.anchor_signals:
        serialized["anchor_signals"] = [item.to_dict() for item in anchor_unit.anchor_signals]
    if anchor_unit.attachment_texts:
        serialized["attachment_texts"] = [
            serialize_attachment_for_prompt(item, config)
            for item in anchor_unit.attachment_texts
        ]
    if anchor_unit.linked_file_texts:
        serialized["linked_file_texts"] = [
            serialize_linked_file_text_for_prompt(item, config)
            for item in anchor_unit.linked_file_texts
        ]
    return serialized


def serialize_anchor_analysis_result_for_prompt(
    result: AnchorAnalysisResult,
) -> dict[str, object]:
    return {
        "anchor_status": result.anchor_status,
        "candidate_events": [item.to_dict() for item in result.candidate_events],
        "context_requests": [
            serialize_context_request_for_prompt(item) for item in result.context_requests
        ],
        "needs_cross_anchor_merge": result.needs_cross_anchor_merge,
    }


def serialize_cross_merge_candidate_for_prompt(
    candidate: SourceBackedEventDraft,
) -> dict[str, object]:
    return {
        "id": candidate.draft_id,
        "t": candidate.topic,
        "c": candidate.content,
    }


def _serialize_collected_source_event_for_prompt(
    source_event: CollectedSourceEvent,
) -> dict[str, object]:
    return {
        "draft_id": source_event.draft_id,
        "person": source_event.person_name,
        "source_file": source_event.source_file,
        "is_merge_owner_source": source_event.is_merge_owner_source,
        "event_id": source_event.event.event_id,
        "source_people": list(source_event.event.source_people),
        "source_event_ids": list(source_event.event.source_event_ids),
        "title": source_event.event.title,
        "content": source_event.event.content,
        "object_hint": source_event.event.object_hint,
        "retention_reason": source_event.event.retention_reason,
        "retention_detail": source_event.event.retention_detail,
        "file_links": [
            {
                "title": item.title,
                "url": item.url,
            }
            for item in source_event.event.file_links
        ],
    }


def serialize_context_request_for_prompt(request: ContextRequest) -> dict[str, object]:
    return {
        "slice_id": request.slice_id,
        "request_type": request.request_type,
        "target_message_ids": list(request.target_message_ids),
        "target_attachment_ids": list(request.target_attachment_ids),
        "target_link_ids": list(request.target_link_ids),
        "reason": request.reason,
        "limit": request.limit,
    }


def serialize_message_for_prompt(
    message: NormalizedMessage,
    config: RuntimeConfig,
    *,
    message_lookup: dict[str, NormalizedMessage] | None = None,
) -> dict[str, object]:
    compressed_text = _trim_text(
        _compress_prompt_message_text(message),
        config.prompt_message_char_limit,
    )
    serialized: dict[str, object] = {
        "id": message.message_id,
        "t": _format_prompt_time(message.send_time, config),
        "s": message.sender_name or message.sender_open_id or "",
        "x": compressed_text,
    }
    attachments = _serialize_message_attachments(message)
    if attachments:
        serialized["attachments"] = attachments
    reactions = _serialize_message_reactions(message)
    if reactions:
        serialized["reactions"] = reactions
    links = [
        {
            "link_id": item.link_id,
            "message_id": item.message_id,
            "url": item.url,
            "title": item.title,
            "link_type": item.link_type,
        }
        for item in build_message_link_candidates(message)
    ]
    if links:
        serialized["links"] = links
    if message_lookup is not None:
        reply_to = _serialize_relation_summary(
            message.reply_to_message_id,
            message_lookup=message_lookup,
            config=config,
        )
        if reply_to:
            serialized["reply_to"] = reply_to
        quote_to = _serialize_relation_summary(
            message.quote_message_id,
            message_lookup=message_lookup,
            config=config,
        )
        if quote_to:
            serialized["quote_to"] = quote_to
    return serialized


def _serialize_message_reactions(message: NormalizedMessage) -> list[dict[str, str]]:
    return [
        {
            "emoji_type": reaction.emoji_type,
            "name": reaction.emoji_name,
            "description": reaction.emoji_description,
            "semantic": reaction.semantic,
        }
        for reaction in message.reactions
        if reaction.emoji_type
    ]


def _serialize_prompt_messages(
    conversation_slice: ConversationSlice,
    messages: list[NormalizedMessage],
    config: RuntimeConfig,
) -> list[dict[str, object]]:
    message_lookup = {message.message_id: message for message in conversation_slice.messages}
    serialized_messages: list[dict[str, object]] = []
    for message in messages:
        serialized = serialize_message_for_prompt(
            message,
            config,
            message_lookup=message_lookup,
        )
        if _is_prompt_message_meaningful(serialized):
            serialized_messages.append(serialized)
    return serialized_messages


def _serialize_anchor_prompt_messages(
    anchor_unit: AnchorUnit,
    messages: list[NormalizedMessage],
    config: RuntimeConfig,
) -> list[dict[str, object]]:
    anchor_ids = set(anchor_unit.anchor_message_ids)
    message_lookup = {message.message_id: message for message in anchor_unit.messages}
    serialized_messages: list[dict[str, object]] = []
    for message in messages:
        serialized = serialize_message_for_prompt(
            message,
            config,
            message_lookup=message_lookup,
        )
        if not _is_prompt_message_meaningful(serialized):
            continue
        if _is_non_anchor_weak_placeholder(message, serialized, anchor_ids):
            continue
        serialized_messages.append(serialized)
    return serialized_messages


def _is_prompt_message_meaningful(message: dict[str, object]) -> bool:
    text = clean_text(str(message.get("x", "")))
    if not text:
        return bool(message.get("attachments")) or bool(message.get("links"))
    if text == "[Sticker]":
        return False
    return True


def _is_non_anchor_weak_placeholder(
    message: NormalizedMessage,
    serialized: dict[str, object],
    anchor_ids: set[str],
) -> bool:
    if message.message_id in anchor_ids:
        return False
    if serialized.get("attachments") or serialized.get("links"):
        return False
    text = clean_text(str(serialized.get("x", "")))
    return text in {
        "[图片]",
        "[链接]",
        "[飞书文档]",
        "[表单链接]",
    } or text.startswith("[文件: ")


def serialize_attachment_for_prompt(
    block: AttachmentTextBlock,
    config: RuntimeConfig,
) -> dict[str, object]:
    return {
        "id": block.attachment_id,
        "mid": block.message_id,
        "name": block.file_name,
        "text": _trim_text(block.text, config.prompt_attachment_char_limit),
    }


def serialize_linked_file_text_for_prompt(
    block: LinkedFileTextBlock,
    config: RuntimeConfig,
) -> dict[str, object]:
    return {
        "link_id": block.link_id,
        "mid": block.message_id,
        "title": block.title,
        "url": block.url,
        "text": _trim_text(block.text, config.prompt_attachment_char_limit),
    }


def _serialize_message_attachments(message: NormalizedMessage) -> list[dict[str, str]]:
    return [
        {
            "attachment_id": item.attachment_id,
            "file_name": item.file_name,
            "mime_type": item.mime_type,
        }
        for item in message.attachments
    ]


def _serialize_relation_summary(
    target_message_id: str | None,
    *,
    message_lookup: dict[str, NormalizedMessage],
    config: RuntimeConfig,
) -> dict[str, object] | None:
    if not target_message_id:
        return None
    target = message_lookup.get(target_message_id)
    if target is None:
        return {
            "message_id": target_message_id,
            "sender": "",
            "text": "",
            "attachments": [],
            "links": [],
        }
    return {
        "message_id": target.message_id,
        "sender": target.sender_name or target.sender_open_id or "",
        "text": _trim_text(
            _compress_prompt_message_text(target),
            min(config.prompt_message_char_limit, 120),
        ),
        "attachments": _serialize_message_attachments(target),
        "links": [
            {
                "link_id": item.link_id,
                "message_id": item.message_id,
                "url": item.url,
                "title": item.title,
                "link_type": item.link_type,
            }
            for item in build_message_link_candidates(target)
        ],
    }


def _trim_text(value: str, limit: int) -> str:
    cleaned = clean_text(value)
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit].rstrip()}..."


def _compress_prompt_message_text(message: NormalizedMessage) -> str:
    feishu_doc_label = _build_feishu_doc_label(message)
    if feishu_doc_label:
        return feishu_doc_label

    text = clean_text(message.text)
    if not text:
        return ""

    file_name = _extract_inline_media_name(text)
    if message.message_type == "file" and file_name:
        return f"[文件: {file_name}]"

    audio_match = _AUDIO_TAG_RE.fullmatch(text)
    if audio_match:
        duration = audio_match.group(1).strip()
        if file_name:
            return f"[语音消息 {duration}: {file_name}]"
        return f"[语音消息 {duration}]"

    video_match = _VIDEO_TAG_RE.fullmatch(text)
    if video_match:
        duration = video_match.group(1).strip()
        return f"[视频 {duration}]"

    if _IMAGE_TAG_RE.fullmatch(text):
        return _build_image_placeholder(message, text)
    if text == "[Sticker]":
        return ""

    if message.message_type == "audio":
        return f"[语音消息: {file_name}]" if file_name else "[语音消息]"
    if message.message_type in {"image", "media"} and not text:
        return f"[媒体消息: {file_name}]" if file_name else "[媒体消息]"
    if message.message_type == "post":
        normalized = _normalize_prompt_text(text)
        normalized_lines = [line for line in normalized.splitlines() if line.strip()]
        deduped_lines: list[str] = []
        seen_image = False
        for line in normalized_lines:
            if line == "[图片]":
                if seen_image:
                    continue
                seen_image = True
            deduped_lines.append(line)
        return "\n".join(deduped_lines)
    return _normalize_prompt_text(text)


def _normalize_prompt_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _AT_TAG_RE.sub(_replace_at_tag, normalized)
    normalized = _MARKDOWN_IMAGE_RE.sub("[图片]", normalized)
    normalized = _MARKDOWN_LINK_RE.sub(_replace_markdown_link, normalized)
    normalized = _URL_RE.sub(_replace_url, normalized)
    normalized = _HTML_TAG_RE.sub("\n", normalized)
    normalized = re.sub(r"<[^>]+>", " ", normalized)
    normalized = normalized.replace("&nbsp;", " ")
    normalized = normalized.replace("&amp;", "&")
    normalized = normalized.replace(" / ", "\n")
    normalized = _EMOJI_TOKEN_RE.sub("", normalized)
    normalized = "\n".join(_WHITESPACE_RE.sub(" ", line).strip() for line in normalized.splitlines())
    normalized = _BLANK_LINE_RE.sub("\n\n", normalized)
    normalized = clean_text(normalized)
    normalized = "\n".join(line for line in normalized.splitlines() if line.strip())
    normalized = clean_text(normalized)
    if not normalized:
        return ""
    if _contains_only_image_placeholders(normalized):
        return "[图片]"
    return normalized


def _replace_at_tag(match: re.Match[str]) -> str:
    label = clean_text(match.group(1) or "")
    if not label:
        return ""
    return f"@{label}"


def _replace_markdown_link(match: re.Match[str]) -> str:
    label = clean_text(match.group(1))
    if label:
        return f"{label}[链接]"
    return "[链接]"


def _replace_url(match: re.Match[str]) -> str:
    url = match.group(0)
    if "feishu.cn/docx/" in url or "larksuite.com/docx/" in url:
        return "[飞书文档]"
    if "feishu.cn/share/base/form/" in url or "larksuite.com/share/base/form/" in url:
        return "[表单链接]"
    return "[链接]"


def _contains_only_image_placeholders(text: str) -> bool:
    compact = clean_text(text.replace("\n", " "))
    if not compact:
        return False
    stripped = compact.replace("[图片]", "").strip()
    return not stripped


def _extract_inline_media_name(text: str) -> str:
    match = re.search(r'name="([^"]+)"', text)
    if not match:
        return ""
    return clean_text(match.group(1))


def _build_image_placeholder(message: NormalizedMessage, text: str) -> str:
    image_count = text.count("[Image:")
    if image_count > 1:
        return f"[图片 {image_count}张]"
    return "[图片]"


def _format_prompt_time(value: str, config: RuntimeConfig) -> str:
    try:
        return datetime.fromisoformat(value).strftime(config.prompt_time_format)
    except ValueError:
        return value


def _build_sensitive_rule(config: RuntimeConfig) -> str:
    if not config.sensitive_event_keywords:
        return ""
    joined = "、".join(config.sensitive_event_keywords)
    return f"涉及{joined}等敏感信息，不要提炼为事项。"


def _build_feishu_doc_label(message: NormalizedMessage) -> str:
    if message.message_type == "file":
        file_name = _extract_inline_media_name(message.text)
        if file_name:
            return f"[文件: {file_name}]"

    text = clean_text(message.text)
    pure_feishu_doc_message = False
    if text:
        normalized_doc_only = _URL_RE.sub("[飞书文档]", text)
        normalized_doc_only = clean_text(normalized_doc_only)
        pure_feishu_doc_message = normalized_doc_only == "[飞书文档]"

    for link in message.links:
        if link.link_type != LinkType.FEISHU_DOC.value:
            continue
        if not pure_feishu_doc_message:
            break
        title = clean_text(link.title)
        if title:
            return f"[飞书文档: {title}]"
        return "[飞书文档]"

    if pure_feishu_doc_message and ("feishu.cn/docx/" in text or "larksuite.com/docx/" in text):
        return "[飞书文档]"

    return ""
