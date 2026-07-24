from __future__ import annotations

from pathlib import Path

from src.worktrace.analyzers.output_schemas import (
    anchor_output_schema,
    batch_output_schema,
)
from src.worktrace.analyzers.prompts import (
    build_anchor_analysis_prompt,
    build_anchor_batch_analysis_prompt,
    build_anchor_expansion_prompt,
    build_batch_analysis_prompt,
    build_merge_prompt,
    serialize_message_for_prompt,
    serialize_anchor_unit_for_prompt,
    serialize_batch_for_prompt,
)
from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.constants import AnchorStatus, ContextRequestType
from src.worktrace.models import (
    AnalysisBatch,
    AnchorAnalysisResult,
    AnchorUnit,
    AttachmentMeta,
    AttachmentTextBlock,
    ContextRequest,
    ConversationSlice,
    LinkMeta,
    LinkedFileTextBlock,
    NormalizedMessage,
    SourceBackedEventDraft,
)


REPO_RETENTION_POLICY = load_runtime_config_overrides(
    RuntimeConfig(),
    cwd=Path.cwd(),
).retention_policy


def test_prompt_serialization_is_compact(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_slice_message_limit=1,
        prompt_message_char_limit=12,
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="abcdefghijklmnopqrstuvwxyz",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    message_2 = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_2",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:01:00+08:00",
        message_type="text",
        text="abcdefghijklmnopqrstuvwxyz",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    conversation_slice = ConversationSlice(
        slice_id="slice-1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1", "om_2"],
        messages=[message, message_2],
        attachment_texts=[],
    )
    batch = AnalysisBatch(
        target_date="2026-06-22",
        batch_id="batch-001",
        retry_round=0,
        estimated_tokens=123,
        self_open_id="ou_self",
        self_display_name="Me",
        slices=[conversation_slice],
    )

    payload = serialize_batch_for_prompt(batch, config=config)

    assert payload["self"]["open_id"] == "ou_self"
    assert payload["self"]["display_name"] == "Me"
    assert payload["slices"][0]["messages"][0]["x"] == "abcdefghijkl..."
    assert payload["slices"][0]["messages"][0]["id"] == "om_1"
    assert len(payload["slices"][0]["messages"]) == 2
    assert payload["slices"][0]["slice_id"] == "slice-1"
    assert payload["slices"][0]["conversation_id"] == "oc_1"
    assert "anchor_message_ids" not in payload["slices"][0]
    assert "in_day_message_ids" not in payload["slices"][0]


def test_batch_prompt_uses_original_message_ids_and_slim_rules(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_slice_message_limit=3,
        prompt_message_char_limit=50,
        retention_policy=REPO_RETENTION_POLICY,
    )
    messages = [
        NormalizedMessage(
            conversation_id="oc_1",
            conversation_name="项目群",
            message_id="om_1",
            sender_open_id="ou_1",
            sender_name="Alice",
            send_time="2026-06-22T10:00:00+08:00",
            message_type="text",
            text="推进发布",
            reply_to_message_id=None,
            quote_message_id=None,
            links=[],
            attachments=[],
            is_system=False,
        ),
        NormalizedMessage(
            conversation_id="oc_1",
            conversation_name="项目群",
            message_id="om_2",
            sender_open_id="ou_2",
            sender_name="Bob",
            send_time="2026-06-22T10:01:00+08:00",
            message_type="text",
            text="收到",
            reply_to_message_id=None,
            quote_message_id=None,
            links=[],
            attachments=[],
            is_system=False,
        ),
    ]
    conversation_slice = ConversationSlice(
        slice_id="slice-1",
        conversation_id="oc_1",
        conversation_name="",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1", "om_2"],
        messages=messages,
        attachment_texts=[],
    )
    batch = AnalysisBatch(
        target_date="2026-06-22",
        batch_id="conversation-001",
        retry_round=0,
        estimated_tokens=0,
        self_open_id="ou_self",
        self_display_name="Me",
        slices=[conversation_slice],
    )

    prompt = build_batch_analysis_prompt(batch, config=config)

    assert '"self": {' in prompt
    assert '"open_id": "ou_self"' in prompt
    assert '"display_name": "Me"' in prompt
    assert "只提炼与本人直接相关的工作事项。" in prompt
    assert "本人信息见 input.self；只有事项明确由本人发起、本人负责、本人审批、本人催办、本人汇报、本人跟进，或他人明确要求本人推进/处理时，才提炼。" in prompt
    assert "如果事项主体明显是他人的工作、他人的进展、他人的承诺，而本人只是参与了会话或说过别的话，不要提炼。" in prompt
    assert "如果只是同群讨论背景信息、但没有明确落到本人，也不要提炼。" in prompt
    assert "本人参与、回复或被询问，只能证明事项与本人有关" in prompt
    assert "单纯询问人员当前状态、位置或是否可用" in prompt
    assert "咨询内容如果形成明确业务结论、问题风险、交付修改或后续业务动作" in prompt
    assert "私人饭局、约饭、离职告别聚餐、同事口碑评价、人际寒暄，不要提炼为事项。" in prompt
    assert "个人请假、家庭原因、孩子学校证明、个人行程报备，不要提炼为工作事件。" in prompt
    assert "加班、请假、补卡、考勤、调休、外出报备等行政流程审批，不要提炼为工作事项。" in prompt
    assert "泛泛完成审核或审批但没有具体业务对象" in prompt
    assert "只有同时具备具体对象、保留理由、保留依据的工作事件才输出" in prompt
    assert "人名只在明确责任分工、任务指派或确认沟通对象时保留" in prompt
    assert "retention_detail 表示保留依据/来源证据" in prompt
    assert "不要写 message id、open_id、conversation_id 或 om_/ou_/oc_ 等内部标识。" in prompt
    assert "不要只写泛泛的价值判断" in prompt
    assert "follow_up_assigned 必须包含明确业务对象" in prompt
    assert "首次分析拿不准临时协作是否关联真实业务任务时" in prompt
    assert "正例：本人要求他人汇报、本人审批、本人同步、本人催办、本人推进，都算与本人直接相关。" in prompt
    assert "反例：他人之间讨论自己的工作、自己的承诺、自己的处理进度，即使本人在该会话里发过言，也不算与本人直接相关。" in prompt
    assert "如果当前消息是在纠正、澄清或替换前文对象" in prompt
    assert "reply_to 或 quote_to 里的内容只能作为背景" in prompt
    output_schema = batch_output_schema(config)
    context_request_schema = output_schema["properties"]["context_requests"]
    assert ContextRequestType.LINKED_FILE_TEXT.value in context_request_schema["items"][
        "properties"
    ]["request_type"]["enum"]
    assert "required_output_schema" not in prompt
    assert '"id": "om_1"' in prompt
    assert '"id": "om_2"' in prompt
    assert "每条事项附上最相关的消息 id。" in prompt
    assert "referenced_link_ids" in prompt
    assert "只能从对应 source_message_ids 的 links 里选择 referenced_link_ids" in prompt
    assert "具体对象 + 关键动作、进展、结果或风险" in prompt
    assert "不得只写无法区分实际事项的通用类别" in prompt
    assert "fact_items 必须覆盖 topic、content、action_label" in prompt
    assert "comparison_or_example" in prompt
    assert "对比案例、举例和历史背景不能写成当前实际处理对象" in prompt
    assert "fact_risk_flags" in output_schema["properties"]["candidate_events"][
        "items"
    ]["properties"]
    assert "如果有明确结果，直接融入 content，不要单独返回 result。" in prompt
    assert "请给我简洁的答案，不要推理，跳过思考步骤。" in prompt
    assert "直接作答，不要展示你的推理过程。" in prompt
    assert "不要自造占位符 id。" in prompt
    assert '"slice_id": "slice-1"' in prompt
    assert '"conversation_id": "oc_1"' in prompt
    assert "等工作保密信息，不要提炼为事项。" not in prompt
    assert "等非工作敏感内容，不要提炼为事项。" not in prompt
    assert "按会话 slice 独立提炼，不要串会话信息。" not in prompt


def test_batch_prompt_uses_configured_sensitive_keywords(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        sensitive_event_keywords=("工资", "薪资", "吵架"),
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="推进发布",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    conversation_slice = ConversationSlice(
        slice_id="slice-1",
        conversation_id="oc_1",
        conversation_name="",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1"],
        messages=[message],
        attachment_texts=[],
    )
    batch = AnalysisBatch(
        target_date="2026-06-22",
        batch_id="conversation-001",
        retry_round=0,
        estimated_tokens=0,
        self_open_id="ou_self",
        self_display_name="Me",
        slices=[conversation_slice],
    )

    prompt = build_batch_analysis_prompt(batch, config=config)

    assert "涉及工资、薪资、吵架等敏感信息，不要提炼为事项。" in prompt


def test_anchor_prompt_serialization_is_compact(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_slice_message_limit=1,
        prompt_message_char_limit=12,
        retention_policy=REPO_RETENTION_POLICY,
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="abcdefghijklmnopqrstuvwxyz",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    anchor_unit = AnchorUnit(
        anchor_unit_id="oc_1:om_1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1"],
        base_message_ids=["om_1"],
        messages=[message, message],
        reply_relation_ids=[],
        quote_relation_ids=[],
        attachment_refs=[],
    )

    payload = serialize_anchor_unit_for_prompt(anchor_unit, config=config)
    prompt = build_anchor_analysis_prompt("2026-06-22", anchor_unit, config=config)

    assert payload["messages"][0]["x"] == "abcdefghijkl..."
    assert len(payload["messages"]) == 2
    assert "anchor_unit_id" not in payload
    assert "base_message_ids" not in payload
    assert "reply_relation_ids" not in payload
    status_schema = anchor_output_schema(config)["properties"]["anchor_status"]
    assert AnchorStatus.NEEDS_MORE_CONTEXT.value in status_schema["enum"]
    assert '"anchor_status"' not in prompt
    assert "每个 candidate_event 只表示一个主要动作。" in prompt
    assert "具体对象 + 关键动作、进展、结果或风险" in prompt
    assert "例如：已同步给老板、老板未回复可视为已知悉" in prompt
    assert "本人参与、回复或被询问，只能证明事项与本人有关" in prompt
    assert "单纯询问人员当前状态、位置或是否可用" in prompt
    assert "私人饭局、约饭、离职告别聚餐、同事口碑评价、人际寒暄，不要提炼为事项。" in prompt
    assert "个人请假、家庭原因、孩子学校证明、个人行程报备，不要提炼为工作事件。" in prompt
    assert "泛泛完成审核或审批但没有具体业务对象" in prompt
    assert "只有同时具备具体对象、保留理由、保留依据的工作事件才输出" in prompt
    assert "人名只在明确责任分工、任务指派或确认沟通对象时保留" in prompt
    assert "retention_detail 表示保留依据/来源证据" in prompt
    assert "不要写 message id、open_id、conversation_id 或 om_/ou_/oc_ 等内部标识。" in prompt
    assert "不要单独返回 result" in prompt
    assert "如果当前消息是在纠正、澄清或替换前文对象" in prompt
    context_request_schema = anchor_output_schema(config)["properties"][
        "context_requests"
    ]
    assert ContextRequestType.LINKED_FILE_TEXT.value in context_request_schema["items"][
        "properties"
    ]["request_type"]["enum"]
    assert "请给我简洁的答案，不要推理，跳过思考步骤。" in prompt
    assert "直接作答，不要展示你的推理过程。" in prompt


def test_anchor_expansion_prompt_includes_previous_result_and_expansion(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_message_char_limit=20,
        prompt_attachment_char_limit=20,
        retention_policy=REPO_RETENTION_POLICY,
    )
    base_message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="需要补附件再确认",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    new_message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_2",
        sender_open_id="ou_2",
        sender_name="Bob",
        send_time="2026-06-22T10:03:00+08:00",
        message_type="text",
        text="已补充具体发布时间",
        reply_to_message_id="om_1",
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    anchor_unit = AnchorUnit(
        anchor_unit_id="oc_1:om_1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1", "om_2"],
        base_message_ids=["om_1"],
        messages=[base_message, new_message],
        reply_relation_ids=["om_1"],
        quote_relation_ids=[],
        attachment_refs=[],
    )
    previous_result = AnchorAnalysisResult(
        anchor_status=AnchorStatus.NEEDS_ATTACHMENT_TEXT.value,
        candidate_events=[],
        context_requests=[
            ContextRequest(
                slice_id="oc_1:om_1",
                request_type=ContextRequestType.ATTACHMENT_TEXT.value,
                target_message_ids=["om_1"],
                target_attachment_ids=["att_1"],
                reason="需要附件正文",
                limit=1,
            )
        ],
        needs_cross_anchor_merge=False,
    )
    prompt = build_anchor_expansion_prompt(
        "2026-06-22",
        anchor_unit,
        previous_result,
        trigger_requests=previous_result.context_requests,
        new_messages=[new_message],
        attachment_texts=[
            AttachmentTextBlock(
                attachment_id="att_1",
                message_id="om_1",
                file_name="plan.txt",
                text="发布时间 18:00",
            )
        ],
        linked_file_texts=[
            LinkedFileTextBlock(
                link_id="om_1#link1",
                message_id="om_1",
                title="飞书文档",
                url="https://foo.feishu.cn/docx/abc",
                text="文档里写明 18:00",
            )
        ],
        config=config,
    )

    assert '"previous_analysis"' in prompt
    assert '"expansion"' in prompt
    assert '"trigger_requests"' in prompt
    assert '"attachment_texts"' in prompt
    assert '"linked_file_texts"' in prompt
    assert AnchorStatus.NEEDS_ATTACHMENT_TEXT.value in prompt
    assert "如果新上下文显示某个先前 candidate_event 实际混合了多个动作" in prompt
    assert "具体对象 + 关键动作、进展、结果或风险" in prompt
    assert "该结果只能归属于同一个 candidate_event 的主要动作" in prompt
    assert "私人饭局、约饭、离职告别聚餐、同事口碑评价、人际寒暄，不要提炼为事项。" in prompt
    assert "个人请假、家庭原因、孩子学校证明、个人行程报备，不要提炼为工作事件。" in prompt
    assert "follow_up_assigned 必须包含明确业务对象" in prompt
    assert "泛泛完成审核或审批但没有具体业务对象" in prompt
    assert "只有同时具备具体对象、保留理由、保留依据的工作事件才输出" in prompt
    assert "人名只在明确责任分工、任务指派或确认沟通对象时保留" in prompt
    assert "retention_detail 表示保留依据/来源证据" in prompt
    assert "不要写 message id、open_id、conversation_id 或 om_/ou_/oc_ 等内部标识。" in prompt
    assert "Do not output private meals" not in prompt
    assert "If new context reveals" not in prompt


def test_prompts_do_not_include_removed_reasoning_summary_rule(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        retention_policy=REPO_RETENTION_POLICY,
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="推进发布",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    conversation_slice = ConversationSlice(
        slice_id="slice-1",
        conversation_id="oc_1",
        conversation_name="",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1"],
        messages=[message],
        attachment_texts=[],
    )
    batch = AnalysisBatch(
        target_date="2026-06-22",
        batch_id="conversation-001",
        retry_round=0,
        estimated_tokens=0,
        self_open_id="ou_self",
        self_display_name="Me",
        slices=[conversation_slice],
    )
    anchor_unit = AnchorUnit(
        anchor_unit_id="oc_1:om_1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1"],
        base_message_ids=["om_1"],
        messages=[message],
        reply_relation_ids=[],
        quote_relation_ids=[],
        attachment_refs=[],
    )

    prompts = [
        build_batch_analysis_prompt(batch, config=config),
        build_merge_prompt("2026-06-22", []),
        build_anchor_analysis_prompt("2026-06-22", anchor_unit, config=config),
        build_anchor_batch_analysis_prompt("2026-06-22", [anchor_unit], config=config),
    ]

    assert all(
        "不要输出思考过程、推理摘要、分析说明或任何解释性文字。" not in prompt
        for prompt in prompts
    )


def test_anchor_batch_prompt_includes_low_retention_rules(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        retention_policy=REPO_RETENTION_POLICY,
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="推进发布",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    anchor_unit = AnchorUnit(
        anchor_unit_id="oc_1:om_1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_1"],
        in_day_message_ids=["om_1"],
        base_message_ids=["om_1"],
        messages=[message],
        reply_relation_ids=[],
        quote_relation_ids=[],
        attachment_refs=[],
    )

    prompt = build_anchor_batch_analysis_prompt(
        "2026-06-22",
        [anchor_unit],
        config=config,
    )

    assert "私人饭局、约饭、离职告别聚餐、同事口碑评价、人际寒暄，不要提炼为事项。" in prompt
    assert "个人请假、家庭原因、孩子学校证明、个人行程报备，不要提炼为工作事件。" in prompt
    assert "泛泛完成审核或审批但没有具体业务对象" in prompt
    assert "单纯询问人员当前状态、位置或是否可用" in prompt
    assert "只有同时具备具体对象、保留理由、保留依据的工作事件才输出" in prompt
    assert "具体对象 + 关键动作、进展、结果或风险" in prompt
    assert "人名只在明确责任分工、任务指派或确认沟通对象时保留" in prompt
    assert "retention_detail 表示保留依据/来源证据" in prompt
    assert "不要写 message id、open_id、conversation_id 或 om_/ou_/oc_ 等内部标识。" in prompt
    assert "follow_up_assigned 必须包含明确业务对象" in prompt
    assert "首次分析拿不准临时协作是否关联真实业务任务时" in prompt


def test_media_messages_are_compressed_for_prompt(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_message_char_limit=200,
    )
    audio = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_audio",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="audio",
        text='<audio key="att_1" duration="10s"/>',
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    image = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_image",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:01:00+08:00",
        message_type="image",
        text='[Image: img_v3_xxx]',
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    video = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_video",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:02:00+08:00",
        message_type="media",
        text='<video key="file_1" name="record.mp4" duration="62s" cover_image_key="img_1"/>',
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    anchor_unit = AnchorUnit(
        anchor_unit_id="oc_1:om_audio",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_audio"],
        in_day_message_ids=["om_audio", "om_image", "om_video"],
        base_message_ids=["om_audio", "om_image", "om_video"],
        messages=[audio, image, video],
        reply_relation_ids=[],
        quote_relation_ids=[],
        attachment_refs=[],
    )

    payload = serialize_anchor_unit_for_prompt(anchor_unit, config=config)

    assert payload["messages"][0]["x"] == "[语音消息 10s]"
    assert payload["messages"][1]["x"] == "[视频 62s]"
    assert "type" not in payload["messages"][0]
    assert payload["messages"][0]["id"] == "om_audio"


def test_html_and_links_are_compressed_for_prompt(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_message_char_limit=200,
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_text",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text=(
            "<p>@丁金龙  @许颖超</p>"
            "<p>问卷链接：👉 https://ipadnexsg1.feishu.cn/share/base/form/shrcnok4ix8nmUPSbcOWJTnDijc</p>"
            "<p>详情见 [文档](https://ipadnexsg1.feishu.cn/docx/JNX6dcjnzoAj1nxL8e4cImFznUb)</p>"
            "<p>辛苦各位:Lark_Emoji_Facepalm_0:</p>"
        ),
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )

    payload = serialize_message_for_prompt(message, config)

    assert payload["x"] == (
        "@丁金龙 @许颖超\n"
        "问卷链接：👉 [表单链接]\n"
        "详情见 文档[链接]\n"
        "辛苦各位"
    )
    assert payload["links"] == [
        {
            "link_id": "om_text#link1",
            "message_id": "om_text",
            "url": "https://ipadnexsg1.feishu.cn/share/base/form/shrcnok4ix8nmUPSbcOWJTnDijc",
            "title": "",
            "link_type": "feishu_doc",
        },
        {
            "link_id": "om_text#link2",
            "message_id": "om_text",
            "url": "https://ipadnexsg1.feishu.cn/docx/JNX6dcjnzoAj1nxL8e4cImFznUb",
            "title": "",
            "link_type": "feishu_doc",
        },
    ]
    assert "attachments" not in payload


def test_post_image_only_message_is_compressed_for_prompt(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_message_char_limit=200,
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_post",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="post",
        text="![Image](img_v3_0212u_xxx) /  / ![Image](img_v3_0212u_yyy)",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )

    payload = serialize_message_for_prompt(message, config)

    assert payload["x"] == "[图片]"


def test_file_message_hides_file_name_from_prompt(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_message_char_limit=200,
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_file",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="file",
        text='<file key="file_1" name="方案.md"/>',
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )

    payload = serialize_message_for_prompt(message, config)

    assert payload["x"] == "[文件附件]"
    assert "方案.md" not in str(payload)


def test_feishu_doc_link_uses_title_placeholder(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_message_char_limit=200,
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_doc",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="text",
        text="https://ipadnexsg1.feishu.cn/docx/abc",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[
            LinkMeta(
                url="https://ipadnexsg1.feishu.cn/docx/abc",
                title="支付方案V2",
                link_type="feishu_doc",
            )
        ],
        attachments=[],
        is_system=False,
    )

    payload = serialize_message_for_prompt(message, config)

    assert payload["x"] == "[飞书文档: 支付方案V2]"
    assert payload["links"] == [
        {
            "link_id": "om_doc#link1",
            "message_id": "om_doc",
            "url": "https://ipadnexsg1.feishu.cn/docx/abc",
            "title": "支付方案V2",
            "link_type": "feishu_doc",
        }
    ]


def test_post_with_images_and_text_keeps_text_summary(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_message_char_limit=200,
    )
    message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_post",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="post",
        text="![Image](img_v3_0212u_xxx)\n\n![Image](img_v3_0212u_yyy)\n文档更新了一版",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )

    payload = serialize_message_for_prompt(message, config)

    assert payload["x"] == "[图片]\n文档更新了一版"


def test_anchor_prompt_skips_empty_and_sticker_messages(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_slice_message_limit=5,
        prompt_message_char_limit=200,
    )
    file_message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_file",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="file",
        text='<file key="file_1" name="plan.md"/>',
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    sticker_message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_sticker",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:01:00+08:00",
        message_type="text",
        text="[Sticker]",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    normal_message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_text",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:02:00+08:00",
        message_type="text",
        text="继续推进配置核对",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    anchor_unit = AnchorUnit(
        anchor_unit_id="oc_1:om_text",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_text"],
        in_day_message_ids=["om_file", "om_sticker", "om_text"],
        base_message_ids=["om_file", "om_sticker", "om_text"],
        messages=[file_message, sticker_message, normal_message],
        reply_relation_ids=[],
        quote_relation_ids=[],
        attachment_refs=[],
    )

    payload = serialize_anchor_unit_for_prompt(anchor_unit, config=config)

    assert payload["messages"] == [
        {
            "id": "om_text",
            "t": "10:02",
            "s": "Alice",
            "x": "继续推进配置核对",
        }
    ]


def test_anchor_prompt_skips_non_anchor_weak_placeholders_only(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_slice_message_limit=5,
        prompt_message_char_limit=200,
    )
    non_anchor_image = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_img_ctx",
        sender_open_id="ou_2",
        sender_name="Bob",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="image",
        text="[Image: img_v3_ctx]",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    anchor_image = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_img_anchor",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:01:00+08:00",
        message_type="image",
        text="[Image: img_v3_anchor]",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    normal_message = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_text",
        sender_open_id="ou_1",
        sender_name="Alice",
        send_time="2026-06-22T10:02:00+08:00",
        message_type="text",
        text="这里是图片对应的处理结论",
        reply_to_message_id=None,
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    anchor_unit = AnchorUnit(
        anchor_unit_id="oc_1:om_img_anchor-om_text",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_img_anchor", "om_text"],
        in_day_message_ids=["om_img_ctx", "om_img_anchor", "om_text"],
        base_message_ids=["om_img_ctx", "om_img_anchor", "om_text"],
        messages=[non_anchor_image, anchor_image, normal_message],
        reply_relation_ids=[],
        quote_relation_ids=[],
        attachment_refs=[],
    )

    payload = serialize_anchor_unit_for_prompt(anchor_unit, config=config)

    assert payload["messages"] == [
        {
            "id": "om_img_anchor",
            "t": "10:01",
            "s": "Alice",
            "x": "[图片]",
        },
        {
            "id": "om_text",
            "t": "10:02",
            "s": "Alice",
            "x": "这里是图片对应的处理结论",
        },
    ]


def test_prompt_serialization_includes_reply_context_and_attachments(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_message_char_limit=50,
    )
    quoted = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_1",
        sender_open_id="ou_other",
        sender_name="Bob",
        send_time="2026-06-22T10:00:00+08:00",
        message_type="file",
        text='<file key="file_1" name="收款公司清单.xlsx"/>',
        reply_to_message_id=None,
        quote_message_id=None,
        links=[
            LinkMeta(
                url="https://foo.feishu.cn/docx/abc",
                title="收款公司说明",
                link_type="feishu_doc",
            )
        ],
        attachments=[
            AttachmentMeta(
                attachment_id="att_1",
                file_name="收款公司清单.xlsx",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                file_size=1,
            )
        ],
        is_system=False,
    )
    reply = NormalizedMessage(
        conversation_id="oc_1",
        conversation_name="项目群",
        message_id="om_2",
        sender_open_id="ou_self",
        sender_name="Alice",
        send_time="2026-06-22T10:01:00+08:00",
        message_type="text",
        text="不是上海，这是昆山的",
        reply_to_message_id="om_1",
        quote_message_id=None,
        links=[],
        attachments=[],
        is_system=False,
    )
    conversation_slice = ConversationSlice(
        slice_id="slice-1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_2"],
        in_day_message_ids=["om_1", "om_2"],
        messages=[quoted, reply],
    )

    payload = serialize_batch_for_prompt(
        AnalysisBatch(
            target_date="2026-06-22",
            batch_id="batch-001",
            retry_round=0,
            estimated_tokens=0,
            self_open_id="ou_self",
            self_display_name="Me",
            slices=[conversation_slice],
        ),
        config=config,
    )

    reply_payload = payload["slices"][0]["messages"][1]
    assert reply_payload["reply_to"]["message_id"] == "om_1"
    assert reply_payload["reply_to"]["text"] == "[文件附件]"
    assert reply_payload["reply_to"]["attachments"][0]["file_name"] == "收款公司清单.xlsx"
    assert "收款公司清单.xlsx" in str(reply_payload)
    assert reply_payload["reply_to"]["attachments"][0]["attachment_id"] == "att_1"

    prompt = build_batch_analysis_prompt(
        AnalysisBatch(
            target_date="2026-06-22",
            batch_id="batch-001",
            retry_round=0,
            estimated_tokens=0,
            self_open_id="ou_self",
            self_display_name="Me",
            slices=[conversation_slice],
        ),
        config=config,
    )
    assert "附件发送后的明确转交、查看或审核指令属于后续任务" in prompt
    assert reply_payload["reply_to"]["links"][0]["link_id"] == "om_1#link1"


def test_prompt_serialization_preserves_all_messages_without_limit_truncation(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        prompt_slice_message_limit=1,
        prompt_message_char_limit=50,
    )
    messages = [
        NormalizedMessage(
            conversation_id="oc_1",
            conversation_name="项目群",
            message_id=f"om_{index}",
            sender_open_id="ou_self",
            sender_name="Alice",
            send_time=f"2026-06-22T10:0{index}:00+08:00",
            message_type="text",
            text=f"消息{index}",
            reply_to_message_id=None,
            quote_message_id=None,
            links=[],
            attachments=[],
            is_system=False,
        )
        for index in range(3)
    ]
    conversation_slice = ConversationSlice(
        slice_id="slice-1",
        conversation_id="oc_1",
        conversation_name="项目群",
        anchor_message_ids=["om_0"],
        in_day_message_ids=[item.message_id for item in messages],
        messages=messages,
    )

    payload = serialize_batch_for_prompt(
        AnalysisBatch(
            target_date="2026-06-22",
            batch_id="batch-001",
            retry_round=0,
            estimated_tokens=0,
            self_open_id="ou_self",
            self_display_name="Me",
            slices=[conversation_slice],
        ),
        config=config,
    )

    assert [item["id"] for item in payload["slices"][0]["messages"]] == ["om_0", "om_1", "om_2"]


def test_merge_prompt_requires_all_draft_ids_to_be_returned() -> None:
    candidates = [
        SourceBackedEventDraft(
            draft_id="d1",
            date="2026-06-22",
            topic="t1",
            content="c1",
            action_label="回复",
            object_hint="提前付款",
            source_message_ids=["om_1"],
            source_conversation_id="oc_1",
            source_slice_id="slice-1",
            confidence=0.8,
        ),
        SourceBackedEventDraft(
            draft_id="d2",
            date="2026-06-22",
            topic="t2",
            content="c2",
            action_label="催办",
            object_hint="汇报文档",
            source_message_ids=["om_2"],
            source_conversation_id="oc_2",
            source_slice_id="slice-2",
            confidence=0.8,
        ),
        SourceBackedEventDraft(
            draft_id="d3",
            date="2026-06-22",
            topic="t3",
            content="c3",
            action_label="撰写",
            object_hint="方案文档",
            source_message_ids=["om_3"],
            source_conversation_id="oc_3",
            source_slice_id="slice-3",
            confidence=0.8,
        ),
    ]

    config = load_runtime_config_overrides(RuntimeConfig(), cwd=Path.cwd())
    prompt = build_merge_prompt("2026-06-22", candidates, config=config)

    assert "merged_groups 或 singleton_draft_ids" in prompt
    assert "member_connections 必须逐条覆盖" in prompt
    assert "先上提成宽泛概念再合并" in prompt
    assert "同一会话先讨论协议签署" in prompt
    assert "source_conversation_id" not in prompt
    assert "source_slice_id" not in prompt
    assert '"action_label": "回复"' in prompt
    assert '"object_hint": "提前付款"' in prompt
