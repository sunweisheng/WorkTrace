# WorkTrace 锚点优先多轮识别设计

> 这是一份未来演进设计稿，不是当前主流程实现说明。当前主流程仍以会话级 `ConversationSlice` 为主，锚点链路主要存在于 `anchor_experiment.py`。

## 1. 文档目标

本文档用于定义 WorkTrace 下一阶段的演进方案：将当前“先构造较大切片、再统一分析”的流程，调整为“锚点优先、按需扩窗、局部缓存、最终再合并”的多轮识别链路。

本文档不是对首版实现的否定，而是为性能优化和可重复运行成本优化提供一条新的主线设计。当前已落地版本仍可继续工作；后续改造建议按本文档逐步替换。

## 2. 设计动机

当前首版流程能够稳定跑通，但在真实日期上已暴露出几个问题：

- 首轮每个分析批次仍然承载较重的聊天上下文理解负担。
- 同一天重复执行时，几乎所有 `slice` 都会重新送给 LLM。
- 附件文本和上下文扩展虽然已经按需触发，但触发时机偏晚，初始输入仍偏大。
- 日级 `merge` 需要让 LLM 再次阅读全量候选事件，成本较高。

这些问题的共同根因，不是单个调用姿势不对，而是分析单元过大、复用粒度过粗。

因此，下一阶段的核心目标是：

1. 把首轮分析单元从“大切片”收缩为“锚点窗口”。
2. 把补前文、补后文、补附件从“异常补救”升级为“多轮按需扩展机制”。
3. 把缓存粒度从“整天结果”收缩为“锚点级结果”。
4. 把最终合并范围限制为“明确需要跨锚点 / 跨会话合并”的候选事项。

## 3. 核心原则

### 3.1 锚点优先，而不是切片优先

Python 先识别目标日期内本人发言锚点，并围绕锚点构造最小分析窗口。

首轮不再先把多个相邻锚点强合并成较大 `ConversationSlice` 再送 LLM，而是优先让每个锚点窗口先独立接受一次语义判断。

### 3.2 多轮识别，而不是一次性喂足上下文

首轮输入只提供：

- 锚点消息
- 小范围前后文
- 1 层 reply / quote 直连关系
- 附件 / 链接元信息

如果模型认为信息已经足够，则直接产出结果并结束该锚点。

如果模型认为信息不足，则通过结构化请求继续索取：

- 更早消息
- 更晚消息
- 指定附件文本
- 指定飞书文档 / wiki 链接正文
- 可选：同会话内相邻锚点窗口

### 3.3 缓存优先复用已完成锚点

锚点级分析结果一旦形成稳定输出，应优先写入本地缓存。后续重复运行同一天时，如果锚点窗口输入未发生变化，则直接复用，不再次调用 LLM。

### 3.4 合并阶段后移且收窄

跨锚点、跨会话、跨批次的事项合并继续保留，但只对“首轮或二轮明确标记为可能需要合并”的候选事项再做最终 LLM 合并，而不是对整天全部候选统一做重型 merge。

## 4. 新旧流程对比

### 4.1 当前流程

1. 按本人消息构造锚点簇
2. 基于窗口和链路规则合并成 `ConversationSlice`
3. 按批送 LLM 做候选事项提取
4. 如需补上下文，再对整个 `slice` 重跑
5. 汇总所有候选事项
6. 对整天全部候选再做一次日级 merge

### 4.2 新流程

1. 按本人消息构造原始锚点
2. 按规则合并成“锚点单元”而不是大切片
3. 围绕每个锚点单元构造最小首轮窗口
4. 对锚点单元做首轮识别
5. 仅对需要更多信息的锚点做扩窗 / 补附件
6. 将已完成锚点结果写入缓存
7. 汇总所有已完成锚点结果
8. 仅对“可能跨锚点 / 跨会话合并”的候选做最终合并

## 5. 新的处理单元

### 5.1 `AnchorUnit`

新增 `AnchorUnit` 作为新的首轮分析基础单元，建议字段：

- `anchor_unit_id`
- `conversation_id`
- `conversation_name`
- `anchor_message_ids`
- `in_day_message_ids`
- `base_message_ids`
- `messages`
- `reply_relation_ids`
- `quote_relation_ids`
- `attachment_refs`

说明：

- `AnchorUnit` 表示围绕一个锚点或一个非常小的锚点簇构造出的最小分析窗口。
- 与当前 `ConversationSlice` 不同，它不承担“预先尽量包住完整事项”的职责。
- 它只承担“给模型做首轮判断所需的最小可靠输入”的职责。

### 5.2 `AnchorAnalysisState`

新增锚点分析状态对象，建议字段：

- `anchor_unit_id`
- `target_date`
- `status`
- `pass_index`
- `message_window_before`
- `message_window_after`
- `included_message_ids`
- `included_attachment_ids`
- `needs_more_context`
- `needs_attachment_text`
- `needs_cross_anchor_merge`
- `completed_event_drafts`
- `warnings`

状态枚举建议：

- `pending`
- `completed`
- `needs_more_context`
- `needs_attachment_text`
- `not_work_related`
- `uncertain`
- `failed`
- `skipped`

该对象只在运行期与缓存中使用，不直接写入最终 Markdown。

## 6. 首轮最小窗口规则

### 6.1 锚点构造

保留“本人消息为锚点”的基本规则，但弱化“先大范围合并”的行为。

建议规则：

- 同一会话内，相邻本人消息之间若不存在其他发送人的消息，可先形成一个小锚点簇。
- 若两个本人消息簇之间虽然很近，但中间存在他人消息，不再默认强合并为大切片。
- 是否需要继续联动理解，优先交由后续多轮识别决定。

### 6.2 首轮窗口

建议首轮窗口配置比当前切片明显更小，例如：

- `anchor_context_before = 8`
- `anchor_context_after = 8`
- 保留锚点自身全部消息
- 保留 1 层 reply / quote 直连消息

首轮窗口目标不是一次讲全，而是让模型先判断：

- 这是工作事项吗
- 这是完整事项吗
- 还缺什么信息

### 6.3 首轮不预读附件正文

首轮只注入附件元信息：

- `attachment_id`
- `file_name`
- `mime_type`

不默认载入附件正文。只有模型明确请求某个附件文本时，才交由 `ContentResolver` 补读。

## 7. 多轮扩展机制

### 7.1 首轮输出协议

首轮不再只返回“候选事项 + 上下文请求”，而是返回更明确的锚点识别状态。建议协议如下：

- `candidate_events`
- `context_requests`
- `anchor_status`
- `needs_cross_anchor_merge`

其中：

- `candidate_events` 每项除标题、内容、来源消息外，还必须包含 `action_label`、`object_hint`、`retention_reason`、`retention_detail`；普通约时间、确认开会、互通信息、泛泛完成审核/审批但没有具体对象和结论的内容不应输出。
- `anchor_status` 取值建议为：
  - `completed`
  - `needs_more_context`
  - `needs_attachment_text`
  - `not_work_related`
  - `uncertain`

其中约束建议固定如下：

- `completed`：当前锚点已经得到足够稳定的候选事项或稳定空结果
- `needs_more_context`：需要补更早或更晚消息
- `needs_attachment_text`：需要补附件正文
- `not_work_related`：当前锚点可明确判定为非工作事项
- `uncertain`：当前信息不足，但也未形成明确补充请求；通常应进入保守人工或后续兜底策略

### 7.2 扩展请求类型

当前实现已经支持 `earlier_messages / later_messages / attachment_text / linked_file_text` 四类请求；后续如要继续扩协议面，建议仍保持保守，不急于引入更多类型。

原因是：

- 更早消息、更多后文、附件正文、飞书文档 / wiki 正文，已经覆盖当前最主要的信息缺口。
- “相邻锚点并看”可以先由 Python 转成追加消息窗口，而不是立即引入新的协议类型。

### 7.3 扩展执行规则

对于单个 `AnchorUnit`：

1. 只要模型返回 `completed`，立即结束该锚点。
2. 只要模型返回 `not_work_related`，立即结束并缓存空结果。
3. 只要模型返回 `needs_more_context` 或合法 `context_requests`，才执行扩展。
4. 每轮扩展后，只重跑该锚点，不影响其他锚点。
5. 达到扩展轮次上限仍未收敛时，标记为 `skipped` 或 `failed`。

### 7.4 每轮扩展必须只增不减

为保证缓存和可解释性，扩展后的分析输入只能在上一轮基础上追加：

- 新增更早消息
- 新增更晚消息
- 新增附件文本
- 新增飞书文档 / wiki 正文

不允许每轮重新洗牌式地替换输入窗口。

## 8. 本地缓存设计

### 8.1 缓存目标

缓存的目标不是保存原始聊天，而是保存“给定锚点输入下的已完成识别结果”。

### 8.2 缓存粒度

缓存粒度固定为锚点级，而不是整批级、整天级。

### 8.3 缓存键

建议缓存键由以下部分组成：

- `target_date`
- `anchor_unit_id`
- `input_fingerprint`
- `prompt_version`
- `schema_version`
- `analyzer_key`

其中 `input_fingerprint` 至少覆盖：

- 当前窗口内消息 ID 及稳定顺序
- 消息文本 / 关键字段 hash
- 已注入附件文本 hash
- 已注入飞书文档 / wiki 正文 hash
- 当前扩展边界

### 8.4 缓存值

缓存值建议包含：

- `status`
- `pass_index`
- `candidate_events`
- `context_requests`
- `needs_cross_anchor_merge`
- `included_message_ids`
- `included_attachment_ids`
- `included_link_ids`
- `created_at`
- `prompt_version`
- `schema_version`

### 8.5 缓存目录建议

建议新增：

```text
data/cache/
└── anchors/
    └── YYYY/
        └── MM/
            └── YYYY-MM-DD/
```

单个锚点可按 `anchor_unit_id` 或 `input_fingerprint` 存储 JSON 文件。

### 8.6 全量重提开口

必须保留人工触发的“忽略缓存并全量重跑”开口。建议后续 CLI 增加：

- `--rebuild-day`
- `--ignore-cache`

该能力用于应对：

- 飞书重新提取口径变化
- prompt / schema 大改
- 历史错误修复后需要整日重算

## 9. 最终合并策略

### 9.1 不再默认对全部候选做重型 merge

最终 merge 只处理满足以下任一条件的候选事项：

- `needs_cross_anchor_merge = true`
- 多个锚点候选共享高度重叠的 `source_message_ids`
- 多个候选引用同一文档链接且主题接近
- Python 通过确定性规则发现它们来自同一会话的连续锚点链

### 9.2 两段式合并

建议把最终合并拆成两段：

1. Python 确定性预分桶
2. LLM 对桶内候选做最终语义合并

预分桶依据可包括：

- 会话 ID
- 文档链接集合
- 共享消息 ID
- 时间接近性

这样可以显著缩小最终 merge 输入规模。

## 10. 与当前代码的迁移方式

### 10.1 保留现有抽象边界

以下边界建议保留：

- `ChatSource`
- `ContentResolver`
- `Analyzer`
- `EventStore`
- `DailyTraceRunner`

迁移重点应放在：

- `pipeline/slicing.py`
- `pipeline/context_expansion.py`
- `runner.py`
- `analyzers/prompts.py`
- 新增缓存模块

### 10.2 分阶段迁移

建议按以下顺序改造：

1. 新增 `AnchorUnit` 与锚点级窗口构造逻辑
2. 新增锚点级缓存读取与写入
3. 让首轮分析从 `ConversationSlice` 切换到 `AnchorUnit`
4. 保留现有 `context_requests` 协议，接入多轮扩展
5. 缩小最终 merge 输入范围
6. 视结果再决定是否彻底废弃旧 `ConversationSlice` 主链路

### 10.3 过渡兼容建议

过渡期可同时保留两套策略：

- `slice_first`
- `anchor_first`

通过配置切换，便于在真实数据上比较：

- 成功率
- 总耗时
- LLM 调用次数
- 候选事项覆盖率
- 最终事件质量

## 11. 新的运行指标

为评估新方案是否真的更优，建议新增以下日志或统计项：

- `anchor_unit_count`
- `anchor_cache_hit_count`
- `anchor_cache_miss_count`
- `anchor_completed_first_pass_count`
- `anchor_context_expanded_count`
- `anchor_attachment_expanded_count`
- `final_merge_candidate_count`

重点观察：

- 首轮直接完成的锚点比例
- 需要扩窗的锚点比例
- 缓存命中率
- 最终 merge 候选规模是否显著下降

## 12. 成功判定

若新方案满足以下条件，即可视为方向正确：

1. 同一日期重复运行时，大部分锚点命中缓存。
2. 首轮完成的锚点占比明显高于需要扩展的锚点。
3. 最终 merge 输入规模明显小于当前“整天全量候选 merge”。
4. 真实总耗时显著下降，同时事件质量不明显变差。

## 13. 结论

WorkTrace 下一阶段最值得推进的，不是简单替换某种 CLI 调用姿势，而是把分析主链从“切片优先”调整为“锚点优先、多轮扩窗、局部缓存、最后合并”。

这一路线兼容当前系统边界，也最符合聊天分析任务“上下文需求不均匀、重复运行复用价值高、最终跨会话合并只占少数”的实际特征。
