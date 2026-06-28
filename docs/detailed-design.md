# WorkTrace 详细设计

## 1. 文档目标

本文档说明 WorkTrace 当前阶段的目标、边界、隐私口径、执行流程、接口契约和存储格式。

本文档面向当前要交付给普通员工使用的单日处理链路，重点是说明“系统现在应该怎么工作”以及“为什么这样设计”。

## 2. 产品目标与范围

### 2.1 当前产品目标

WorkTrace 的当前目标是：从飞书中提取“目标日期内本人至少发过 1 条消息”的会话内容，结合 LLM 做语义分析，生成仅保留公共工作信息的事件清单，写入本地 Markdown 文件，并通过飞书 CLI 发送给员工自己，方便后续自行审阅、回顾和转发。

### 2.2 当前范围

当前阶段正式范围包括：

- 手动指定日期执行单日处理
- 基于 `lark-cli` 读取飞书会话与消息
- Python 侧消息过滤、切片、扩窗、校验、链接聚合、投递和存储
- 会话级首轮事件提炼
- 同一会话内按 `context_requests` 自动补上下文并重跑
- 同日候选事件跨会话分组归并
- 本地 Markdown 覆盖写入
- 生成成功后将 Markdown 文件发送给当前登录用户自己

### 2.3 当前不做

当前版本仍不处理：

- 真正的定时调度
- 跨天事项合并
- 图片 OCR
- 完整飞书文档正文预抓取
- 自动给领导、部门或公司做汇总
- 自动把日报上传到统一公司数据库
- 其他即时通讯工具的正式接入

## 3. 隐私与信任边界

这一节不是附属说明，而是产品边界的一部分。

### 3.1 当前系统想解决的问题

WorkTrace 想解决的是：员工和管理者做事后复盘、日报整理、运营分析时，常常回忆不清当天到底发生了什么。它试图把“与本人直接相关的工作事件”整理成一份结构化记录，降低回忆失真。

### 3.2 当前系统不应该被描述成什么

WorkTrace 不应被描述成：

- 私聊抓取工具
- 员工行为监控工具
- 自动向上级汇报的强制系统
- 零风险、绝对不外发数据的系统

### 3.3 当前默认隐私保护规则

当前实现以以下规则为准：

- 分析范围限定为“目标日期内本人至少发过 1 条消息的会话”
- 只提取与本人直接相关的工作事项
- 输出结果只保留日期，不保留时间
- 最终员工可见产物不显示人名、群名、open_id、消息 ID、会话 ID
- 默认过滤薪资、绩效、争吵、辱骂等敏感内容
- 正式日处理主流程默认不长期落盘原始聊天内容
- 最终长期保留的是结构化 Markdown，不是原始聊天记录
- 默认发送目标是员工自己，不是领导

### 3.4 当前客观边界

当前系统必须如实说明：

- 飞书聊天通过本地 `lark-cli` 读取
- 语义分析依赖员工本地配置的在线 LLM 服务
- 因此被发送给模型服务的是经过裁剪和压缩后的必要上下文，而不是零数据外发

## 4. 当前确定性业务规则

当前实现以以下规则为准：

- “当天”按 `Asia/Shanghai` 时区的 `00:00:00` 到 `23:59:59` 切分
- 与工作无关的聊天内容需要自动忽略
- 同一事项允许跨多个会话合并，但只在同一天内合并
- 存储采用“按年月目录组织、每天一个 Markdown 文件”的模式
- 同一天重复执行采用覆盖策略
- 当前对员工和后续人工汇总暴露的输出字段固定为 `date`、`title`、`content`、`file_links`
- `event_id` 仅作为内部稳定标识，不作为员工最终可见字段
- 当天无本人发言时，按“成功空覆盖”处理
- 获取当前飞书 user 身份统一通过 `ChatSource.get_self_identity()` 完成
- 首轮每个候选事项必须且只能来自单个会话切片
- 补前文和补后文的边界统一由 `target_message_ids` 决定
- 附件补读结果采用结构化 `AttachmentTextBlock`
- 咨询类事件、流程审核类事件、团建活动组织类事件、技能培训类事件默认不纳入长期保留事件

## 5. 设计原则

- 保守高准确，宁可少记也不误记
- LLM 只负责语义理解，不负责确定性流程控制和数据计算
- 文档链接优先用于人类回忆和追溯，不作为模型重推理材料
- 正式主流程默认强制 `/no_think` 和关闭推理配置
- 正式日处理主流程默认不长期落盘原始聊天内容
- 仓库根目录同时作为通用脚本项目根目录和 Codex skill 根目录
- 运行流程、能力装配和存储都通过抽象层解耦，便于后续扩展

## 6. 总体架构

### 6.1 当前六层结构

WorkTrace 当前采用六层结构：

1. Codex Skill 层  
   负责接收用户在 Codex 或兼容 Agent 对话中的触发请求，解析目标日期，调用底层脚本完成处理，并向用户返回执行摘要。

2. 安装 / 自检层  
   负责首次依赖初始化、环境检测、错误提示和员工侧使用引导。

3. Runner / Orchestrator 层  
   负责组织单日执行流程，串联聊天抓取、消息过滤、会话切片、首轮分析、会话内重跑、跨会话合并、链接聚合、Markdown 写入和投递。

4. Source / Resolver / Analyzer / Delivery / Store 抽象层  
   定义统一接口，通过工厂创建具体实现，降低飞书、LLM、发送链路和存储之间的耦合。

5. Python 实现层  
   实现首版具体能力，包括飞书消息抓取、消息预处理、附件补读、事件物化、文件链接聚合、Markdown 写入和异常处理。

6. 外部依赖层  
   当前依赖 `lark-cli`、analyzer 调用通道和本地 Markdown 文件。

### 6.2 当前仓库结构

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

## 7. 运行入口与职责边界

### 7.1 Python 入口契约

当前固定 Python 入口为：

- `python -m src.worktrace.cli --date YYYY-MM-DD`

当前约束如下：

- `--date` 为必填参数
- `--debug-output` 为可选参数；启用后把会话级分析与日级 merge 的调试文件写入本地
- 日期格式固定为 `YYYY-MM-DD`
- 业务时区固定为 `Asia/Shanghai`
- `stdout` 返回 `DailyRunResult` 的 machine-readable JSON
- `stderr` 用于输出日志

退出码约定如下：

- `0`：执行成功，包括成功空覆盖
- `1`：业务执行失败
- `2`：输入参数不合法

### 7.2 Python 负责的内容

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
- 对 LLM 返回的 `source_message_ids` 做验真、去重和顺序规范化
- 根据跨会话分组结果物化 `MergedEventDraft`
- 基于最终事件生成稳定的 `event_id`
- 根据最终事件来源消息聚合显式文件链接
- 覆盖写入 Markdown 文件
- 把生成文件发送给当前登录用户自己
- 控制日志、异常和重试
- 在显式启用 `--debug-output` 时，将会话分析输入输出与跨会话 merge 输入输出落盘到本地调试目录

### 7.3 LLM 负责的内容

当前 LLM 负责非确定性的语义工作，包括：

- 判断内容是否属于工作事项
- 在单个会话内提炼 `candidate_events`
- 判断是否需要补充更早消息、更晚消息或附件正文
- 在全日范围内判断哪些候选事项属于同一真实工作事件
- 从当前输入切片中的真实飞书消息 ID 里，选择与候选事件最相关的 `source_message_ids`
- 提炼事件主题
- 总结事件内容
- 忽略与工作无关的内容

### 7.4 LLM 不负责的内容

LLM 不负责以下工作：

- 数据计算
- 北京时间日边界切分
- 去重逻辑
- 身份识别
- 自造消息 ID
- Markdown 读写
- 覆盖策略
- 稳定 ID 生成
- 最终真实文件链接聚合

## 8. LLM 调用策略

当前主流程中的 LLM 调用默认且强制遵循以下约束：

- prompt 末尾统一追加 `/no_think`
- 请求体显式携带关闭推理配置，默认是 `reasoning={"effort":"none"}`
- 若 provider 不支持该字段，至少保留 `/no_think`
- 不在员工默认主流程中提供关闭该策略的开关

当前 prompt 中对链接的处理原则如下：

- 允许保留 `[飞书文档]`
- 允许保留 `[飞书文档: 标题]`
- 不应把完整 URL 集合直接展开给模型

## 9. 单日执行主流程

当前单日执行流程如下：

1. 接收目标日期
2. 获取当前飞书 user 身份
3. 找出“目标日期内本人至少发送过 1 条消息”的会话
4. 拉取这些会话的当日消息
5. Python 过滤明显无效消息
6. 按会话构造 `ConversationSlice`
7. 对每个 `ConversationSlice` 调用会话级分析
8. 首轮返回 `context_requests` 时，在同一会话内自动补上下文并重跑
9. 汇总全日所有 `SourceBackedEventDraft`
10. 调用 `merge_day_candidates(...)` 做跨会话分组
11. Python 物化 `MergedEventDraft`
12. 调用 `build_work_events(...)` 构建最终 `WorkEvent`
13. 根据最终事件来源消息聚合 `file_links`
14. 覆盖写入当天 Markdown 文件
15. 发送当天 Markdown 文件给当前登录用户自己
16. 输出运行摘要

### 9.1 调试落盘入口

正式主流程默认不落盘调试上下文。

当用户显式使用：

- `python -m src.worktrace.cli --date YYYY-MM-DD --debug-output`

时，CLI 会把 `conversation_debug_root` 自动指向：

- `data/debug/conversations`

单日运行后的调试目录结构为：

```text
data/debug/conversations/<target_date>/
  <safe_slice_id>/pass_01/
    input.json
    prompt.txt
    output.json
    meta.json
  _merge_day_candidates/
    input.json
    prompt.txt
    output.json
```

其中：

- 会话目录记录单个 `ConversationSlice` 的输入、prompt、模型输出与统计信息
- `_merge_day_candidates` 记录全日候选事件 merge 的输入、prompt 与模型输出
- 这些文件仅用于本地排障，不属于正式业务产物

## 10. 输出模型

### 10.1 对外公共字段

当前阶段最终员工可见的公共字段固定为：

- `date`
- `title`
- `content`
- `file_links`

### 10.2 内部字段

当前仍保留内部稳定字段，例如：

- `event_id`
- `source_message_ids`
- `source_conversation_ids`
- `draft_id`
- `action_label`
- `object_hint`

这些字段用于内部追踪、去重或链接聚合，不应作为员工最终视图主字段。

其中：

- `draft_id` 是单日候选事项在进入跨会话 merge 前的稳定草稿标识
- `action_label` 是候选事项的主要动作标签，例如 `回复 / 催办 / 撰写 / 同步`
- `object_hint` 是候选事项的核心对象提示，例如 `提前付款 / 汇报文档 / 优惠券配置`

`action_label` 与 `object_hint` 当前只作为内部语义字段使用，主要用于帮助跨会话 merge 更稳地判断“是不是同一真实事件”，不会进入员工最终看到的 Markdown 字段。

### 10.3 `source_message_ids` 的来源与规范化

这里容易混淆，当前系统按下面三层理解：

1. 飞书 CLI 返回真实消息  
   Python 先通过 `lark-cli` 拉取消息，并将每条消息标准化为 `NormalizedMessage`。  
   这些消息对象中的 `message_id` 才是原始真实来源。

2. LLM 选择事件关联消息  
   LLM 在输出 `candidate_events` 时，需要从当前输入切片里已经存在的真实 `message_id` 中，挑选它认为与该事件最相关的消息，写入 `source_message_ids`。  
   因此，`source_message_ids` 这个字段是 LLM 输出的，但字段里的 ID 值本体来自飞书真实消息。

3. Python 再做规范化  
   Python 不会直接信任 LLM 原样返回的 `source_message_ids`，而是会进一步：
   - 过滤不在当前切片中的 ID
   - 过滤不属于当天消息集合的 ID
   - 去重
   - 按当前切片中的真实消息顺序重排

规范化后的来源消息集合，才会继续用于后续的 `draft_id`、`event_id`、跨会话合并和文件链接聚合。

### 10.4 首轮候选事项内部结构

当前首轮 `candidate_events` 在 Python 侧会落成 `SourceBackedEventDraft`，核心字段包括：

- `draft_id`
- `date`
- `topic`
- `content`
- `source_message_ids`
- `source_conversation_id`
- `source_slice_id`
- `confidence`
- `action_label`
- `object_hint`

其中：

- `draft_id` 在校验阶段默认由 `target_date + 规范化后的 source_message_ids` 稳定生成
- 如同一批次内出现相同基础 `draft_id`，Python 会追加稳定后缀做区分
- `action_label` 与 `object_hint` 来自 LLM 首轮提炼结果，但会在 Python 校验阶段做裁剪与保留

## 11. 存储与发送

### 11.1 本地存储

当前存储采用“按年月目录组织、每天一个 Markdown 文件”的模式。

### 11.2 发送策略

当前阶段默认发送方式为：

- 生成成功后，通过当前登录飞书 `user` 身份
- 将本地 `.md` 文件
- 发送给当前用户自己

若本地文件写入成功但发送失败：

- 整次运行记为 `success_with_warnings`
- 本地文件必须保留
- 结果中应包含明确的投递失败信息

## 12. 运行前检查

在正式处理前，系统应至少检查：

- Python 版本满足要求
- Python 依赖已安装
- `lark-cli` 已安装
- `lark-cli` 已登录为可用 user 身份
- 本地 `.env` 或环境变量中已配置在线模型参数
- `WORKTRACE_LLM_REASONING_EFFORT=none`
- 在线 LLM 连通性正常
- `data/` 目录可创建或可写
- 时区配置可用

若关键依赖缺失、版本不满足、未登录、`no_think` 配置不满足或目录不可写，应直接失败并返回明确原因，而不是在处理中途报错。
