# WorkTrace 详细设计

## 1. 文档目标

本文档用于约束 WorkTrace 首版的实现边界、执行流程、接口契约、数据模型和存储格式，目标是让开发时优先依据统一规则落地，而不是在代码中二次发明口径。

本文档面向首版实现，关注“单日处理链路可稳定跑通”。若未来新增自动调度、更多聊天源或更多存储后端，应在保持本文抽象边界稳定的前提下扩展。

## 2. 产品目标与范围

### 2.1 产品目标

WorkTrace 的目标是：每天从飞书中提取“目标日期内本人至少发过 1 条消息”的会话内容，结合 Codex 做语义分析，生成事项级工作事件与一段可直接向上级汇报的当日总结，并将结果写入本地 Markdown 文件，方便后续回顾、整理日报素材和沉淀工作记录。

### 2.2 首版范围

首版只支持手动指定日期执行，重点是把单日处理链路跑通，不实现真正的自动调度。架构设计需要兼顾后续扩展，便于逐步增加自动调度、更多聊天源、更多存储后端以及其他 Agent 封装。

### 2.3 首版不做

- 真正的定时调度
- 跨天事项合并
- 图片 OCR
- 飞书文档正文抓取
- 汇总报表或日报视图

## 3. 首版确定性决策

本节汇总首版已经拍板的业务规则，后续实现以本节为最高优先级准绳。

- 分析范围限定为“目标日期内本人至少发过 1 条消息的会话”，不采用更宽泛的“参与过讨论”口径。
- 消息读取策略为：先确定目标会话，再抓取这些会话的当日消息，随后围绕本人发言锚点在本地按消息条数和回复关系裁剪上下文。
- 当 LLM 请求补前文或后文时，由 `ChatSource.fetch_related_messages(...)` 统一承接。
- 事件粒度为事项级，不按单条消息或单次发言分别存储。
- 同一事项允许跨多个会话合并，但只在同一天内合并，不做跨天合并。
- “当天”按北京时间 `00:00:00` 到 `23:59:59` 切分。
- 输出结果只保留日期，不保留时间。
- 与工作无关的聊天内容需要自动忽略。
- `result` 字段允许为空字符串，用于表示当天只有推进过程、没有明确结果。
- 若事项推进过程中出现与该事项直接相关的飞书文档链接，可写入 `content` 字段一并保留。
- 存储采用“按年月目录组织、每天一个 Markdown 文件”的模式。
- 同一天重复执行采用覆盖策略，而不是追加或保留多版本。
- 首版最终输出字段固定为 `date`、`event_id`、`topic`、`content`、`result`。
- 每日输出文件必须额外包含一段“给上级汇报的当日总结”，用于直接复用。
- 当天无本人发言时，按“成功空覆盖”处理，而不是失败。
- 获取当前飞书 user 身份统一通过 `ChatSource.get_self_identity()` 完成。
- 首轮每个候选事项必须且只能来自单个切片、单个会话；跨切片和跨会话合并统一放到次轮处理。
- 补前文和补后文的边界统一由 `target_message_ids` 决定，不再引入第二套补充边界语义。
- 附件补读结果采用结构化 `AttachmentTextBlock`，不使用无来源信息的纯文本数组。
- 补充重跑阶段的统一输入硬上限配置项为 `max_model_input_tokens`，按 token 计量。

## 4. 设计原则

- 保守高准确，宁可少记也不误记。
- LLM 只负责语义理解，不负责确定性流程控制和数据计算。
- 原始聊天内容不长期落盘，只保留结构化事件结果和每日总结。
- 仓库根目录同时作为通用脚本项目根目录和 Codex skill 根目录。
- 所有可替换能力通过抽象工厂统一管理，便于未来扩展实现。

## 5. 总体架构

### 5.1 五层结构

WorkTrace 首版采用五层结构：

1. Codex Skill 层  
   负责接收用户在 Codex 对话中的触发请求，解析目标日期，调用底层脚本完成处理，并向用户返回执行摘要。

2. Runner / Orchestrator 层  
   负责组织单日执行流程，串联聊天抓取、上下文裁剪、批量分析、结果校验、总结生成和 Markdown 写入。

3. Source / Resolver / Analyzer / Store 抽象层  
   定义统一接口，通过抽象工厂创建具体实现，降低飞书、Codex、Markdown 存储等组件的耦合。

4. Python 实现层  
   实现首版具体能力，包括飞书消息抓取、消息预处理、链接与附件解析、批量任务组装、Markdown 写入和异常处理。

5. 外部依赖层  
   首版依赖 `lark-cli`、Codex 和本地 Markdown 文件。

### 5.2 仓库结构建议

建议仓库逐步演进为如下结构：

```text
.
├── SKILL.md
├── README.md
├── docs/
│   └── detailed-design.md
├── data/
├── src/
└── tests/
```

目录职责约定如下：

- `SKILL.md`：位于仓库根目录，作为 Codex skill 入口说明。
- `src/`：核心通用逻辑，供 Codex skill 和未来其他入口复用。
- `tests/`：单元测试与集成测试。
- `docs/`：设计和架构文档。
- `data/`：生成的 Markdown 结果文件。

### 5.3 Skill 与脚本的关系

仓库根目录就是 skill 根目录，因此安装时应把整个仓库链接到 `~/.codex/skills/<skill-name>`，而不是只链接一个子目录。

这种结构下，根目录 `SKILL.md` 可以直接约束 Codex 在仓库内调用 `src/` 中的脚本入口。推荐做法是通过稳定的 Python 入口命令调用核心逻辑，而不是在 `SKILL.md` 中堆业务实现细节。

## 6. 运行入口与职责边界

### 6.1 Python 入口契约

为避免 `SKILL.md`、CLI 和测试各自定义不同调用方式，首版固定 Python 入口契约如下：

- 入口命令固定为 `python -m src.worktrace.cli --date YYYY-MM-DD`
- `--date` 为必填参数，首版不提供“默认今天”
- 日期格式固定为 `YYYY-MM-DD`
- 业务时区固定为 `Asia/Shanghai`
- 标准输出 `stdout` 固定返回 `DailyRunResult` 的 machine-readable JSON 序列化结果
- 标准错误 `stderr` 用于输出日志

退出码约定如下：

- `0`：执行成功，包括“成功空覆盖”
- `1`：业务执行失败
- `2`：输入参数不合法

`stdout` 返回的执行摘要至少包含以下字段：

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

补充约束：

- `output_path` 在成功写入文件时返回绝对路径字符串。
- `output_path` 在 `failed` 或 `invalid_input` 状态下返回 `null`。
- `error_summary` 无错误时返回空字符串 `""`，不返回 `null`。
- CLI 在进入正式业务流程前必须执行一次轻量 preflight 检查。
- preflight 检查至少覆盖：Python 版本、`lark-cli` 是否存在、`lark-cli` 当前登录身份是否为可读取消息的飞书 user、Codex 调用能力是否可用、结果目录是否可创建或可写、业务时区配置是否可用。
- “Codex 调用能力是否可用” 不得仅通过命令存在性判断，必须执行一次最小真实探测调用。
- 最小真实探测调用至少需要验证：可成功发起一次结构化请求、可在预设超时时间内收到合法 machine-readable JSON 响应。
- Codex 最小探测失败时，`error_summary` 应尽量区分为以下可读原因之一：命令不存在、未登录或无权限、网络或服务不可达、返回结果不是合法 JSON、探测调用超时。
- 因命令缺失、版本不满足、身份未登录或关键依赖不可用导致的 preflight 失败，统一归类为 `failed`，并在 `error_summary` 中返回可读原因。
- `--date` 缺失或格式不合法这类参数问题仍归类为 `invalid_input`。

### 6.2 Python 负责的内容

Python 脚本负责所有确定性工作，包括：

- 读取目标日期参数。
- 获取本人身份信息。
- 找出目标日期内本人发过消息的会话。
- 拉取这些会话的当日消息。
- 识别本人发言锚点。
- 裁剪与合并上下文切片。
- 预处理文本、卡片、链接和附件元数据。
- 根据 LLM 返回的 machine-readable 请求补读附件文本或补充上下文。
- 按批次组织分析输入。
- 调用 Codex 执行首轮候选事件分析和次轮日内合并分析。
- 校验 Codex 返回的 JSON 结构。
- 在次轮合并完成后，基于日期和源消息 ID 集合生成稳定的 `event_id`。
- 基于最终 `WorkEvent` 列表请求 Codex 生成上级汇报总结。
- 覆盖写入 Markdown 文件。
- 控制临时数据写入、校验、替换与清理。
- 控制日志、异常和重试。

### 6.3 Codex 负责的内容

Codex 只负责非确定性的语义工作，包括：

- 判断内容是否属于工作事项。
- 在首轮分析中将单个切片内容归并成带来源的候选事项。
- 在次轮分析中对同一天的候选事项做跨会话、跨批次合并。
- 提炼事件主题。
- 总结事件内容。
- 在提炼事件内容和总结时，避免输出聊天中涉及吵架、调情、辱骂或其他情绪发泄的具体言语，以保护个人隐私。
- 从消息、卡片或链接中识别与事项直接相关的飞书文档链接，并在 `content` 中保留。
- 提炼事件结果，结果允许为空。
- 判断是否需要补充附件、前文或后文。
- 忽略与工作无关的内容。
- 基于最终事件列表生成一段简洁、可直接复用的上级汇报总结。

### 6.4 Codex 不负责的内容

Codex 不负责以下工作：

- 数据计算
- 北京时间日边界切分
- 去重逻辑
- 身份识别
- Markdown 读写
- 覆盖策略
- 稳定 ID 生成

## 7. 单日执行主流程

单日执行流程按以下顺序进行：

1. 接收目标日期。
2. 获取当前飞书 user 身份。
3. 找出“目标日期内本人至少发送过 1 条消息”的会话。
4. 拉取这些会话的当日消息。
5. 按北京时间对消息时间做归一化，并按稳定顺序排序。
6. 根据本人发言定位锚点。
7. 围绕锚点裁剪候选切片，允许多个锚点窗口重叠。
8. 将切片整理为首轮候选事项切片。
9. 本地粗过滤零风险无效消息。
10. 按 token 预算拆成多个首轮分析批次。
11. 批量调用 Codex 做首轮候选事项分析。
12. 处理 `context_requests`，按需补附件、前文或后文，并以 `slice` 为最小单位重试对应分析输入。
13. 汇总所有 `SourceBackedEventDraft`。
14. 调用 Codex 做同一天内的次轮合并分析。
15. 对次轮结果做本地校验，并生成 `event_id`。
16. 基于最终 `WorkEvent` 列表调用 Codex 生成给上级汇报的当日总结。
17. 将当天新结果写入临时存储并校验。
18. 将临时结果一次性替换正式 Markdown 文件。
19. 清理临时数据。
20. 输出运行摘要。

## 8. 会话识别与切片规则

本节描述从原始消息到 `ConversationSlice` 的确定性规则。

### 8.1 目标会话识别

- 只处理“目标日期内本人至少发送过 1 条消息”的会话。
- 先找目标会话，再抓取这些会话的当日消息。
- 不在会话发现阶段引入“围观但未发言”的会话。

### 8.2 锚点与锚点簇

- 本人在目标日期内发出的每条消息，先视为一个原始锚点。
- 在同一会话内，相邻两条本人消息之间如果不存在其他发送人的消息，则两条消息属于同一个锚点簇；一旦中间出现其他发送人的消息，则开始新的锚点簇。
- 锚点簇用于代表同一轮由本人参与推进的沟通，而不是逐条本人消息单独切片。
- 本人身份以 `open_id` 为准，显示名仅作为日志与调试辅助信息。

### 8.3 基础窗口

围绕锚点簇裁剪时，先按消息条数确定基础范围：

- 锚点簇之前最多向前保留 `50` 条消息。
- 锚点簇之后最多向后保留 `50` 条消息。
- 锚点簇内的本人消息不计入上述前后配额。
- 单个切片的基础总消息数上限为 `150` 条。
- 消息排序固定为 `send_time asc, message_id asc`；锚点前后 `50` 条均基于该排序截取。

### 8.4 回复关系补全

在基础消息数范围之外，以下与锚点簇有关的消息必须纳入同一切片：

- 锚点簇内消息引用的上游消息。
- 对锚点簇内消息的直接回复。
- 对锚点簇内消息引用内容的回复。
- 与锚点簇内消息存在 1 层直接回复关系的关键上下游消息。

首版引用链和回复链只追 1 层直接关系，不做递归深追。

### 8.5 切片合并规则

如果多个锚点簇裁出的候选切片满足以下任一条件，应合并为同一个 `ConversationSlice`：

- 两个切片有消息重叠。
- 一个切片中包含另一个切片的锚点消息。
- 两个切片通过引用或回复链路相连。

补充说明：

- 若两个锚点簇之间虽然夹有其他发送人的消息，但两侧锚点簇各自按“前后各最多 50 条”裁出的基础窗口已经发生重叠，则这两个锚点簇必须合并为同一个 `ConversationSlice`。
- 例如，两个本人锚点簇之间只隔了 `10` 条其他人的消息时，两侧基础窗口必然重叠，因此应视为同一个切片。

### 8.6 切片边界优先级

切片边界判断优先级如下：

1. 先确定锚点簇。
2. 再按前后各最多 50 条消息构造基础范围。
3. 再补齐与锚点簇有关的引用消息和引用回复。
4. 最后在基础总上限 `150` 条消息的前提下做合并与裁剪。

当“回复关系补全”与“150 条基础上限”冲突时，优先保留与锚点簇直接相关的引用链和回复链，普通邻近消息优先被裁掉。

### 8.7 超长切片处理

- 首轮切片生成时，如果候选内容超过基础总上限 `150` 条，先按优先级截断。
- 截断优先保留锚点簇内消息、锚点簇前后基础范围内的近邻消息，以及与锚点簇存在 1 层直接引用或回复关系的消息。
- 被截断的更早或更晚消息不在首轮直接送入 LLM。
- 如果 LLM 在首轮分析后明确判断当前上下文不足，并要求补充更早或更晚的消息，则由 Python 追加相应方向的消息重新组织分析输入。
- 一旦进入“按 LLM 请求补充上下文”的二次扩展流程，基础 `150` 条上限不再生效，以满足该事项的补充分析需要为准。

### 8.8 切片阶段不做的判断

- Python 先按锚点、消息条数和回复关系生成候选切片。
- Python 可在送 LLM 前，基于重叠、包含关系、引用和回复链路对候选切片做确定性合并。
- Codex 可在首轮分析中判断单个切片是否属于工作事项，并在该切片内部归并连续事项，不做跨切片归并。
- 首轮语义判断只影响候选事项归并，不直接改写 `ConversationSlice` 边界，也不直接突破当前消息选择范围。
- 不在切片阶段做跨会话合并。
- 跨会话、跨批次的同日事项合并统一放到次轮分析处理。

## 9. 抽象接口与数据模型

### 9.1 抽象接口

首版定义四类核心接口和一个编排器：

```text
ChatSource
- get_self_identity() -> SelfIdentity
- list_target_conversations(target_date, self_identity) -> list[ConversationRef]
- fetch_conversation_messages(target_date, conversation_ids) -> list[NormalizedMessage]
- fetch_related_messages(conversation_id, target_message_ids, direction, limit) -> list[NormalizedMessage]

ContentResolver
- to_text(message) -> str
- load_attachment_text_if_needed(message, attachment_ids, hint) -> list[AttachmentTextBlock] | None

Analyzer
- analyze_batch(target_date, batch_input: AnalysisBatch) -> BatchAnalysisResult
- merge_day_candidates(target_date, candidates) -> list[MergedEventDraft]
- summarize_for_manager(target_date, events) -> ManagerSummary

EventStore
- replace_day(target_date, events, manager_summary) -> StoreWriteResult
- read_day(target_date) -> DayDocument | None

DailyTraceRunner
- run(target_date) -> DailyRunResult
```

首版具体实现规划如下：

- `FeishuCliChatSource`
- `FeishuMessageContentResolver`
- `CodexAnalyzer`
- `MarkdownEventStore`

建议使用抽象工厂统一实例创建，至少包括：

- `ChatSourceFactory`
- `ContentResolverFactory`
- `AnalyzerFactory`
- `StorageFactory`

补充约束：

- `fetch_related_messages` 的 `direction` 取值限定为 `earlier` 或 `later`。
- `fetch_conversation_messages` 只返回目标日期内的消息，不返回跨天消息；跨天消息只能通过 `fetch_related_messages` 在补上下文阶段获取。
- `fetch_conversation_messages` 在 source 层必须完成去重、北京时间归一化，并按 `send_time asc, message_id asc` 稳定排序后再返回。
- `fetch_conversation_messages` 返回的每条消息都必须是抓取时可见的最终态；首版不追踪消息编辑历史，也不恢复撤回前正文。
- `fetch_related_messages` 用于承接 `ContextRequest` 中的 `earlier_messages` 和 `later_messages`。
- 当 `direction = earlier` 时，以 `target_message_ids` 中最早的消息为边界，向前抓取最多 `limit` 条消息。
- 当 `direction = later` 时，以 `target_message_ids` 中最晚的消息为边界，向后抓取最多 `limit` 条消息。
- `limit` 对单次请求整体生效，而不是对每个 `target_message_id` 分别生效。
- 返回结果必须按 `send_time asc, message_id asc` 排序，去重，并排除原切片中已有的消息。
- `replace_day` 的 `manager_summary` 为单个字符串。
- Runner 只依赖接口，不直接依赖实现类，便于未来扩展到其他聊天工具、其他 Agent 和其他存储形式。

### 9.2 数据模型

#### 9.2.1 `SelfIdentity`

表示当前飞书用户身份，至少包含本人 `open_id`、显示名和身份来源信息，用于识别本人发言锚点。

#### 9.2.2 `ConversationRef`

表示一个待处理会话的轻量引用，至少包含：

- 会话标识
- 会话名称

补充约束：

- 仅包含“目标日期内本人至少发送过 1 条消息”的会话。
- 该对象用于先枚举目标会话，再批量读取当日消息。

#### 9.2.3 `NormalizedMessage`

表示统一格式的消息对象，屏蔽 `lark-cli` 原始输出差异。首版固定字段如下：

- `conversation_id: str`
- `conversation_name: str`
- `message_id: str`
- `sender_open_id: str | null`
- `sender_name: str`
- `send_time: str`
- `message_type: str`
- `text: str`
- `reply_to_message_id: str | null`
- `quote_message_id: str | null`
- `links: list[LinkMeta]`
- `attachments: list[AttachmentMeta]`
- `is_system: bool`

补充约束：

- `send_time` 固定使用带时区的 ISO 8601 时间字符串，并统一归一化为 `Asia/Shanghai` 语义下可比较的时间值。
- `text` 固定为供分析使用的主文本出口；消息正文、富文本、卡片文本、@人显示文本、链接标题等可直接提取的可读内容，都应尽量收敛到 `text` 或 `links`，不向上层暴露飞书原始结构。
- `message_type` 保留来源类型信息，但上层逻辑不得依赖底层私有字段判断消息语义。
- `sender_open_id` 对系统消息可为空；是否属于本人发言的判断仍以非空 `sender_open_id` 为准。
- 每个附件元数据都应包含稳定的 `attachment_id`，用于后续精确补读。
- 链接元数据 `LinkMeta` 首版固定包含：`url: str`、`title: str`、`link_type: str`；其中 `link_type` 至少区分 `feishu_doc` 与 `normal`。
- 附件元数据 `AttachmentMeta` 首版固定包含：`attachment_id: str`、`file_name: str`、`mime_type: str`、`file_size: int | null`。
- `NormalizedMessage` 是确定性预处理后的统一对象，不直接暴露底层 `lark-cli` 原始结构。

#### 9.2.4 `AttachmentTextBlock`

表示某个附件被补读后的结构化文本块，建议包含：

- `attachment_id`
- `message_id`
- `file_name`
- `text`

补充约束：

- `attachment_id` 必须与消息中的附件元数据稳定对应。
- `message_id` 用于明确该附件来源于哪条消息。
- `text` 为供后续语义分析使用的补读文本，不要求保留底层原始附件结构。

#### 9.2.5 `ConversationSlice`

表示围绕本人发言锚点裁剪后的上下文切片，用于作为 LLM 的基本分析输入单元。一个切片可包含多个连续消息，并保留来源会话信息。

建议包含：

- `slice_id`
- `conversation_id`
- `conversation_name`
- `anchor_message_ids`
- `in_day_message_ids`
- `messages`
- `attachment_texts`

补充约束：

- `ConversationSlice` 只表示 Python 在首轮分析前生成的输入对象。
- LLM 不回写或修改 `ConversationSlice` 定义本身，只能基于单个切片生成候选事项。
- `messages` 中每条消息固定使用 `NormalizedMessage` 的统一字段，不允许再透传实现私有字段。
- `attachment_texts` 仅包含本轮已补读并批准注入分析上下文的附件文本，不预先塞入所有附件正文。
- `attachment_texts` 内元素固定为 `AttachmentTextBlock`。
- `in_day_message_ids` 表示该切片内属于目标日期的消息 ID 集合，用于后续来源校验和稳定 ID 归一化。

#### 9.2.6 `AnalysisBatch`

表示一批待分析的切片集合，用于批量调用 Codex。该对象还应包含批次编号、估算 token 大小和重试上下文。

建议包含：

- `target_date`
- `batch_id`
- `retry_round`
- `estimated_tokens`
- `slices`

补充约束：

- `AnalysisBatch` 是首轮 `Analyzer.analyze_batch(...)` 的固定输入结构，而不是仅供内部调度使用的临时对象。
- `retry_round` 表示当前批次的整体重试轮次；因补充上下文而重跑单个 `slice` 时，应重新生成只包含该 `slice` 的 `AnalysisBatch`。
- `slices` 内元素固定为 `ConversationSlice`，不允许混入自由文本或未结构化提示片段。

#### 9.2.7 `ContextRequest`

表示 LLM 在首轮分析后提出的补充请求。该对象必须是 machine-readable JSON，至少包含：

- `slice_id`
- `request_type`，取值限定为 `earlier_messages`、`later_messages`、`attachment_text`
- `target_message_ids`
- `target_attachment_ids`
- `reason`
- `limit`

补充约束：

- `earlier_messages` 和 `later_messages` 用于请求补前文或后文。
- `attachment_text` 用于请求补读指定消息关联附件的文本内容。
- `limit` 表示建议补充的最大消息条数或附件数量，由 Python 决定是否执行和如何截断。
- 当 `request_type = earlier_messages` 或 `later_messages` 时，`target_message_ids` 只用于确定补充边界，不表示对每条消息分别独立补充。
- 当 `request_type = attachment_text` 时，必须同时提供非空的 `target_message_ids` 和 `target_attachment_ids`。
- 当 `request_type = earlier_messages` 或 `later_messages` 时，`target_attachment_ids` 必须为空数组。

#### 9.2.8 `SourceBackedEventDraft`

表示首轮分析产出的候选事项。该对象用于内存态和临时数据，不写入最终 Markdown 文件。建议包含：

- `draft_id`
- `date`
- `topic`
- `content`
- `result`
- `source_message_ids`
- `source_conversation_id`
- `source_slice_id`
- `confidence`

补充约束：

- `source_message_ids` 只存在于内存或临时数据中，不写入最终 Markdown 文件。
- 首轮每个候选事项必须且只能对应一个 `source_slice_id`，不得在首轮跨多个 `slice` 合并。
- 首轮每个候选事项必须且只能对应一个 `source_conversation_id`；跨会话合并统一放到次轮处理。
- `source_message_ids` 仅包含目标日期内的消息 ID；跨天补充的上下文消息不得写入该字段。
- Python 在接受首轮 `candidate_event` 前，必须先对 `source_message_ids` 做本地归一化：去掉空值和非字符串值、按消息 ID 去重、仅保留当前 `source_slice_id` 对应 `ConversationSlice` 内存在的消息 ID、仅保留目标日期内消息 ID，并按该切片中消息的稳定顺序排序。
- `source_message_ids` 的稳定顺序以所属 `ConversationSlice.messages` 中的顺序为准；该顺序已由上游统一约束为 `send_time asc, message_id asc`。
- 若首轮 `candidate_event` 的 `source_slice_id`、`source_conversation_id` 与实际切片来源不一致，则该 `candidate_event` 必须直接视为无效。
- 若首轮 `candidate_event` 的 `source_message_ids` 归一化后为空数组，则该 `candidate_event` 必须直接视为无效。
- 若首轮 `candidate_event` 的 `source_message_ids` 仅混入少量非法、重复或跨天消息 ID，但归一化后仍保留至少 1 个合法目标日期消息 ID，则 Python 应接受归一化后的结果继续后续流程，而不是整条丢弃。
- `content` 可包含与该事项直接相关的飞书文档标题或链接。

#### 9.2.9 `BatchAnalysisResult`

表示首轮批量分析返回结果，至少包含：

- `candidate_events: list[SourceBackedEventDraft]`
- `context_requests: list[ContextRequest]`

补充约束：

- 两个字段都必须始终返回，允许为空数组，不允许返回自由文本。
- Python 只接受合法 JSON；不合法即视为该批失败。
- 某个 `slice` 只要发出了合法且待处理的 `context_requests`，其当前轮次返回的 `candidate_events` 就不得直接进入最终汇总。

#### 9.2.10 `MergedEventDraft`

表示次轮日内合并后的事项草稿，用于生成最终 `event_id` 之前的中间结果。建议包含：

- `date`
- `topic`
- `content`
- `result`
- `source_message_ids`
- `source_conversation_ids`

补充约束：

- 仅做同一天内合并，不做跨天合并。
- `source_message_ids` 仅包含目标日期内消息，不包含跨天补充上下文消息。
- 若多个候选事项都引用同一飞书文档链接，次轮合并时应去重后保留。
- Python 在接受次轮 `MergedEventDraft` 前，必须再次对 `source_message_ids` 做本地归一化：去重、仅保留目标日期内消息、按全日消息稳定顺序排序。
- 若次轮 `MergedEventDraft` 的 `source_message_ids` 归一化后为空数组，则该草稿必须直接视为无效。

#### 9.2.11 `WorkEvent`

表示最终结构化工作事件，首版字段固定为：

- `date`
- `event_id`
- `topic`
- `content`
- `result`

补充约束：

- `date` 仅保留 `YYYY-MM-DD`。
- `result` 可为空字符串。
- `content` 允许包含与该事项直接相关的飞书文档链接，作为内容摘要的一部分写入，不单独拆列。
- `event_id` 由脚本在次轮合并完成后，基于目标日期和排序后的当日源消息 ID 集合稳定生成。
- 首版固定算法为：`sha1(f"{date}|{','.join(in_day_source_message_ids)}")[:16]`。
- 其中 `in_day_source_message_ids` 表示去重、排序后的目标日期内 `source_message_ids`；跨天补充上下文消息不得参与 `event_id` 计算。
- Python 在计算 `event_id` 前必须先对 `source_message_ids` 做本地归一化：去重、仅保留目标日期内消息、按全日消息稳定顺序排序；全日消息稳定顺序固定为 `send_time asc, message_id asc`。
- `topic`、`content`、`result` 仅作为展示字段，不参与 `event_id` 计算。
- 若多个 `MergedEventDraft` 归一化后得到完全相同的 `in_day_source_message_ids` 集合，Python 必须先在本地视为同一事项并合并，再计算 `event_id`。
- 对“同一事项”的判定固定以归一化后的 `in_day_source_message_ids` 完全一致为准，不比较 `topic`、`content`、`result` 文本是否一致。
- 对同一来源集合的多个 `MergedEventDraft`，Python 只保留一条合并后的事项；该合并后的事项继续沿用该来源集合计算单个 `event_id`。
- 同一来源集合的展示字段采用确定性择优规则：
- `topic`：优先保留非空值；若存在多个非空值，则保留长度更长且不超过实现上限的值；若仍并列，则保留首次出现的值。
- `content`：按段落顺序去重后合并；若某条 `content` 完全包含另一条，则保留信息更完整的一条；若各自包含不同有效信息，则按原始出现顺序拼接并去重重复段落与重复飞书文档链接。
- `result`：优先保留非空值；若存在多个非空值，则保留信息更完整的一条；若结果文本明显冲突且无法在不引入语义判断的前提下安全合并，则保留首次出现的值并记录告警。
- 上述“展示字段择优”仅允许使用非空、长度、包含关系、段落去重和首次出现顺序等确定性规则，不允许 Python 在本地做新的语义改写或事实裁决。
- 若多个 `MergedEventDraft` 生成出相同的 `event_id`，Python 必须在本地按来源集合再次合并后重算；若仍无法消除冲突，则判定整日失败。
- 不长期保存原始消息正文。
- `source_message_ids` 等来源字段不写入最终 Markdown 文件。

#### 9.2.12 `ManagerSummary`

表示写入每日 Markdown 文件、供直接向上级汇报使用的当日总结。建议包含：

- `date`
- `summary_text`

补充约束：

- `summary_text` 由 Codex 基于最终 `WorkEvent` 列表生成，而不是基于原始消息直接生成。
- 总结应聚焦“当天推进了什么、达成了什么、还有什么待跟进”。
- 风格应简洁、可直接复用。
- 若当天无工作事件，`summary_text` 应明确表达“当天未提取到可汇报的工作事项”。

#### 9.2.13 `DayDocument`

表示最终落盘的单日文档对象，建议包含：

- `date`
- `manager_summary`
- `events`
- `generated_at`

#### 9.2.14 `StoreWriteResult`

表示 `EventStore.replace_day(...)` 的写入结果，建议包含：

- `output_path`
- `temp_path`
- `event_count`
- `written_at`
- `validation_passed`

补充约束：

- `output_path` 为正式输出文件的绝对路径。
- `temp_path` 为本次写入使用的临时文件绝对路径；写入成功且临时文件已被替换清理后，允许返回空字符串。
- `event_count` 表示本次写入到正式结果中的 `WorkEvent` 数量。
- `written_at` 使用带时区的 ISO 8601 时间字符串。
- `validation_passed` 表示临时文件回读校验是否通过。

#### 9.2.15 `DailyRunResult`

表示单日执行摘要，建议包含：

- `target_date`
- `conversation_count`
- `message_count`
- `slice_count`
- `batch_count`
- `event_count`
- `skipped_slice_count`
- `warning_count`
- `status`
- `output_path`
- `error_summary`

补充约束：

- `status` 固定枚举为 `success`、`success_with_warnings`、`failed`、`invalid_input`
- `success` 表示成功写入结果文件，且没有 `slice` 跳过或其他告警
- `success_with_warnings` 表示成功写入结果文件，但存在 `slice` 跳过、附件读取失败或请求被拒绝等告警
- `failed` 表示未写入新结果文件
- `invalid_input` 表示参数不合法，流程未开始执行
- CLI `stdout` 与 `DailyRunResult` 使用同一套字段命名，不再额外定义第二套摘要格式。

## 10. 分析协议与运行策略

### 10.1 分批原则

- 不按单条消息调用。
- 不按单条事件调用。
- 一批包含多个 `ConversationSlice`。
- 当某个切片内容过长时，可单独成批。

首版固定阈值如下：

- 首轮单批最大 `10` 个 `slice`。
- 首轮目标 token 预算为 `30,000`。
- 首轮硬上限为 `42,000`。
- 超长单 `slice` 允许单独成批，硬上限为 `55,000`。
- 实现配置项 `max_model_input_tokens` 固定按 token 计量，首版默认值为 `100,000`。

补充约束：

- 上述阈值仅适用于首轮切片分批。
- 因 `context_requests` 触发的 `slice` 级补充重跑，不再受首轮 `30,000 / 42,000 / 55,000` 阈值约束。
- 补充重跑以“优先满足语义理解效果”为原则，但仍必须受实现配置项 `max_model_input_tokens` 约束；超过该上限时，Python 才可截断或拒绝补充请求。

### 10.2 职责分工

Python 负责：

- preflight 依赖检查。
- 切片分批。
- token 预算估算。
- 批次重试。
- 多批结果汇总。
- 首轮补充请求处理。
- 次轮日内合并组织。
- 最终本地校验。
- `slice` 级重跑状态管理。

Codex 负责：

- 接收一个批次的多个切片。
- 在首轮返回结构化 JSON 的 `candidate_events` 与 `context_requests`。
- 在次轮返回结构化 JSON 的 `MergedEventDraft` 数组。
- 在总结阶段返回结构化 `ManagerSummary`。

### 10.3 补充请求协议

首轮分析返回的补充请求必须满足以下约束：

- 仅允许 `earlier_messages`、`later_messages`、`attachment_text` 三类请求。
- 每条请求必须指向一个明确的 `slice_id`。
- `target_message_ids` 必须是当前切片内已有消息或其直接关联附件对应的消息 ID。
- 当 `request_type = attachment_text` 时，`target_attachment_ids` 必须是 `target_message_ids` 所关联的附件 ID 子集。
- Python 负责决定是否批准请求，以及实际补充多少内容。
- 若请求不合法、超范围或无法执行，Python 可忽略该请求并继续处理其他结果。

批准策略补充如下：

- 首版不设置偏保守的小条数上限；对合法且可执行的补充请求，Python 应优先批准，以效果优先。
- `earlier_messages` / `later_messages` 的 `limit` 默认尽量尊重 LLM 请求值，只有在超过实现配置项 `max_model_input_tokens` 或超出可读取范围时才截断。
- 一旦进入补上下文流程，首轮切片基础 `150` 条上限不再生效。
- 跨天补充继续允许，但只允许在原命中的同一会话内扩展，不新增新的会话范围。
- 同一轮、同一 `slice` 内的多个合法 `context_requests`，Python 应先统一收集并去重，再合并执行一次补充，不应为每条请求分别立即重跑该 `slice`。
- 同一 `slice` 的补充执行顺序固定为：先 `attachment_text`，再 `earlier_messages`，最后 `later_messages`。
- 已成功补入某个 `slice` 的消息和附件文本，在该 `slice` 后续重跑轮次中持续保留；后续重复请求同一附件或已补过的消息范围时，Python 应直接去重，不重复加载。

补充处理规则如下：

- 只有“已批准且待执行”的 `context_request` 会阻塞当前轮 `candidate_events` 进入最终汇总。
- 若请求不合法，应直接忽略该请求，并接受当前轮 `candidate_events`。
- 若请求合法但超出策略上限，Python 可先按上限截断；若截断后仍不可执行，则忽略该请求，并接受当前轮 `candidate_events`。
- 若请求执行失败，但当前轮已返回合法 `candidate_events`，则接受当前轮结果并记录告警。
- 若请求执行失败且当前轮没有可接受的 `candidate_events`，则该 `slice` 进入一次失败重跑计数。

### 10.4 `slice` 级重跑规则

- `slice` 是补充上下文后的最小重跑单位。
- 某个 `slice` 只要返回了合法且待处理的 `context_requests`，该 `slice` 在当前轮次产生的 `candidate_events` 就先不进入最终汇总。
- Python 执行补充后，仅重组该 `slice` 的分析输入并重跑该 `slice`。
- 只有在该 `slice` 不再请求补充，或其请求被 Python 拒绝后仍接受当前结果时，对应 `candidate_events` 才能进入最终汇总。
- 若某个 `slice` 多次补充后仍无法得到合法结果，可按重试上限判定该 `slice` 失败并跳过，但必须记录到运行摘要。
- 若某个 `slice` 达到补充重跑上限后仍继续请求补充，则跳过该 `slice`，不接受其当前轮结果。

固定重试阈值如下：

- 单批失败最多重试 `2` 次。
- 单个 `slice` 补充上下文最多重跑 `3` 轮。
- 次轮日内合并最多重试 `3` 次。

### 10.5 成功/失败判定

- 当天无本人发言时，视为“成功空覆盖”。
- 当天分析结果为 `0` 条事件时，若属于正常空结果，也视为成功。
- 若“有效候选事项为 `0` 且不是正常空结果”，则判定整日失败。
- 若次轮日内合并失败，则判定整日失败。

“正常空结果”固定定义为以下场景：

- 当天无本人发言。
- 有本人发言，但全部被判定为非工作内容。
- 有本人发言，但全部属于系统消息、空消息或无可解析内容，且已在粗过滤阶段被安全过滤。
- 存在部分 `slice` 被跳过，但至少有成功完成的有效分析结果，且这些成功结果一致表明当天没有可提取的工作事项。

以下场景不属于“正常空结果”，必须判定为 `failed`：

- 所有 `slice` 都因 Codex 调用失败、JSON 不合法、补充重跑耗尽或协议错误而没有形成可接受结果。
- 次轮日内合并失败。
- 所有候选结果都因本地校验失败而无法进入最终汇总。

### 10.6 失败恢复

- preflight 检查失败时，不进入当日业务处理流程，不写入任何结果文件。
- 单批失败只重试该批，不重跑整天。
- 补充上下文或附件时，只重跑相关 `slice`，不影响其他已完成结果。
- 整日执行失败时不写入半成品结果。
- 当批次结果格式不合法时，应在 Python 层判定失败并进入重试或终止流程。
- 某个 `slice` 超出补充重跑上限后，可跳过该 `slice` 并继续整日流程，但必须在运行摘要中明确标记。
- 若次轮日内合并失败，则整日失败，不写入新结果。
- 若次轮结果生成出不可消解的 `event_id` 冲突，则整日失败，不写入新结果。

## 11. 内容补充与过滤规则

### 11.1 附件处理

- 默认只使用可直接读取的消息文本、卡片文本、链接标题等内容。
- 是否需要补读附件文本由 LLM 通过 `ContextRequest` 判断；当 LLM 明确请求附件内容时，Python 再补读附件文本并追加到后续分析输入中。
- 附件解析失败不应导致整日失败，应降级为忽略该附件并记录日志。

### 11.2 跨天补充上下文

- 首版允许为理解事项而补充同一会话内目标日期边界之外的前文或后文。
- 跨天补充只允许发生在已经命中的目标会话内，不新增新的会话范围。
- 跨天补充消息只作为分析输入上下文，不计入 `source_message_ids`。
- 最终 `WorkEvent.date` 永远等于目标日期，不因补充上下文跨天而改变。
- `event_id` 仅基于目标日期内的 `source_message_ids` 生成。

### 11.3 本地粗过滤边界

Python 仅做零风险粗过滤，不做“是否属于工作事项”的语义判断。首版可过滤对象限定为：

- 空消息
- 纯系统消息
- 无文本且无可解析内容的消息
- 明确的撤回、入群、改名等系统事件

### 11.4 飞书文档链接保留规则

- 对消息中可直接识别的飞书文档链接，可直接作为事项线索参与分析，无需额外补读文档正文。
- 若某个事项涉及飞书文档，且消息文本、卡片文本或链接元数据中已能直接提取该文档 URL，可将该链接写入最终 `content`。
- 飞书文档链接是否写入，以“对后续回顾该事项是否有帮助”为准；无关链接不应保留。
- 同一事项内若出现重复的飞书文档链接，写入 `content` 前应去重。
- 首版仅保留文档链接，不额外抓取文档正文，也不新增单独字段。

### 11.5 隐私保护与人名保留规则

- LLM 在提炼 `topic`、`content`、`result` 和 `manager_summary` 时，应避免复述聊天中涉及吵架、调情、辱骂或其他情绪发泄的具体言语。
- 若情绪化表达与事项推进有关，可仅保留必要的事实性结论，例如“沟通中出现分歧，后续已明确责任人和处理方式”，不保留原始情绪性表述。
- 该约束的目标是保护个人隐私，而不是删除任务相关事实。
- 与任务分配、责任归属、协作推进直接相关的人名允许保留在事件内容或总结中。
- 若某人名仅出现在与工作无关的私人表达中，且不影响事项理解，则不应主动保留。

## 12. 存储设计

### 12.1 目录与文件路径

首版固定存储位置如下：

- 根目录：`data/`
- 年目录：`data/YYYY/`
- 月目录：`data/YYYY/MM/`
- 日文件：`data/YYYY/MM/YYYY-MM-DD.md`

示例：

- `data/2026/06/2026-06-22.md`
- `data/2026/07/2026-07-01.md`

### 12.2 每日 Markdown 文件结构

每日 Markdown 文件固定包含以下部分：

1. Front matter
2. 文档标题
3. 给上级汇报的当日总结
4. 当日事项列表

Front matter 固定字段如下：

- `date`
- `event_count`
- `generated_at`
- `generator`

补充约束：

- `event_count` 表示当日最终写入的 `WorkEvent` 数量。
- `generated_at` 使用带时区的 ISO 8601 时间字符串。
- `generator` 首版固定写 `worktrace`。
- 事项列表中的事件按 `event_id asc` 稳定排序。
- `content` 和 `result` 采用块文本格式，允许多行，不再编码为单行列表字段。
- `manager_summary` 与每个事件块都必须带 machine-readable HTML 注释边界，供 `read_day` 和写后校验使用。
- `read_day` 应优先依赖 HTML 注释边界和 Front matter 解析，而不是依赖正文标题文案或单行冒号分隔。
- `content` 与 `result` 的正文即使出现 `###`、`#### content`、`#### result` 等文本，也不应影响结构解析，因为这些文本只在对应事件边界内部按原样保留。

### 12.3 覆盖策略

- 同日期重跑时，必须先生成当天完整新结果，再替换当天旧文件。
- 不保留原始聊天长期归档。
- 不保留版本历史。
- 首版不做汇总表。
- 当天分析结果为 `0` 条事件时，也视为一次成功覆盖，应替换为对应的空结果 Markdown 文件。

### 12.4 临时写入与替换流程

为避免“先删后写”导致的当日数据丢失，首版采用“临时文件写入 + 整文件原子替换”的日级替换流程：

1. 先在内存中构造当天完整 `DayDocument`。
2. 将当天新结果写入临时文件，例如 `data/YYYY/MM/.YYYY-MM-DD.tmp.md`。
3. 重新读取临时文件并完成结构校验。
4. 校验通过后，使用 `os.replace(tmp_path, final_path)` 原子替换正式文件。
5. 写入成功后清理临时数据。

补充约束：

- 若在第 1 到第 3 步失败，旧文件必须保持不变。
- 若第 4 步失败，允许保留临时文件以便重试。
- 临时文件中不写入 `source_message_ids` 等来源字段。
- 即使当天 `event_count = 0`，也应生成完整 Markdown 文件，保留标题和“给上级汇报的当日总结”区块。

## 13. 错误处理与日志

### 13.1 需覆盖的异常场景

首版至少需要覆盖以下异常场景：

- `lark-cli` 调用失败
- 身份读取失败
- 当天无本人发言
- Codex 单批分析失败
- 次轮日内合并分析失败
- JSON 格式不合法
- Markdown 写入失败
- 附件读取失败
- 某个 `slice` 超过补充重跑上限后被跳过

### 13.2 错误处理要求

- preflight 失败时，应优先返回缺失依赖、版本不满足、未登录或不可写目录等明确错误原因，而不是在业务中途报模糊错误。
- 单批失败可重试。
- 次轮合并失败时，不应替换当天旧文件。
- 整日失败不应写入半成品。
- 若某个附件读取失败，不影响其他切片继续处理。
- 当天无本人发言时，按成功空覆盖处理，不记为失败。

### 13.3 日志要求

日志至少应包含：

- 目标日期
- 批次号
- 会话标识
- 当前处理阶段
- 错误摘要

## 14. 附录

### 14.1 JSON 契约示例

首轮 `analyze_batch` 输入固定对象：

```json
{
  "target_date": "2026-06-22",
  "batch_id": "batch-001",
  "retry_round": 0,
  "estimated_tokens": 18320,
  "slices": [
    {
      "slice_id": "slice-001",
      "conversation_id": "oc_1",
      "conversation_name": "项目上线群",
      "anchor_message_ids": ["om_2", "om_3"],
      "in_day_message_ids": ["om_1", "om_2", "om_3", "om_4"],
      "messages": [
        {
          "conversation_id": "oc_1",
          "conversation_name": "项目上线群",
          "message_id": "om_1",
          "sender_open_id": "ou_xxx",
          "send_time": "2026-06-22T10:02:00+08:00",
          "message_type": "text",
          "text": "今晚能否确认上线窗口？",
          "reply_to_message_id": null,
          "quote_message_id": null,
          "links": [],
          "attachments": [],
          "is_system": false
        }
      ],
      "attachment_texts": []
    }
  ]
}
```

首轮分析返回固定对象：

```json
{
  "candidate_events": [
    {
      "draft_id": "draft-001",
      "date": "2026-06-22",
      "topic": "项目上线排期确认",
      "content": "与产品和研发确认了上线窗口、依赖项和回归安排。相关文档：https://xxx.feishu.cn/docx/abc123",
      "result": "确定 6 月 25 日晚发布。",
      "source_message_ids": ["om_1", "om_2", "om_3"],
      "source_conversation_id": "oc_1",
      "source_slice_id": "slice-001",
      "confidence": 0.91
    }
  ],
  "context_requests": [
    {
      "slice_id": "slice-001",
      "request_type": "attachment_text",
      "target_message_ids": ["om_8"],
      "target_attachment_ids": ["att_1"],
      "reason": "附件中可能包含最终排期表。",
      "limit": 1
    }
  ]
}
```

次轮合并返回固定数组：

```json
[
  {
    "date": "2026-06-22",
    "topic": "项目上线排期确认",
    "content": "整合多个会话中的确认信息，沉淀为统一事项。相关文档：https://xxx.feishu.cn/docx/abc123",
    "result": "确定 6 月 25 日晚发布。",
    "source_message_ids": ["om_1", "om_2", "om_3", "om_10"],
    "source_conversation_ids": ["oc_1", "oc_2"]
  }
]
```

总结阶段返回固定对象：

```json
{
  "date": "2026-06-22",
  "summary_text": "今天主要推进了项目上线排期确认和数据口径对齐两项工作。上线方面，已与产品、研发确认上线窗口和依赖安排，确定 6 月 25 日晚发布。数据方面，已与运营统一日报统计口径，明确按支付成功口径出数。后续需继续跟进上线前回归和发布准备。"
}
```

补充约束：

- 首轮输入必须是 `AnalysisBatch` 固定对象，不允许直接拼接自由文本作为协议主体。
- 首轮必须返回对象，且同时包含 `candidate_events` 与 `context_requests` 两个字段。
- 首轮每条 `candidate_event` 必须仅引用一个 `source_slice_id` 和一个 `source_conversation_id`。
- 次轮必须返回数组，不允许外包裹解释文本。
- 总结阶段必须返回对象，不允许外包裹解释文本。
- `result` 允许为空字符串 `""`，不允许返回 `null`。
- 若返回飞书文档链接，应写入 `content` 文本中，不新增独立返回字段。

### 14.2 每日 Markdown 样例

```md
---
date: 2026-06-22
event_count: 2
generated_at: 2026-06-23T20:10:00+08:00
generator: worktrace
---

# WorkTrace 2026-06-22

## 给上级汇报的当日总结

<!-- worktrace:manager_summary:start -->
今天主要推进了项目上线排期确认和数据口径对齐两项工作。上线方面，已与产品、研发确认上线窗口和依赖安排，确定 6 月 25 日晚发布。数据方面，已与运营统一日报统计口径，明确按支付成功口径出数。后续需继续跟进上线前回归和发布准备。
<!-- worktrace:manager_summary:end -->

## 事项列表

<!-- worktrace:event:start event_id="e3b0c44298fc1c14" -->
### e3b0c44298fc1c14 项目上线排期确认

- date: 2026-06-22
- event_id: e3b0c44298fc1c14
- topic: 项目上线排期确认

#### content

与产品和研发确认了上线窗口、依赖项和回归安排。
相关文档：https://xxx.feishu.cn/docx/abc123

#### result

确定 6 月 25 日晚发布。
<!-- worktrace:event:end -->
```

## 15. 扩展方向

以下能力属于后续扩展方向，不在首版范围内：

- 自动定时调度
- Claude Code 封装
- 更多聊天源
- 更多存储后端
- 汇总日报视图
- 候选低置信事件复核机制
