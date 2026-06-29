# WorkTrace 锚点首轮识别协议

## 1. 文档目标

本文档定义当前 `anchor_experiment` 使用的 `AnchorUnit` 首轮识别协议：Python 送给 LLM 的输入结构，以及 LLM 必须返回的输出结构。

## 2. 输入对象

首轮锚点识别固定输入如下：

- `target_date`
- `pass_index`
- `anchor_unit`

其中 `anchor_unit` 当前实际送出的字段为：

- `conversation_name`
- `messages`
- `omitted_message_count`
- `attachment_refs`

### 2.1 `messages` 字段

每条消息当前使用压缩字段：

- `t`：时间
- `s`：发送人
- `x`：压缩后的消息文本

锚点协议当前不会在消息对象里额外发送 `id`、`type`、`reply_to`、`quote_to`、`links`、`attachments`。

### 2.2 `attachment_refs` 字段

首轮只提供附件元信息，不提供附件正文。每个附件对象当前包含：

- `id`
- `name`
- `mime`

## 3. 输出对象

LLM 必须返回单个 JSON 对象，固定包含以下顶层字段：

- `anchor_status`
- `candidate_events`
- `context_requests`
- `needs_cross_anchor_merge`

不允许返回 Markdown、解释性文字或额外顶层键。

## 4. `anchor_status` 枚举

`anchor_status` 固定取值如下：

- `completed`
- `needs_more_context`
- `needs_attachment_text`
- `not_work_related`
- `uncertain`

语义约束：

- `completed`：当前锚点已经可以形成稳定候选事项，或可稳定判定为空结果
- `needs_more_context`：需要补更早或更晚聊天上下文
- `needs_attachment_text`：需要补附件正文
- `not_work_related`：当前锚点可明确判定为非工作事项
- `uncertain`：当前信息不足，但未形成清晰补充请求

## 5. `candidate_events`

`candidate_events` 结构与现有首轮候选事项保持兼容，每个元素固定包含：

- `draft_id`
- `date`
- `topic`
- `content`
- `action_label`
- `object_hint`
- `retention_reason`
- `retention_detail`
- `source_message_ids`
- `source_conversation_id`
- `source_slice_id`
- `confidence`

补充约束：

- `source_slice_id` 在锚点协议里写 `anchor_unit_id`
- `source_message_ids` 只能引用当前 `anchor_unit` 内已有消息
- `action_label` 只写主要动作标签，不要写成长句
- `object_hint` 只写核心对象或主题，不要把完整事件内容复制进去
- `retention_reason` 只能取 `deliverable_updated`、`decision_made`、`issue_or_risk_found`、`follow_up_assigned`、`external_business_progress`、`substantive_approval`
- `retention_detail` 用一句话说明为什么值得沉淀，必须来自当前输入中的具体对象、结论、问题、待办或结果
- 普通约时间、确认开会、互通信息、泛泛完成审核/审批但没有具体对象和结论的内容，不要输出 candidate event

## 6. `context_requests`

`context_requests` 继续沿用现有协议，只允许：

- `earlier_messages`
- `later_messages`
- `attachment_text`

每个请求固定包含：

- `slice_id`
- `request_type`
- `target_message_ids`
- `target_attachment_ids`
- `reason`
- `limit`

补充约束：

- `slice_id` 在锚点协议里写 `anchor_unit_id`
- `attachment_text` 请求必须同时提供 `target_message_ids` 与 `target_attachment_ids`
- `earlier_messages / later_messages` 请求不允许填写附件 ID

## 7. `needs_cross_anchor_merge`

`needs_cross_anchor_merge` 是布尔值。

只有在模型明确判断：

- 当前事项可能延伸到其他锚点窗口
- 或当前事项可能需要跨会话合并

时，才返回 `true`。

否则固定返回 `false`。

## 8. 失败处理建议

若输出违反以下任一条件，Python 应视为协议失败：

- 顶层不是 JSON 对象
- 缺失 `anchor_status`
- `anchor_status` 不在允许枚举内
- `candidate_events` 不是数组
- `context_requests` 不是数组
- `needs_cross_anchor_merge` 不是布尔值

首版建议：

- 解析失败不自动兜底改写
- 由调用方决定重试、跳过或回退
