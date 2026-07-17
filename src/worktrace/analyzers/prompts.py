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
    CollectedGroupingGroup,
    CollectedSourceEvent,
    ConversationSegmentUnit,
    ConversationSegmentationResult,
    ConversationSlice,
    ContextRequest,
    LinkedFileTextBlock,
    NormalizedMessage,
    PersonalFactReviewBatch,
    ResponseSignal,
    RetentionReviewBatch,
    SegmentAnalysisBatch,
    SourceBackedEventDraft,
)
from ..utils.hashing import is_sha256_fingerprint
from ..utils.json_io import dump_json
from ..utils.link_refs import build_message_link_candidates
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


RETENTION_COMPLETENESS_RULE = (
    "只有同时具备具体对象、保留理由、保留依据的工作事件才输出；"
    "缺少任一项时，不要输出 candidate_event。"
)

RETENTION_DETAIL_EVIDENCE_RULE = (
    "retention_detail 表示保留依据/来源证据，用一句话写清楚来源会话、"
    "发起人或确认人、关键动作或结论；不要只写泛泛的价值判断，"
    "不要写 message id、open_id、conversation_id 或 om_/ou_/oc_ 等内部标识。"
)

PERSON_NAME_RETENTION_RULE = (
    "人名只在明确责任分工、任务指派或确认沟通对象时保留；"
    "与事件责任和推进无关的人名改写为岗位、角色或相关同事，不要输出参与人名单。"
)

EVENT_TITLE_RULE = (
    "topic 或 title 必须让读者脱离正文也能识别具体事项；"
    "优先采用‘具体对象 + 关键动作、进展、结果或风险’的结构，"
    "保持简洁，不得只写无法区分实际事项的通用类别。"
)

ATTACHMENT_FILE_NAME_RULE = (
    "附件元数据中的 file_name 仅用于识别文件：当消息明确是在发送、查看、审核、"
    "转交或处理该附件时，topic 和 object_hint 必须写明 file_name，并将 attachment_id "
    "填入 referenced_attachment_ids。附件发送后的明确转交、查看或审核指令属于后续任务，"
    "必须以 follow_up_assigned 输出；不得根据文件名推断文件正文事实。"
)


def _build_personal_retention_rules(config: RuntimeConfig) -> list[str]:
    return [
        *config.retention_policy.prompt_rules,
        RETENTION_COMPLETENESS_RULE,
        RETENTION_DETAIL_EVIDENCE_RULE,
        PERSON_NAME_RETENTION_RULE,
        *_build_personal_fact_rules(config),
    ]


def _build_personal_fact_rules(config: RuntimeConfig) -> list[str]:
    risk_definitions = "、".join(
        f"{item.key}={item.description}"
        for item in config.retention_policy.fact_risk_signals
    )
    risk_rule = (
        "fact_risk_flags 只能使用以下配置：" + risk_definitions
        if risk_definitions
        else "fact_risk_flags 没有可用配置时返回空数组。"
    )
    return [
        "fact_items 必须覆盖 topic、content、action_label、object_hint、retention_detail 和非空 workstream_key；每项使用 field、text、evidence_message_ids。",
        "除 content 可拆成多个按顺序连接的事实外，其他非空字段各返回一项，text 必须与对应字段完全一致。",
        "content 必须与全部 content fact_items 的 text 按返回顺序直接连接后完全一致。",
        "每个 fact_item 必须引用 source_message_ids 中直接支持该项文字的真实消息；不能用无关消息为推断或补充内容背书。",
        risk_rule,
        *config.retention_policy.fact_review_rules,
    ]


def _personal_fact_output_shape() -> dict[str, object]:
    return {
        "fact_items": [
            {
                "field": "topic | content | action_label | object_hint | retention_detail | workstream_key",
                "text": "field text",
                "evidence_message_ids": ["message_id"],
            }
        ],
        "fact_risk_flags": ["configured fact risk key"],
    }


def _personal_fact_review_items_shape() -> dict[str, object]:
    single_item = {
        "text": "reviewed field text, or empty",
        "evidence_message_ids": ["message_id"],
    }
    return {
        "topic": single_item,
        "content": [
            {
                "text": "ordered content fragment",
                "evidence_message_ids": ["message_id"],
            }
        ],
        "action_label": single_item,
        "object_hint": single_item,
        "retention_detail": single_item,
        "workstream_key": single_item,
    }


def _build_self_relation_rule(config: RuntimeConfig) -> str:
    options = "、".join(
        f"{item.key}={item.label}" for item in config.self_relation_types
    )
    if not options:
        return "self_relations 无可用配置时返回空数组。"
    return (
        "self_relations 判断本人参与方式，只能使用以下配置："
        f"{options}。每项必须返回 relation 和 evidence_message_ids；"
        "证据必须是当前事件中能直接证明该参与方式的本人消息，不能使用他人消息。"
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
            *_build_personal_retention_rules(runtime_config),
            _build_sensitive_rule(runtime_config),
            "一件事写一条；如果有多件事就拆开。",
            EVENT_TITLE_RULE,
            "content 写完整事项；如果有明确结果，直接融入 content，不要单独返回 result。",
            "action_label 只写主要动作标签，例如：回复、审批、催办、撰写、核对、跟进、同步、确认。",
            "object_hint 只写该事项的核心对象或主题，例如：提前付款、优惠券配置、汇报文档、上海点位签约方案。",
            (
                "retention_reason 必须从以下枚举选择：deliverable_updated、decision_made、"
                "issue_or_risk_found、follow_up_assigned、external_business_progress、substantive_approval。"
            ),
            "每条事项附上最相关的消息 id。",
            "只能使用输入里出现过的真实 message id，不要自造占位符 id。",
            "如需给事项挂涉及文件，只能从对应 source_message_ids 的 links 里选择 referenced_link_ids；拿不准就返回空数组。",
            "self_evidence_message_ids 必须列出证明本人发起、负责、审批或跟进该事项的本人消息；它可以与 source_message_ids 不同。",
            _build_self_relation_rule(runtime_config),
            ATTACHMENT_FILE_NAME_RULE,
            "workstream_key 只在消息明确命名项目、产品或政策时填写其稳定规范名称；不能使用城市、地点、部门、工具类别、环境或泛化主题，无法确定时返回空字符串。",
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
                    "referenced_attachment_ids": ["attachment_id"],
                    "self_evidence_message_ids": ["message_id"],
                    "self_relations": [
                        {
                            "relation": "configured relation key",
                            "evidence_message_ids": ["message_id"],
                        }
                    ],
                    "workstream_key": "string or empty string",
                    "source_message_ids": ["message_id"],
                    **_personal_fact_output_shape(),
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
    attachment_texts: list[AttachmentTextBlock] | None = None,
    config: RuntimeConfig | None = None,
) -> str:
    runtime_config = config or RuntimeConfig()
    message_lookup = {message.message_id: message for message in messages}
    message_refs = _build_segmentation_message_refs(messages)
    signal_refs = _build_segmentation_signal_refs(response_signals)
    attachment_texts_by_message: dict[str, list[AttachmentTextBlock]] = {}
    for block in attachment_texts or []:
        attachment_texts_by_message.setdefault(block.message_id, []).append(block)
    serialized_messages: list[dict[str, object]] = []
    for message in messages:
        serialized = serialize_message_for_prompt(
            message,
            runtime_config,
            message_lookup=message_lookup,
        )
        serialized = _replace_segmentation_message_refs(serialized, message_refs)
        if message_attachment_texts := attachment_texts_by_message.get(message.message_id):
            serialized["attachment_texts"] = [
                serialize_attachment_for_prompt(block, runtime_config)
                for block in message_attachment_texts
            ]
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
            "相邻的本人消息只要明确切换到不同项目、产品、政策、客户或业务对象，就必须开始新轮次；同一发送人、相近时间或没有回复链都不能作为合并理由。",
            "标记 hard_boundary_before 的消息必须出现在 segment_start_message_ids 中。",
            "本人文本和本人表情都是参与信号，不等于同意、完成或结束；必须结合后续沟通判断是否仍为同一话题。",
            "消息内已提供的图片摘要属于该消息的内容，应结合摘要判断是否换题。",
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
            "每条 candidate 必须在 self_evidence_message_ids 中列出本人发起、负责、审批或跟进的消息；事实来源可由他人的执行、反馈或文件消息组成。",
            _build_self_relation_rule(runtime_config),
            "每条 candidate 至少引用一条本人参与证据，或引用该 segment 的本人回应 signal。",
            "一个 candidate 只能描述一条工作线；若同一 segment 同时出现两个命名项目、产品、政策或不相干业务对象，必须拆成多个 candidate_events。",
            ATTACHMENT_FILE_NAME_RULE,
            "图片和文件附件默认只提供元数据；判断依赖其内容时，必须返回 attachment_text 的 context_requests，并给出对应消息和附件 ID，不要猜测图片或文件内容。",
            "workstream_key 只在消息明确命名项目、产品或政策时填写其稳定规范名称；不能使用城市、地点、部门、工具类别、环境或泛化主题，无法确定时返回空字符串。",
            "本人提出的问题、风险和待确认事项本身可以提炼，不要求已有处理结果。",
            EVENT_TITLE_RULE,
            "表情是本人回复证据，但不能单凭表情描述事项已完成、已同意或已拒绝。",
            *_build_personal_retention_rules(runtime_config),
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
                                "referenced_attachment_ids": ["attachment_id"],
                                "self_evidence_message_ids": ["message_id"],
                                "self_relations": [
                                    {
                                        "relation": "configured relation key",
                                        "evidence_message_ids": ["message_id"],
                                    }
                                ],
                                "workstream_key": "string or empty string",
                                "source_message_ids": ["primary_message_id"],
                                **_personal_fact_output_shape(),
                            }
                        ],
                        "context_requests": [],
                    },
                }
            ]
        },
    }
    return dump_json(protocol, pretty=True)


def build_retention_review_prompt(
    batch: RetentionReviewBatch,
    *,
    config: RuntimeConfig | None = None,
) -> str:
    runtime_config = config or RuntimeConfig()
    policy = runtime_config.retention_policy
    routine_signal_types = {
        item.key: item.description for item in policy.routine_signals
    }
    substantive_signal_types = {
        item.key: item.description for item in policy.substantive_signals
    }
    protocol = {
        "instruction": (
            "复核边界工作事件对应的原聊天，只判断聊天中存在的语义信号。"
            "必须返回每个 draft_id 的 routine_signals 和 substantive_signals。"
            "不要决定保留或删除，不要计算数量，不要添加解释。"
        ),
        "rules": [
            "candidate_summary 仅用于定位候选，不能作为语义证据。",
            "只能根据 messages 判断信号，context 消息仅用于理解，不能作为 evidence_message_ids。",
            "每个信号必须引用 allowed_evidence_message_ids 中真实存在的消息。",
            "没有对应信号时返回空数组，不要为了填满字段推断或编造。",
            "临时协作和实质工作可能同时存在；存在时必须分别返回，不能互相覆盖。",
            "每个输入 draft_id 必须且只能返回一次，不得遗漏、重复或增加。",
        ],
        "signal_definitions": {
            "routine_signals": routine_signal_types,
            "substantive_signals": substantive_signal_types,
        },
        "required_output_schema": {
            "results": [
                {
                    "draft_id": "draft_id",
                    "routine_signals": [
                        {
                            "type": "configured routine signal type",
                            "evidence_message_ids": ["message_id"],
                        }
                    ],
                    "substantive_signals": [
                        {
                            "type": "configured substantive signal type",
                            "evidence_message_ids": ["message_id"],
                        }
                    ],
                }
            ]
        },
        "input": {
            "target_date": batch.target_date,
            "batch_id": batch.batch_id,
            "candidates": [
                {
                    "draft_id": item.candidate.draft_id,
                    "candidate_summary": {
                        "topic": item.candidate.topic,
                        "content": item.candidate.content,
                        "action_label": item.candidate.action_label,
                        "object_hint": item.candidate.object_hint,
                        "retention_reason": item.candidate.retention_reason,
                        "retention_detail": item.candidate.retention_detail,
                    },
                    "allowed_evidence_message_ids": list(
                        item.allowed_evidence_message_ids
                    ),
                    "messages": [
                        serialize_message_for_prompt(message, runtime_config)
                        | {
                            "role": (
                                "evidence"
                                if message.message_id
                                in set(item.allowed_evidence_message_ids)
                                else "context"
                            )
                        }
                        for message in item.messages
                    ],
                }
                for item in batch.candidates
            ],
        },
    }
    return dump_json(protocol, pretty=True)


def build_personal_fact_review_prompt(
    batch: PersonalFactReviewBatch,
    *,
    config: RuntimeConfig | None = None,
) -> str:
    runtime_config = config or RuntimeConfig()
    policy = runtime_config.retention_policy
    fact_risk_types = {
        item.key: item.description for item in policy.fact_risk_signals
    }
    protocol = {
        "instruction": (
            "复核个人工作事件的每项事实是否得到原聊天支持。"
            "候选摘要只用于定位问题，messages 才是事实来源。"
            "确认后可以保持原文，也可以删除或改写无证据内容。"
            "不要计算数量，不要添加解释。"
        ),
        "rules": [
            "每个输入 draft_id 必须且只能返回一次，并保持输入顺序。",
            "只能使用 role=evidence 且属于 allowed_evidence_message_ids 的消息作为事实证据；context 只能帮助理解。",
            "allowed_evidence_message_ids 按候选独立生效；即使同批其他候选中出现了某个消息 ID，也不能把它用于当前候选。",
            "supported=true 时，只在 fact_items 中返回原聊天直接支持的 topic、content、action_label、object_hint、retention_detail 和 workstream_key；不要在 fact_items 之外重复返回这些文字字段。",
            "supported=true 时，topic、content、object_hint 和 retention_detail 必须都有非空文字和至少一个合法 evidence_message_id；缺少任一必填字段的合法证据时必须返回 supported=false。",
            "supported=true 时，topic、action_label、object_hint、retention_detail 以及非空 workstream_key 各返回且只返回一个对象；workstream_key 为空时返回空 text 和空 evidence_message_ids。",
            "content 可以拆成一个或多个对象，但必须按最终正文顺序返回；Python 会直接连接所有 content.text 生成正文，包括标点，不能把其他字段的文字放入 content。",
            "supported=false 表示原聊天无法支持一条同时具备 topic、content、object_hint 和 retention_detail 的有效事件；此时 fact_items 中各文字字段和证据必须为空，content 返回空数组，removed_claims 至少写一项。",
            "不要因为事件包含多人、多地点、多步骤或较长聊天就返回 supported=false。",
            "fact_items 的字段覆盖、文字一致性和消息证据要求与首次提炼相同。",
            "removed_claims 只写被删除或改写的原候选表述，不要写内部消息 ID。",
            *policy.fact_review_rules,
        ],
        "retry_feedback": batch.retry_feedback,
        "risk_signal_definitions": fact_risk_types,
        "required_output_schema": {
            "results": [
                {
                    "draft_id": "draft_id",
                    "supported": "boolean",
                    "fact_items": _personal_fact_review_items_shape(),
                    "removed_claims": ["removed or revised claim"],
                }
            ]
        },
        "input": {
            "target_date": batch.target_date,
            "batch_id": batch.batch_id,
            "candidates": [
                {
                    "draft_id": item.candidate.draft_id,
                    "review_reasons": list(item.review_reasons),
                    "candidate_summary": {
                        "topic": item.candidate.topic,
                        "content": item.candidate.content,
                        "action_label": item.candidate.action_label,
                        "object_hint": item.candidate.object_hint,
                        "retention_reason": item.candidate.retention_reason,
                        "retention_detail": item.candidate.retention_detail,
                        "workstream_key": item.candidate.workstream_key,
                        "fact_items": [
                            fact.to_dict() for fact in item.candidate.fact_items
                        ],
                        "fact_risk_flags": list(
                            item.candidate.fact_risk_flags
                        ),
                    },
                    "allowed_evidence_message_ids": list(
                        item.allowed_evidence_message_ids
                    ),
                    "messages": [
                        serialize_message_for_prompt(message, runtime_config)
                        | {
                            "role": (
                                "evidence"
                                if message.message_id
                                in set(item.allowed_evidence_message_ids)
                                else "context"
                            )
                        }
                        for message in item.messages
                    ],
                }
                for item in batch.candidates
            ],
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
            "只把明显属于同一真实事件或同一项目生命周期的事项分到一起。",
            "如果拿不准，宁可分开。",
            "背景相同不等于同一事件。",
            "不同的非空 workstream_key 必须分开，即使消息相邻、同一发送人或共享地点、设备也不能合并。",
            "若多个候选具有相同的非空 workstream_key，必须合并为同一项目或政策生命周期；项目根候选作为 primary_draft_id。",
            "workstream_key 为空的候选只能在某个非空工作流候选明确分配了同一具体对象或动作时并入该组；否则必须单独成组。",
            "同一政策的适用范围/通知指令与其直接执行反馈应合并为一条闭环事件；范围和执行对象不一致时必须分开。",
            "不能以城市、地点、部门、通用工具或相近时间作为合并理由。",
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
                    "primary_draft_id": "draft_id",
                }
            ]
        },
        "target_date": target_date,
        "candidates": [
            {
                "draft_id": candidate.draft_id,
                "action_label": candidate.action_label,
                "object_hint": candidate.object_hint,
                "workstream_key": candidate.workstream_key,
                "source_conversation_id": candidate.source_conversation_id,
                "topic": candidate.topic,
                "content": candidate.content,
            }
            for candidate in candidates
        ],
    }
    return dump_json(protocol, pretty=True)


def build_workstream_assignment_prompt(
    target_date: str,
    candidates: list[SourceBackedEventDraft],
) -> str:
    protocol = {
        "instruction": (
            "判断当天候选事项是否属于某个明确命名的项目、产品或政策工作流。"
            "只返回 JSON assignments，不展示推理过程。"
        ),
        "rules": [
            "每个 draft_id 必须且只能返回一次。",
            "parent_draft_id 填该事项的直接项目父候选 draft_id；独立事项返回空字符串。",
            "一个明确命名的项目、产品或政策根候选的 parent_draft_id 填自己的 draft_id，并在 root_workstream_name 填输入中明确出现的名称；其余事项的 root_workstream_name 返回空字符串。",
            "只有候选内容明确表明项目启动时分配了该任务、该任务是该项目的实施/验收/监控/成效统计，或该政策的范围指令与直接执行反馈构成同一闭环时，才能归入同一项目根候选。",
            "不同命名项目、产品或政策不得归并；共享城市、地点、部门、设备、通用工具、相近时间或相似措辞都不是依据。",
            "不确定时返回独立事项，绝不猜测。",
            "每个非独立、非根归属必须在 evidence_message_ids 中引用至少一条子事项或直接父候选的 source_message_ids；直接父候选本身及其 source_message_ids 共同构成另一侧证据，不能使用输入外的消息 ID。",
            "同一政策的适用范围指令与其直接执行/通知反馈，即使描述的是不同对象子集，只要输入没有明确命名不同政策，应归入同一政策工作流。",
            "不同写法是否为同一命名项目由消息语义判断，不能仅凭字符串相似度判断。",
        ],
        "required_output_schema": {
            "assignments": [
                {
                    "draft_id": "draft_id",
                    "parent_draft_id": "draft_id or empty string",
                    "root_workstream_name": "string or empty string",
                    "evidence_message_ids": ["message_id"],
                }
            ]
        },
        "target_date": target_date,
        "candidates": [
            {
                "draft_id": candidate.draft_id,
                "topic": candidate.topic,
                "content": candidate.content,
                "object_hint": candidate.object_hint,
                "workstream_key": candidate.workstream_key,
                "source_message_ids": candidate.source_message_ids,
                "self_evidence_message_ids": candidate.self_evidence_message_ids,
                "retention_detail": candidate.retention_detail,
            }
            for candidate in candidates
        ],
    }
    return dump_json(protocol, pretty=True)


def build_unassigned_workstream_assignment_prompt(
    target_date: str,
    *,
    known_workstreams: list[dict[str, object]],
    unassigned_candidates: list[SourceBackedEventDraft],
) -> str:
    protocol = {
        "instruction": (
            "复核尚未归属的候选事项是否属于已确认的项目、产品或政策工作流。"
            "只返回 JSON assignments，不展示推理过程。"
        ),
        "rules": [
            "只处理 unassigned_candidates 中的 draft_id；每个必须且只能返回一次。",
            "parent_draft_id 只能填 known_workstreams.members 中的 draft_id；无法明确归属时返回空字符串。",
            "不能新建项目根，也不能合并不同 known_workstreams。root_workstream_name 必须返回空字符串。",
            "只有候选内容明确表明是该项目的启动分配、实施、验收、监控、成效统计，或是该政策的范围指令与直接执行/通知反馈时，才能归入。",
            "同一政策的适用范围指令与其直接执行/通知反馈，即使描述不同对象子集，只要输入没有明确命名不同政策，应归入同一政策工作流。",
            "城市、地点、部门、设备、工具、时间相近或文字相似都不是归属依据；不确定时必须独立。",
            "每个非独立归属必须在 evidence_message_ids 中引用至少一条子事项或直接父候选的 source_message_ids，不能使用输入外的消息 ID。",
        ],
        "required_output_schema": {
            "assignments": [
                {
                    "draft_id": "unassigned draft_id",
                    "parent_draft_id": "known member draft_id or empty string",
                    "root_workstream_name": "empty string",
                    "evidence_message_ids": ["message_id"],
                }
            ]
        },
        "target_date": target_date,
        "known_workstreams": known_workstreams,
        "unassigned_candidates": [
            {
                "draft_id": candidate.draft_id,
                "topic": candidate.topic,
                "content": candidate.content,
                "object_hint": candidate.object_hint,
                "source_message_ids": candidate.source_message_ids,
                "retention_detail": candidate.retention_detail,
            }
            for candidate in unassigned_candidates
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
            EVENT_TITLE_RULE,
            "content 要整合所有来源中不冲突的事实、动作、结果、风险和待办，不按人员逐条展示贡献。",
            "不同员工可能从不同视角描述同一事件；不要编造输入中没有的信息，也不能丢失任何一方提供的有效补充。",
            "是否属于同一真实事件仍由你判断，不要依赖 Python 预先给出同题结论。",
            (
                "只有不同来源对版本号、结论、进展、结果或待办指向存在明确冲突时，"
                "才以 is_merge_owner_source=true 的来源为准，并将 merge_owner_conflict 设为 true，"
                "conflict_detail 简要说明冲突；没有明确冲突时这两个字段分别返回 false 和空字符串。"
            ),
            (
                "反例：普通员工写 WorkTrace 技能升级到 1.0.4，"
                "合并人来源写升级到 1.0.5；如果你判断它们属于同一真实事件，"
                "最终 group 必须以 1.0.5 为主事实，不能改回 1.0.4，并标记存在冲突。"
            ),
            "两条事件都有非空 workstream_name 且名称不同，禁止放入同一 group。",
            "workstream_name 相同只表示可能属于同一工作范围，不能据此直接合并。",
            (
                "evidence_relations 由 Python 对待判断事件的消息指纹和文件指纹完成集合比较后生成；"
                "shared_message_count、shared_file_count 是共同项数量，"
                "message_sets_equal、file_sets_equal 仅在双方对应集合非空且完全相同时为 true。"
            ),
            (
                "evidence_relations、相同具体对象或 action_labels 构成连续动作时，是同一事件的强证据，"
                "仍需结合内容确认；即使指纹集合完全相同，也不能仅据此合并。"
            ),
            "workstream_name 为空的事件，只有 evidence_relations 中的共同消息/文件或明确相同业务对象支持时，才能并入已命名工作流。",
            "只有标题相似、时间接近或部门相同，不能作为合并依据。",
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
                    "merge_owner_conflict": "boolean",
                    "conflict_detail": "string or empty string",
                }
            ]
        },
        "target_date": target_date,
        "merge_owner_person": merge_owner_person,
        "deterministic_groups": deterministic_groups,
        "evidence_relations": _build_collected_evidence_relations(
            events,
            excluded_draft_ids=deterministic_ids,
        ),
        "remaining_events": [
            _serialize_collected_source_event_for_prompt(item, runtime_config)
            for item in events
            if item.draft_id not in deterministic_ids
        ],
        "deterministic_group_events": [
            [
                _serialize_collected_source_event_for_prompt(item, runtime_config)
                for item in events
                if item.draft_id in set(group)
            ]
            for group in deterministic_groups
        ],
    }
    return dump_json(protocol, pretty=True)


def build_collected_grouping_prompt(
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
    protocol = {
        "instruction": (
            "先判断多人 WorkTrace 事件中哪些属于同一真实事项。"
            "返回 groups、draft_ids 和供跨批判断使用的候选摘要，不生成正式汇总正文。"
            "请直接返回 JSON，不要展示推理过程。"
        ),
        "rules": [
            "每个输入 draft_id 必须且只能出现在一个 group。",
            "deterministic_groups 必须原样保留，不能拆分或加入其他组。",
            "conversation_groups 表示事件来自同一天同一飞书会话，只是候选关系，不代表必须合并。",
            "同一大群中的不同真实事项必须分开。",
            "共同消息、共同文件、相同具体对象、连续动作或内容明确一致可支持合并。",
            "不同员工或部门从不同视角描述同一真实事项时可以合并。",
            "拿不准是否同一事项时必须分开。",
            "不同非空工作流名称通常应分开；只有共享会话或共同消息且内容明确一致时才可合并。",
            (
                "多条记录组成的组必须返回非空 summary_title、summary_content 和 "
                "summary_object_hint；摘要应整合具体对象、动作、进展、结果、风险、"
                "待办和明确冲突，不得按人员逐条罗列或补充来源中没有的事实。"
            ),
            "只有一条记录的组将三个 summary 字段返回空字符串，由 Python 保留原事件。",
            "group_reason 只返回实际成立的共同消息、共同文件、同日会话、相同对象或连续动作依据。",
            "risk_flags 标记跨批、工作流冲突、对象过宽或来源很多等需要复核的风险。",
            "来源负责人相同不能单独作为合并依据。",
        ],
        "required_output_schema": {
            "groups": [
                {
                    "group_id": "string",
                    "draft_ids": ["draft_id"],
                    "summary_title": "string or empty for singleton",
                    "summary_content": "string or empty for singleton",
                    "summary_object_hint": "string or empty for singleton",
                    "group_reason": [
                        "shared_message | shared_file | same_conversation | same_object | continuous_action"
                    ],
                    "risk_flags": [
                        "cross_batch | workstream_conflict | broad_object | large_group"
                    ],
                }
            ]
        },
        "target_date": target_date,
        "deterministic_groups": deterministic_groups,
        "conversation_groups": _build_collected_conversation_groups(
            events,
            excluded_draft_ids=deterministic_ids,
        ),
        "evidence_relations": _build_collected_evidence_relations(
            events,
            excluded_draft_ids=deterministic_ids,
        ),
        "events": [
            {
                "draft_id": item.draft_id,
                "person": item.person_name,
                "source_people": list(item.event.source_people),
                "source_report_owners": list(
                    dict.fromkeys(
                        [
                            *item.event.source_report_owners,
                            *([item.source_report_owner] if item.source_report_owner else []),
                        ]
                    )
                ),
                "title": item.event.title,
                "content": clean_text(item.event.content),
                "object_hint": item.event.object_hint,
                "workstream_name": item.event.workstream_name,
                "action_labels": list(item.event.action_labels),
            }
            for item in events
        ],
    }
    return dump_json(protocol, pretty=True)


def build_collected_review_prompt(
    target_date: str,
    events: list[CollectedSourceEvent],
    candidate_group: CollectedGroupingGroup,
    *,
    config: RuntimeConfig | None = None,
    review_reasons: list[str] | None = None,
) -> str:
    runtime_config = config or RuntimeConfig()
    computed_review_reasons: list[str] = []
    if len(candidate_group.draft_ids) >= runtime_config.high_risk_source_event_count:
        computed_review_reasons.append("source_event_count")
    if (
        len({item.source_file for item in events})
        >= runtime_config.high_risk_source_file_count
    ):
        computed_review_reasons.append("source_file_count")
    if (
        runtime_config.review_cross_batch_groups
        and "cross_batch" in candidate_group.risk_flags
    ):
        computed_review_reasons.append("cross_batch")
    if runtime_config.review_repaired_groups and candidate_group.was_repaired:
        computed_review_reasons.append("repaired_group")
    workstreams = {
        "".join(item.event.workstream_name.casefold().split())
        for item in events
        if item.event.workstream_name.strip()
    }
    if runtime_config.review_workstream_conflicts and len(workstreams) > 1:
        computed_review_reasons.append("workstream_conflict")
    protocol = {
        "instruction": (
            "复核一个高风险多人事件候选组是否混入了不同真实事项。"
            "可以保留原组，也可以拆成多个子组。返回 groups，不生成正式汇总正文。"
            "请直接返回 JSON，不要展示推理过程。"
        ),
        "rules": [
            "所有输入 draft_id 必须且只能出现在一个输出 group。",
            "确认属于同一真实事项时保留在同一组；拿不准时拆开。",
            "同一会话、同一负责人、标题相似或部门相同都不能单独证明是同一事项。",
            "不同业务对象、不同主要动作或不同结果方向应拆开。",
            "不同非空工作流通常拆开，除非共享消息证据且内容明确一致。",
            "多成员组返回非空候选摘要；单成员组三个摘要字段返回空字符串。",
            "group_reason 和 risk_flags 按实际情况返回。",
        ],
        "required_output_schema": {
            "groups": [
                {
                    "group_id": "string",
                    "draft_ids": ["draft_id"],
                    "summary_title": "string or empty for singleton",
                    "summary_content": "string or empty for singleton",
                    "summary_object_hint": "string or empty for singleton",
                    "group_reason": [
                        "shared_message | shared_file | same_conversation | same_object | continuous_action"
                    ],
                    "risk_flags": [
                        "cross_batch | workstream_conflict | broad_object | large_group"
                    ],
                }
            ]
        },
        "target_date": target_date,
        "review_reasons": list(
            dict.fromkeys(
                computed_review_reasons
                if review_reasons is None
                else review_reasons
            )
        ),
        "candidate_group": {
            "group_id": candidate_group.group_id,
            "draft_ids": list(candidate_group.draft_ids),
            "summary_title": candidate_group.summary_title,
            "summary_object_hint": candidate_group.summary_object_hint,
            "group_reason": list(candidate_group.group_reason),
            "risk_flags": list(candidate_group.risk_flags),
            "was_repaired": candidate_group.was_repaired,
        },
        "conversation_groups": _build_collected_conversation_groups(
            events,
            excluded_draft_ids=set(),
        ),
        "evidence_relations": _build_collected_evidence_relations(
            events,
            excluded_draft_ids=set(),
        ),
        "events": [
            {
                "draft_id": item.draft_id,
                "person": item.person_name,
                "source_people": list(item.event.source_people),
                "source_report_owners": list(
                    dict.fromkeys(
                        [
                            *item.event.source_report_owners,
                            *(
                                [item.source_report_owner]
                                if item.source_report_owner
                                else []
                            ),
                        ]
                    )
                ),
                "title": item.event.title,
                "content": clean_text(item.event.content),
                "object_hint": item.event.object_hint,
                "workstream_name": item.event.workstream_name,
                "action_labels": list(item.event.action_labels),
            }
            for item in events
        ],
        "config_context": {
            "max_model_input_tokens": runtime_config.max_model_input_tokens,
        },
    }
    return dump_json(protocol, pretty=True)


def build_collected_render_prompt(
    target_date: str,
    events: list[CollectedSourceEvent],
    locked_groups: list[list[str]],
    *,
    config: RuntimeConfig | None = None,
) -> str:
    runtime_config = config or RuntimeConfig()
    events_by_id = {item.draft_id: item for item in events}
    protocol = {
        "instruction": (
            "为已经确认属于同一真实事项的多人事件组生成正式汇总内容。"
            "组成员已经锁定，不要重新分组。只返回 JSON groups。"
        ),
        "rules": [
            "每个 locked_group 必须原样返回为一个 group，draft_ids 不得增删或移动。",
            EVENT_TITLE_RULE,
            "content 要整合全部来源中不冲突的事实、动作、结果、风险和待办。",
            "不得按人员逐条罗列，不得编造来源中没有的信息。",
            (
                "只有版本号、结论、状态、结果或待办方向明确冲突时，"
                "才采用 is_merge_owner_source=true 的来源，并标记 merge_owner_conflict。"
            ),
            "必须返回具体 object_hint、合法 retention_reason 和非空 retention_detail。",
            "covered_draft_ids 必须完整列出本组全部 draft_id，不得遗漏或增加。",
            (
                "fact_items 列出正文保留的关键事实；每项 text 必须具体，"
                "source_draft_ids 只能引用支持该事实的本组 draft_id。"
            ),
            _build_sensitive_rule(runtime_config),
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
                    "merge_owner_conflict": "boolean",
                    "conflict_detail": "string or empty string",
                    "covered_draft_ids": ["draft_id"],
                    "fact_items": [
                        {
                            "text": "string",
                            "source_draft_ids": ["draft_id"],
                        }
                    ],
                }
            ]
        },
        "target_date": target_date,
        "locked_groups": [
            {
                "group_id": f"locked-{index:03d}",
                "draft_ids": list(group),
                "events": [
                    _serialize_collected_render_event_for_prompt(
                        events_by_id[draft_id],
                        runtime_config,
                    )
                    for draft_id in group
                    if draft_id in events_by_id
                ],
            }
            for index, group in enumerate(locked_groups, start=1)
        ],
    }
    return dump_json(protocol, pretty=True)


def _serialize_collected_render_event_for_prompt(
    source_event: CollectedSourceEvent,
    config: RuntimeConfig,
) -> dict[str, object]:
    serialized = _serialize_collected_source_event_for_prompt(source_event, config)
    for key in ("source_file", "event_id", "source_event_ids"):
        serialized.pop(key, None)
    return serialized


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
            *_build_personal_retention_rules(runtime_config),
            "每个 candidate_event 只能落在当前 anchor_unit 内。",
            "每个 candidate_event 只表示一个主要动作。",
            EVENT_TITLE_RULE,
            "action_label 只写主要动作标签，例如：回复、审批、催办、撰写、核对、跟进、同步、确认。",
            "object_hint 只写该事项的核心对象或主题。",
            (
                "retention_reason 必须从以下枚举选择：deliverable_updated、decision_made、"
                "issue_or_risk_found、follow_up_assigned、external_business_progress、substantive_approval。"
            ),
            "如需给事项挂涉及文件，只能从对应 source_message_ids 的 links 里选择 referenced_link_ids；拿不准就返回空数组。",
            "self_evidence_message_ids 列出证明本人直接相关的本人消息；事实来源可以是他人的反馈。",
            _build_self_relation_rule(runtime_config),
            ATTACHMENT_FILE_NAME_RULE,
            "workstream_key 只填写消息明确命名的项目、产品或政策；无法确定时返回空字符串。",
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
                "referenced_attachment_ids": ["attachment_id"],
                "self_evidence_message_ids": ["message_id"],
                "self_relations": [
                    {
                        "relation": "configured relation key",
                        "evidence_message_ids": ["message_id"],
                    }
                ],
                "workstream_key": "string or empty string",
                "source_message_ids": ["message_id"],
                **_personal_fact_output_shape(),
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
            *_build_personal_retention_rules(runtime_config),
            "每个 candidate_event 只能留在自己的 anchor_unit 内。",
            "每个 candidate_event 只表示一个主要动作。",
            EVENT_TITLE_RULE,
            "action_label 只写主要动作标签，例如：回复、审批、催办、撰写、核对、跟进、同步、确认。",
            "object_hint 只写该事项的核心对象或主题。",
            (
                "retention_reason 必须从以下枚举选择：deliverable_updated、decision_made、"
                "issue_or_risk_found、follow_up_assigned、external_business_progress、substantive_approval。"
            ),
            "如需给事项挂涉及文件，只能从对应 source_message_ids 的 links 里选择 referenced_link_ids；拿不准就返回空数组。",
            "self_evidence_message_ids 列出证明本人直接相关的本人消息；事实来源可以是他人的反馈。",
            _build_self_relation_rule(runtime_config),
            ATTACHMENT_FILE_NAME_RULE,
            "workstream_key 只填写消息明确命名的项目、产品或政策；无法确定时返回空字符串。",
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
                            "referenced_attachment_ids": ["attachment_id"],
                            "self_evidence_message_ids": ["message_id"],
                            "self_relations": [
                                {
                                    "relation": "configured relation key",
                                    "evidence_message_ids": ["message_id"],
                                }
                            ],
                            "workstream_key": "string or empty string",
                            "source_message_ids": ["message_id"],
                            **_personal_fact_output_shape(),
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
            _build_self_relation_rule(runtime_config),
            "每个 candidate_event 仍然只能表示一个主要动作或工作线索。",
            "每个 candidate_event 必须包含 object_hint、retention_reason 和 retention_detail。",
            EVENT_TITLE_RULE,
            *_build_personal_retention_rules(runtime_config),
            (
                "retention_reason 必须是 deliverable_updated、decision_made、issue_or_risk_found、"
                "follow_up_assigned、external_business_progress 或 substantive_approval。"
            ),
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
                    "referenced_attachment_ids": ["attachment_id"],
                    "self_evidence_message_ids": ["message_id"],
                    "self_relations": [
                        {
                            "relation": "configured relation key",
                            "evidence_message_ids": ["message_id"],
                        }
                    ],
                    "workstream_key": "string or empty string",
                    "source_message_ids": ["message_id"],
                    **_personal_fact_output_shape(),
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
    config: RuntimeConfig,
) -> dict[str, object]:
    return {
        "draft_id": source_event.draft_id,
        "person": source_event.person_name,
        "source_file": source_event.source_file,
        "is_merge_owner_source": source_event.is_merge_owner_source,
        "event_id": source_event.event.event_id,
        "source_people": list(source_event.event.source_people),
        "source_event_ids": list(source_event.event.source_event_ids),
        "source_report_owners": list(
            dict.fromkeys(
                [
                    *source_event.event.source_report_owners,
                    *(
                        [source_event.source_report_owner]
                        if source_event.source_report_owner
                        else []
                    ),
                ]
            )
        ),
        "title": source_event.event.title,
        "content": source_event.event.content,
        "object_hint": source_event.event.object_hint,
        "retention_reason": source_event.event.retention_reason,
        "retention_detail": source_event.event.retention_detail,
        "workstream_name": source_event.event.workstream_name,
        "action_labels": list(source_event.event.action_labels),
        "self_relations": [
            {
                "key": relation,
                "label": next(
                    (
                        item.label
                        for item in config.self_relation_types
                        if item.key == relation
                    ),
                    relation,
                ),
            }
            for relation in source_event.event.self_relations
        ],
        "file_links": [
            {
                "title": item.title,
                "url": item.url,
            }
            for item in source_event.event.file_links
        ],
    }


def _build_collected_evidence_relations(
    events: list[CollectedSourceEvent],
    *,
    excluded_draft_ids: set[str],
) -> list[dict[str, object]]:
    comparable_events = sorted(
        (item for item in events if item.draft_id not in excluded_draft_ids),
        key=lambda item: item.draft_id,
    )
    evidence_sets = {
        item.draft_id: {
            value
            for value in item.event.evidence_fingerprints
            if is_sha256_fingerprint(value)
        }
        for item in comparable_events
    }
    file_sets = {
        item.draft_id: {
            value for value in item.event.file_keys if is_sha256_fingerprint(value)
        }
        for item in comparable_events
    }
    relations: list[dict[str, object]] = []
    for left_index, left in enumerate(comparable_events):
        for right in comparable_events[left_index + 1 :]:
            left_evidence = evidence_sets[left.draft_id]
            right_evidence = evidence_sets[right.draft_id]
            left_files = file_sets[left.draft_id]
            right_files = file_sets[right.draft_id]
            shared_message_count = len(left_evidence & right_evidence)
            shared_file_count = len(left_files & right_files)
            if not shared_message_count and not shared_file_count:
                continue
            relations.append(
                {
                    "draft_ids": [left.draft_id, right.draft_id],
                    "shared_message_count": shared_message_count,
                    "shared_file_count": shared_file_count,
                    "message_sets_equal": bool(left_evidence)
                    and left_evidence == right_evidence,
                    "file_sets_equal": bool(left_files) and left_files == right_files,
                }
            )
    return relations


def _build_collected_conversation_groups(
    events: list[CollectedSourceEvent],
    *,
    excluded_draft_ids: set[str],
) -> list[dict[str, object]]:
    grouped: dict[str, set[str]] = {}
    for item in events:
        if item.draft_id in excluded_draft_ids:
            continue
        for value in item.event.conversation_fingerprints:
            if not is_sha256_fingerprint(value):
                continue
            grouped.setdefault(value, set()).add(item.draft_id)

    member_groups = sorted(
        {
            tuple(sorted(draft_ids))
            for draft_ids in grouped.values()
            if len(draft_ids) > 1
        }
    )
    return [
        {
            "group_id": f"conversation-{index:03d}",
            "draft_ids": list(draft_ids),
        }
        for index, draft_ids in enumerate(member_groups, start=1)
    ]


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
        "[文件附件]",
    }


def serialize_attachment_for_prompt(
    block: AttachmentTextBlock,
    config: RuntimeConfig,
) -> dict[str, object]:
    return {
        "id": block.attachment_id,
        "mid": block.message_id,
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
    if message.message_type == "file":
        return "[文件附件]"

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
        return "[文件附件]"

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
