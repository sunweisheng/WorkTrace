# WorkTrace 跨会话事件合并设计

## 1. 文档目标

本文档说明 WorkTrace 当前已经落地的“全日候选事件跨会话合并”实现。

它解决的问题是：

- 首轮单会话提炼只能看单个会话
- 同一天内不同会话可能在讲同一件真实工作事件
- 需要在全日范围内统一判断哪些候选事件应归为同一组

本文档不讨论首轮单会话提炼、会话内扩窗重跑或 Markdown 输出。

## 2. 当前背景

### 2.1 首轮已经是单会话提炼

当前主流程中：

1. 按会话构造 `ConversationSlice`
2. `1 个会话 = 1 次首轮 LLM`
3. 每次首轮只负责提炼该会话中的 `candidate_events`

这一步的目标是识别“这个会话里讲了哪些事”，而不是判断跨会话关系。

### 2.2 跨会话关系必须在后置阶段判断

由于首轮每次只看一个会话，模型天然无法直接判断：

- A 会话里的候选事件
- 与 B 会话里的候选事件
- 是否其实在描述同一件真实工作事件

因此当前实现把“提炼事项”和“跨会话分组”拆成两步。

## 3. 当前实现目标

当前实现遵循以下目标：

1. 首轮只做单会话事件提炼
2. 所有会话提炼完成后，再统一做一次跨会话分组
3. 不再把原始聊天上下文重新送入合并阶段
4. 只把瘦身后的候选事件卡片送给 LLM
5. Python 根据分组结果物化真正的 `MergedEventDraft`

## 4. 当前主流程

当前代码中的跨会话合并流程如下：

1. 对每个会话完成首轮提炼与必要的扩窗重跑
2. 汇总全日所有 `candidate_events`
3. 若候选事件只有 1 条，则直接物化为单组 `MergedEventDraft`
4. 若候选事件多于 1 条，则调用 `merge_day_candidates(...)`
5. analyzer 返回 `CrossConversationGroupResult`
6. Python 调用 `materialize_grouped_merged_drafts(...)`
7. 生成真正合并后的 `MergedEventDraft`
8. 再进入 `build_work_events(...)`

主流程代码位于 [runner.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/runner.py)。

## 5. 输入与输出边界

### 5.1 进入合并阶段之前

进入跨会话合并阶段之前，每条 `candidate_event` 已经是：

- 单会话内相对完整的事项草稿
- 带 `topic`
- 带 `content`
- 带 `result`
- 带来源会话和来源消息

### 5.2 跨会话合并阶段负责的事

当前这一轮只负责：

- 判断哪些候选事件属于同一真实工作事件
- 给每条候选事件分配最终 group

### 5.3 跨会话合并阶段不负责的事

当前这一轮不负责：

- 再次补前后文
- 再次补附件正文
- 判断是否是工作事项
- 重新阅读原始会话 transcript

这些都应在前面的单会话阶段完成。

## 6. 送给 LLM 的输入形态

### 6.1 当前输入原则

为了降低 prompt 体积，当前跨会话分组阶段只发送瘦身后的候选事件卡片，而不发送原始聊天消息。

### 6.2 当前卡片字段

当前 prompt 里每条候选事件卡片使用的是压缩字段：

- `id`
- `t`
- `c`
- `r`

其中分别对应：

- `draft_id`
- `topic`
- `content`
- `result`

序列化逻辑位于 [prompts.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/analyzers/prompts.py) 的 `serialize_cross_merge_candidate_for_prompt(...)`。

### 6.3 当前不传的信息

当前这一轮不会传入：

- 原始消息正文
- 原始会话 transcript
- `source_message_ids`
- 附件正文
- 扩窗后的上下文消息

这样可以让模型聚焦“是否同一事件”的判断本身。

## 7. LLM 返回协议

### 7.1 当前返回结构

当前 `merge_day_candidates(...)` 返回的是：

- `CrossConversationGroupResult`

其中包含：

- `groups: list[CrossConversationGroup]`

每个 `CrossConversationGroup` 包含：

- `group_id`
- `draft_ids`

对应数据模型位于 [models.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/models.py)。

### 7.2 当前语义

该返回值表达的是：

- 哪些 `draft_id` 属于同一个跨会话分组

而不是直接返回最终 `MergedEventDraft` 文案。

## 8. Python 物化阶段

当前 Python 侧通过 [cross_conversation_merge.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/pipeline/cross_conversation_merge.py) 中的 `materialize_grouped_merged_drafts(...)` 完成分组物化。

物化规则包括：

- `topic` 从组内候选中选优
- `content` 合并组内内容
- `result` 从组内候选中选优
- `source_message_ids` 去重汇总
- `source_conversation_ids` 去重汇总

因此当前的 `MergedEventDraft` 才真正表示“已经做过跨会话归并后的事件草稿”。

## 9. 当前设计原则

### 9.1 职责分层清晰

当前主流程明确拆成两层：

- 单会话首轮：提炼事项
- 全日合并阶段：判断哪些事项其实是一件事

### 9.2 一次性全日分组优先

当前优先采用：

- 全日候选事件一次性送 LLM 分组

而不是旧方案里的：

- 先预分桶
- 再桶内归并
- 再桶间二次归并

### 9.3 保守合并

当前提示词采用保守原则：

- 只有明显属于同一真实事件时才合并
- 拿不准时宁可分开

这能减少错误归并造成的信息污染。

## 10. 当前代码落点

本设计当前主要落在以下文件：

- [runner.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/runner.py)
- [cross_conversation_merge.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/pipeline/cross_conversation_merge.py)
- [prompts.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/analyzers/prompts.py)
- [base.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/analyzers/base.py)
- [models.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/models.py)

## 11. 当前状态总结

截至当前版本，WorkTrace 已经具备真实的跨会话事件合并阶段。

主流程不再把 `MergedEventDraft` 当作简单包装层，而是：

- 先让 LLM 在全日范围内做跨会话分组
- 再由 Python 物化为真正合并后的事件草稿

这使最终事件列表能更准确地表达“同一天里真正发生了哪些工作事件”。
