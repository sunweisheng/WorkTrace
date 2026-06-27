# WorkTrace 详细设计

## 1. 文档目标

本文档说明 WorkTrace 当前已经落地的首版实现边界、执行流程、接口契约、数据模型和存储格式。

本文档面向当前可运行的单日处理链路，重点是描述“现在代码真实怎么工作”，而不是未来规划。

## 2. 产品目标与范围

### 2.1 当前产品目标

WorkTrace 的目标是：从飞书中提取“目标日期内本人至少发过 1 条消息”的会话内容，结合 LLM 做语义分析，生成事项级工作事件清单，并将结果写入本地 Markdown 文件，方便后续回顾、整理日报素材和沉淀工作记录。

### 2.2 当前范围

当前已经落地的能力包括：

- 手动指定日期执行单日处理
- 基于 `lark-cli` 读取飞书会话与消息
- Python 侧消息过滤、切片、扩窗、校验和存储
- 会话级首轮事件提炼
- 同一会话内按 `context_requests` 自动补上下文并重跑
- 同日候选事件跨会话分组归并
- 本地 Markdown 覆盖写入

### 2.3 当前不做

当前版本仍不处理：

- 真正的定时调度
- 跨天事项合并
- 图片 OCR
- 完整飞书文档正文预抓取
- 汇总报表或日报视图

## 3. 当前确定性业务规则

当前实现以以下规则为准：

- 分析范围限定为“目标日期内本人至少发过 1 条消息的会话”。
- “当天”按 `Asia/Shanghai` 时区的 `00:00:00` 到 `23:59:59` 切分。
- 输出结果只保留日期，不保留时间。
- 与工作无关的聊天内容需要自动忽略。
- 同一事项允许跨多个会话合并，但只在同一天内合并。
- 存储采用“按年月目录组织、每天一个 Markdown 文件”的模式。
- 同一天重复执行采用覆盖策略。
- 首版最终输出字段固定为 `date`、`event_id`、`topic`、`content`。
- 最终 Markdown 只保留事件清单，不再生成管理者总结。
- 当天无本人发言时，按“成功空覆盖”处理。
- 获取当前飞书 user 身份统一通过 `ChatSource.get_self_identity()` 完成。
- 首轮每个候选事项必须且只能来自单个会话切片。
- 补前文和补后文的边界统一由 `target_message_ids` 决定。
- 附件补读结果采用结构化 `AttachmentTextBlock`。

## 4. 设计原则

- 保守高准确，宁可少记也不误记。
- LLM 只负责语义理解，不负责确定性流程控制和数据计算。
- 正式日处理主流程默认不长期落盘原始聊天内容，最终只保留结构化事件清单。
- 仓库根目录同时作为通用脚本项目根目录和 Codex skill 根目录。
- 运行流程、能力装配和存储都通过抽象层解耦，便于后续扩展。

## 5. 总体架构

### 5.1 当前五层结构

WorkTrace 当前采用五层结构：

1. Codex Skill 层
   负责接收用户在 Codex 对话中的触发请求，解析目标日期，调用底层脚本完成处理，并向用户返回执行摘要。

2. Runner / Orchestrator 层
   负责组织单日执行流程，串联聊天抓取、消息过滤、会话切片、首轮分析、会话内重跑、跨会话合并和 Markdown 写入。

3. Source / Resolver / Analyzer / Store 抽象层
   定义统一接口，通过工厂创建具体实现，降低飞书、LLM 调用链路和 Markdown 存储之间的耦合。

4. Python 实现层
   实现首版具体能力，包括飞书消息抓取、消息预处理、附件补读、事件物化、Markdown 写入和异常处理。

5. 外部依赖层
   当前依赖 `lark-cli`、analyzer 调用通道和本地 Markdown 文件。

### 5.2 当前仓库结构

当前仓库结构如下：

```text
.
├── SKILL.md
├── README.md
├── docs/
├── data/
├── src/
└── tests/
```

目录职责如下：

- `SKILL.md`：Codex skill 入口说明
- `src/`：核心通用逻辑
- `tests/`：单元测试与集成测试
- `docs/`：设计和架构文档
- `data/`：生成的 Markdown 结果文件

### 5.3 Skill 与脚本的关系

仓库根目录就是 skill 根目录，因此安装时应把整个仓库链接到 `~/.codex/skills/<skill-name>`，而不是只链接子目录。

根目录 `SKILL.md` 负责约束 Codex 如何调用 `src/` 中的稳定 Python 入口，业务实现逻辑仍放在 Python 代码中。

## 6. 运行入口与职责边界

### 6.1 Python 入口契约

当前固定 Python 入口为：

- `python -m src.worktrace.cli --date YYYY-MM-DD`

当前约束如下：

- `--date` 为必填参数
- 日期格式固定为 `YYYY-MM-DD`
- 业务时区固定为 `Asia/Shanghai`
- `stdout` 返回 `DailyRunResult` 的 machine-readable JSON
- `stderr` 用于输出日志

退出码约定如下：

- `0`：执行成功，包括成功空覆盖
- `1`：业务执行失败
- `2`：输入参数不合法

`stdout` 返回的执行摘要至少包含：

- `target_date`
- `status`
- `conversation_count`
- `message_count`
- `slice_count`
- `batch_count`
- `event_count`
- `skipped_slice_count`
- `warning_count`
- `output_path`
- `error_summary`

### 6.2 Python 负责的内容

当前 Python 负责所有确定性工作，包括：

- 读取目标日期参数
- 获取本人身份信息
- 找出目标日期内本人发过消息的会话
- 拉取这些会话的当日消息
- 过滤明显无效消息
- 按会话构造 `ConversationSlice`
- 根据 LLM 返回的 `context_requests` 补前文、后文或附件正文
- 组织首轮分析输入和 retry 输入
- 调用 analyzer 做会话级首轮分析和全日跨会话分组
- 校验 analyzer 返回的 JSON 结构
- 根据跨会话分组结果物化 `MergedEventDraft`
- 基于最终事件生成稳定的 `event_id`
- 覆盖写入 Markdown 文件
- 控制日志、异常和重试

### 6.3 LLM 负责的内容

当前 LLM 负责非确定性的语义工作，包括：

- 判断内容是否属于工作事项
- 在单个会话内提炼 `candidate_events`
- 判断是否需要补充更早消息、更晚消息或附件正文
- 在全日范围内判断哪些候选事项属于同一真实工作事件
- 提炼事件主题
- 总结事件内容
- 忽略与工作无关的内容

### 6.4 LLM 不负责的内容

LLM 不负责以下工作：

- 数据计算
- 北京时间日边界切分
- 去重逻辑
- 身份识别
- Markdown 读写
- 覆盖策略
- 稳定 ID 生成

## 7. 单日执行主流程

当前单日执行流程如下：

1. 接收目标日期
2. 获取当前飞书 user 身份
3. 找出“目标日期内本人至少发送过 1 条消息”的会话
4. 拉取这些会话的当日消息
5. Python 过滤明显无效消息
6. 按会话构造 `ConversationSlice`
7. 对每个 `ConversationSlice` 调用 `_analyze_conversation_slice_with_retry(...)`
8. 首轮返回 `context_requests` 时，在同一会话内自动补上下文并重跑
9. 汇总全日所有 `SourceBackedEventDraft`
10. 调用 `merge_day_candidates(...)` 做跨会话分组
11. Python 物化 `MergedEventDraft`
12. 调用 `build_work_events(...)` 构建最终 `WorkEvent`
13. 覆盖写入当天 Markdown 文件
14. 输出运行摘要

主流程代码位于 [runner.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/runner.py)。

## 8. 会话识别与切片规则

本节描述从原始消息到 `ConversationSlice` 的当前规则。

### 8.1 目标会话识别

- 只处理“目标日期内本人至少发送过 1 条消息”的会话
- 先找目标会话，再抓取这些会话的当日消息
- 不在会话发现阶段引入“围观但未发言”的会话

### 8.2 当前切片粒度

当前实现已经采用“按会话构造切片”的策略，而不是早期的多锚点局部切片策略。

也就是：

- `1 个会话 = 1 个 ConversationSlice`
- 该会话中本人当日发出的消息会作为 `anchor_message_ids`

实现位于 [conversation_first_pass.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/pipeline/conversation_first_pass.py)。

### 8.3 当前基础裁剪

当前如果单个会话消息数超过 `slice_base_limit`，会执行基础裁剪。

保留优先级为：

1. 锚点消息本身
2. 被锚点直接 reply / quote 的上游消息
3. 直接 reply / quote 锚点的消息
4. 其他带 reply / quote 关系的消息
5. 普通消息
6. 系统消息

同优先级下，距离锚点越近越优先保留。

### 8.4 当前扩窗策略

若首轮分析后模型返回 `context_requests`，系统会在同一会话内自动扩窗。

当前支持：

- `earlier_messages`
- `later_messages`
- `attachment_text`

扩窗逻辑位于 [context_expansion.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/pipeline/context_expansion.py)。

### 8.5 当前停止条件

会话内扩窗重跑会在以下情况停止：

- 本轮没有 `context_requests`
- 达到 `slice_retry_limit`
- 扩窗后消息和附件签名未变化

## 9. 抽象接口与数据模型

### 9.1 抽象接口

当前定义四类核心接口和一个编排器：

```text
ChatSource
- get_self_identity() -> SelfIdentity
- list_target_conversations(target_date, self_identity) -> list[ConversationRef]
- fetch_conversation_messages(target_date, conversation_ids) -> list[NormalizedMessage]
- fetch_related_messages(conversation_id, target_message_ids, direction, limit) -> list[NormalizedMessage]

ContentResolver
- load_attachment_text_if_needed(message, attachment_ids, hint) -> list[AttachmentTextBlock] | None

Analyzer
- analyze_batch(target_date, batch_input: AnalysisBatch) -> BatchAnalysisResult
- merge_day_candidates(target_date, candidates) -> CrossConversationGroupResult

EventStore
- replace_day(target_date, events) -> StoreWriteResult
- read_day(target_date) -> DayDocument | None

DailyTraceRunner
- run(target_date) -> DailyRunResult
```

当前 runner 主流程实际依赖的是：

- `ChatSource`
- `ContentResolver`
- `Analyzer.analyze_batch(...)`
- `Analyzer.merge_day_candidates(...)`
- `EventStore`

`Analyzer` 中仍保留了一些旧实验接口，但它们已不属于当前主流程。

### 9.2 当前具体实现

当前已存在的实现包括：

- `FeishuCliChatSource`
- `FeishuMessageContentResolver`
- `OnlineLLMAnalyzer`
- `CodexAnalyzer`
- `MarkdownEventStore`

依赖组装通过工厂层完成。

### 9.3 关键数据模型

#### 9.3.1 `SelfIdentity`

表示当前飞书用户身份，至少包含：

- `open_id`
- `display_name`
- `source`

#### 9.3.2 `ConversationRef`

表示一个待处理会话的轻量引用，至少包含：

- `conversation_id`
- `conversation_name`

#### 9.3.3 `NormalizedMessage`

表示统一格式的消息对象，当前固定字段包括：

- `conversation_id`
- `conversation_name`
- `message_id`
- `sender_open_id`
- `sender_name`
- `send_time`
- `message_type`
- `text`
- `reply_to_message_id`
- `quote_message_id`
- `links`
- `attachments`
- `is_system`

#### 9.3.4 `AttachmentTextBlock`

表示某个附件被补读后的结构化文本块，当前包含：

- `attachment_id`
- `message_id`
- `file_name`
- `text`

#### 9.3.5 `ConversationSlice`

表示按会话构造的分析输入单元，当前包含：

- `slice_id`
- `conversation_id`
- `conversation_name`
- `anchor_message_ids`
- `in_day_message_ids`
- `messages`
- `attachment_texts`

#### 9.3.6 `AnalysisBatch`

表示一批待分析的切片集合，当前包含：

- `target_date`
- `batch_id`
- `retry_round`
- `estimated_tokens`
- `slices`

当前主流程里：

- 首轮是单会话单 batch
- 重跑时会构造只包含该会话的 retry batch

#### 9.3.7 `ContextRequest`

表示 LLM 提出的补充请求，当前至少包含：

- `slice_id`
- `request_type`
- `target_message_ids`
- `target_attachment_ids`
- `reason`
- `limit`

当前 `request_type` 支持：

- `earlier_messages`
- `later_messages`
- `attachment_text`

#### 9.3.8 `SourceBackedEventDraft`

表示会话级首轮分析产出的候选事项，当前包含：

- `draft_id`
- `date`
- `topic`
- `content`
- `source_message_ids`
- `source_conversation_id`
- `source_slice_id`
- `confidence`

#### 9.3.9 `BatchAnalysisResult`

表示会话级分析返回结果，当前至少包含：

- `candidate_events`
- `context_requests`

#### 9.3.10 `CrossConversationGroupResult`

表示全日跨会话分组结果，当前包含：

- `groups`

每个 group 为：

- `group_id`
- `draft_ids`

#### 9.3.11 `MergedEventDraft`

表示跨会话分组后、由 Python 物化得到的事件草稿，当前至少包含：

- `date`
- `topic`
- `content`
- `source_message_ids`
- `source_conversation_ids`

#### 9.3.12 `WorkEvent`

表示最终写入 Markdown 的事件对象，当前包含：

- `date`
- `event_id`
- `topic`
- `content`

#### 9.3.13 `DayDocument`

表示某一天最终落盘的数据对象，当前包含：

- `date`
- `events`
- `generated_at`

当前已经不再包含任何总结层字段。

## 10. 分析与合并链路

### 10.1 会话级首轮分析

当前首轮采用：

- `1 个会话 = 1 次 LLM`

每轮输入为单个 `ConversationSlice`，输出为：

- `candidate_events`
- `context_requests`

### 10.2 会话内扩窗重跑

当前 `_analyze_conversation_slice_with_retry(...)` 负责：

- 对单个会话执行首轮分析
- 按 `context_requests` 自动扩窗
- 对扩展后的同一会话重跑
- 在最终 unresolved 时记录 warning

### 10.3 跨会话分组

当前所有会话提炼完成后，会把全日 `candidate_events` 一次性送入：

- `merge_day_candidates(...)`

LLM 返回：

- `CrossConversationGroupResult`

Python 再调用：

- `materialize_grouped_merged_drafts(...)`

物化真正的 `MergedEventDraft`。

### 10.4 最终事件构建

当前由 Python 调用：

- `build_work_events(...)`

基于合并后的草稿生成最终 `WorkEvent`，并稳定生成 `event_id`。

## 11. 存储格式与覆盖策略

### 11.1 当前存储方式

当前存储采用：

- 按年月目录组织
- 每天一个 Markdown 文件
- 同日重跑直接覆盖

### 11.2 当前 Markdown 结构

当前 Markdown 文件结构为：

1. front matter
2. `# WorkTrace YYYY-MM-DD`
3. `## 我的日报`
4. 逐条日报小节
5. `## 事项列表`
6. 逐条事件块

当前已不再写入：

- 管理者总结
- “给上级汇报的当日总结”段落

### 11.3 当前事件块字段

每条事件当前写入：

- `date`
- `event_id`
- `topic`
- `content`

实现位于 [markdown.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/stores/markdown.py)。

## 12. 错误处理与运行结果

### 12.1 当前失败来源

当前主流程会处理以下类型的失败：

- 聊天源失败
- analyzer 协议失败
- 存储写入失败
- 数据校验失败

### 12.2 当前 warning 来源

当前 warning 主要来自：

- 会话在重跑上限后仍缺上下文
- 扩窗后没有拿到新信息
- 事件合并或敏感过滤阶段的附加提示

### 12.3 当前运行结果

当前 `DailyRunResult` 会返回：

- 目标日期
- 会话数
- 消息数
- slice 数
- analyzer 调用次数
- 事件数
- skipped slice 数
- warning 数
- 状态
- 输出路径
- 错误摘要

## 13. 当前状态总结

截至当前版本，WorkTrace 的主链路已经是：

1. 按会话组织首轮 LLM 提炼
2. 在单会话内按需扩窗和补附件正文
3. 汇总全日候选事件并做跨会话分组
4. Python 物化最终事件
5. 直接写 Markdown 事件清单

当前详细设计文档应与以下分项文档配套阅读：

- [conversation-slice-retry-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/conversation-slice-retry-design.md)
- [cross-conversation-merge-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/cross-conversation-merge-design.md)
- [markdown-output-simplification-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/markdown-output-simplification-design.md)
