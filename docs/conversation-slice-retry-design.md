# WorkTrace 会话扩窗重跑闭环设计

## 1. 文档目标

本文档说明 WorkTrace 当前已经落地的“单会话首轮提炼 + 会话内扩窗重跑”闭环实现。

当前闭环覆盖：

- 会话消息扩窗
- 附件正文按需补充
- 同一会话多轮重跑直到收敛

本文档不讨论跨会话合并、缓存策略或 provider 切换。

## 2. 当前实现概览

当前主流程已经实现为：

1. 按会话构造 `ConversationSlice`
2. `1 个会话 = 1 次首轮 LLM`
3. LLM 返回：
   - `candidate_events`
   - `context_requests`
4. Python 校验首轮结果
5. 若存在 `context_requests`，则自动补上下文并对同一会话重跑
6. 直到收敛、无新信息或达到重跑上限
7. 只有最终 unresolved 时才记录 warning

对应主控代码位于 [runner.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/runner.py) 的 `_analyze_conversation_slice_with_retry(...)`。

## 3. 闭环目标

当前实现中，`context_requests` 已经从“提醒信息”升级为“可执行控制信号”。

系统会在单会话内完成：

1. 分析
2. 补上下文
3. 重跑
4. 收敛或退出

这保证了模型在明确表达“缺上下文”时，主流程会继续推进，而不是仅记录 warning 后结束。

## 4. 支持范围

### 4.1 已支持的 `context_requests`

当前支持以下三类补充请求：

- `earlier_messages`
- `later_messages`
- `attachment_text`

### 4.2 当前不做的事

当前闭环不支持：

- 跨会话扩窗
- 邻近 slice 联动补充
- 基于语义的复杂二次裁剪
- 无上限自动追问

范围被刻意限制在“同一会话内闭环”，以保持流程可解释且易于收敛。

## 5. 核心原则

### 5.1 只在单会话内闭环

当前首轮已经是：

- `1 个会话 = 1 个 slice = 1 次首轮 LLM`

因此重跑也保持相同粒度：

- 只扩展当前 `ConversationSlice`
- 只重跑当前 `ConversationSlice`

### 5.2 `context_requests` 不是失败

只要模型返回合法 `context_requests`，就表示：

- 它已经初步识别到事项
- 也知道自己还缺哪类信息

因此在闭环完成前，这不应直接视为失败。

### 5.3 自动扩窗必须可收敛

当前实现有明确停止条件，避免重复请求或无限循环：

- 没有 `context_requests`
- 达到 `slice_retry_limit`
- 扩窗后 `ConversationSlice` 签名未变化

### 5.4 附件正文按需补充

附件正文只在模型明确请求时才补充，不会在首轮默认预读全部附件正文。

## 6. 运行流程

当前单会话处理流程如下：

1. 用当前 `ConversationSlice` 构造 `AnalysisBatch`
2. 调用 `analyzer.analyze_batch(...)`
3. 用 `validate_batch_analysis_result(...)` 校验返回结果
4. 若没有 `context_requests`，该会话处理完成
5. 若有 `context_requests`，调用 `expand_slice_context(...)` 补充上下文
6. 生成扩展后的 `ConversationSlice`
7. 构造 retry batch 并再次调用 analyzer
8. 重复以上过程直到满足停止条件

补上下文的主实现位于 [context_expansion.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/pipeline/context_expansion.py)。

## 7. 关键对象与职责

### 7.1 `ConversationSlice`

`ConversationSlice` 持续作为单会话处理的核心载体，承载：

- 当前会话消息列表
- 锚点消息 id
- 当日消息 id
- 附件正文块

### 7.2 `ContextRequest`

`ContextRequest` 使用现有协议字段：

- `slice_id`
- `request_type`
- `target_message_ids`
- `target_attachment_ids`
- `reason`
- `limit`

### 7.3 `_analyze_conversation_slice_with_retry(...)`

该函数负责：

- 对单个会话执行首轮分析
- 根据 `context_requests` 自动扩窗
- 控制重跑轮数
- 在最终 unresolved 时产出 warning

当前返回值为：

- `BatchAnalysisResult`
- 最终 `ConversationSlice`
- `list[str]` warning
- `bool` 是否 unresolved
- `int` 本会话实际 analyzer 调用次数

## 8. 上下文补充细节

### 8.1 消息扩窗

当请求类型为：

- `earlier_messages`
- `later_messages`

系统会通过 `chat_source` 拉取目标消息附近的更多会话消息，并并入当前 slice。

### 8.2 附件正文补充

当请求类型为 `attachment_text` 时，系统会：

1. 在当前 slice 中定位目标消息
2. 按 `target_attachment_ids` 过滤附件
3. 调 `content_resolver` 加载正文
4. 把新增 `AttachmentTextBlock` 合并回 slice

### 8.3 扩展合并

扩展后的新老信息会按以下规则合并：

- 消息按 `message_id` 去重
- 按时间顺序重排消息
- 附件正文按 `attachment_id` 去重

## 9. 停止条件与 warning 策略

### 9.1 正常收敛

以下情况视为正常完成：

- 本轮没有 `context_requests`

### 9.2 达到重跑上限

当 `retry_round >= slice_retry_limit` 且仍有 `context_requests` 时：

- 当前会话停止继续扩窗
- 对未解决请求记录 warning

### 9.3 扩窗无新信息

系统会比较扩窗前后的 slice 签名：

- 消息 `message_id` 序列
- 附件 `attachment_id` 序列

若签名未变化，说明本轮扩展没有引入新信息，也会停止重跑并记录 warning。

## 10. 当前代码落点

本设计当前主要落在以下文件：

- [runner.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/runner.py)
- [context_expansion.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/pipeline/context_expansion.py)
- [validation.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/pipeline/validation.py)
- [models.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/models.py)

## 11. 当前状态总结

截至当前版本，WorkTrace 已经不再把 `context_requests` 仅仅当作 warning。

系统会在单会话内自动完成：

- 首轮提炼
- 按需扩窗
- 附件补充
- 重跑收敛

这为后续跨会话合并阶段提供了更完整、更稳定的候选事件输入。
