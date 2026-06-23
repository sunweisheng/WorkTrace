# WorkTrace 编码任务与模块拆解

## 1. 文档目标

本文基于 [detailed-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/detailed-design.md) 的约束，补充首版实现时的模块组成、编码任务拆解、开发顺序和测试范围，作为进入编码阶段的直接执行清单。

本文不改动详细设计中的业务口径；若与详细设计冲突，以详细设计为准。

## 2. 首版实现目标

首版目标不是做完整平台，而是稳定跑通以下闭环：

1. 接收 `--date YYYY-MM-DD`
2. 完成 preflight 检查
3. 从飞书识别“当天本人发过消息的会话”
4. 拉取当日消息并构造 `ConversationSlice`
5. 调用 Codex 做首轮分析、补充重跑和次轮合并
6. 生成 `WorkEvent` 与 `ManagerSummary`
7. 覆盖写入 `data/YYYY/MM/YYYY-MM-DD.md`
8. 向 `stdout` 输出 `DailyRunResult` JSON

## 3. 建议模块结构

建议从一开始就按包边界拆分，避免把所有逻辑堆到 `cli.py` 或单文件中。

```text
src/
└── worktrace/
    ├── __init__.py
    ├── cli.py
    ├── config.py
    ├── constants.py
    ├── factories.py
    ├── logging_utils.py
    ├── models.py
    ├── runner.py
    ├── preflight.py
    ├── errors.py
    ├── sources/
    │   ├── __init__.py
    │   ├── base.py
    │   └── feishu_cli.py
    ├── resolvers/
    │   ├── __init__.py
    │   ├── base.py
    │   └── feishu_message.py
    ├── analyzers/
    │   ├── __init__.py
    │   ├── base.py
    │   ├── prompts.py
    │   ├── protocol.py
    │   └── codex.py
    ├── stores/
    │   ├── __init__.py
    │   ├── base.py
    │   └── markdown.py
    ├── pipeline/
    │   ├── __init__.py
    │   ├── filtering.py
    │   ├── slicing.py
    │   ├── batching.py
    │   ├── context_expansion.py
    │   ├── validation.py
    │   └── event_merge.py
    └── utils/
        ├── __init__.py
        ├── dates.py
        ├── hashing.py
        ├── json_io.py
        └── text.py
```

## 4. 模块职责拆解

### 4.1 入口与运行编排

#### `cli.py`

职责：

- 解析 `--date`
- 执行 preflight
- 调用 `DailyTraceRunner.run`
- 统一输出 `DailyRunResult` JSON
- 映射退出码 `0/1/2`

要求：

- 不承载业务细节
- 不直接拼接飞书命令和 Codex 提示词
- 只做参数、流程入口和异常兜底

#### `runner.py`

职责：

- 串联单日处理全流程
- 汇总运行统计
- 控制批次、重跑、跳过与失败策略

这是首版的核心编排模块，应尽量保持“只编排、不做底层细节”的风格。

### 4.2 配置与通用基础设施

#### `config.py`

职责：

- 定义运行配置
- 管理默认阈值
- 为后续环境变量或配置文件扩展预留入口

建议包含：

- `timezone = "Asia/Shanghai"`
- `slice_context_before = 50`
- `slice_context_after = 50`
- `slice_base_limit = 150`
- `batch_slice_limit = 10`
- `batch_target_tokens = 30000`
- `batch_hard_limit = 42000`
- `single_slice_hard_limit = 55000`
- `max_model_input_tokens = 100000`
- `batch_retry_limit = 2`
- `slice_retry_limit = 3`
- `merge_retry_limit = 3`

#### `constants.py`

职责：

- 集中定义固定枚举值
- 避免协议字符串散落在各处

建议包含：

- `DailyRunStatus`
- `ContextRequestType`
- `ContextDirection`

#### `logging_utils.py`

职责：

- 创建统一 logger
- 保证日志字段最少覆盖目标日期、批次号、会话、阶段、错误摘要

#### `errors.py`

职责：

- 定义可区分的异常类型
- 让 preflight、source、analyzer、store 和 runner 之间传递结构化错误

建议异常：

- `InvalidInputError`
- `PreflightError`
- `ChatSourceError`
- `AnalyzerProtocolError`
- `StoreWriteError`

### 4.3 抽象接口与工厂

#### `models.py`

职责：

- 定义详细设计中的核心数据模型
- 承担 JSON 序列化和基础字段校验

首批应落地的模型：

- `SelfIdentity`
- `ConversationRef`
- `LinkMeta`
- `AttachmentMeta`
- `NormalizedMessage`
- `AttachmentTextBlock`
- `ConversationSlice`
- `AnalysisBatch`
- `ContextRequest`
- `SourceBackedEventDraft`
- `BatchAnalysisResult`
- `MergedEventDraft`
- `WorkEvent`
- `ManagerSummary`
- `DayDocument`
- `StoreWriteResult`
- `DailyRunResult`

建议优先使用 `dataclasses`，配合显式 `from_dict` / `to_dict`，避免首版引入过重依赖。

#### `sources/base.py`

职责：

- 定义 `ChatSource` 抽象接口

#### `resolvers/base.py`

职责：

- 定义 `ContentResolver` 抽象接口

#### `analyzers/base.py`

职责：

- 定义 `Analyzer` 抽象接口

#### `stores/base.py`

职责：

- 定义 `EventStore` 抽象接口

#### `factories.py`

职责：

- 统一创建 `ChatSource`、`ContentResolver`、`Analyzer`、`EventStore`
- 让 `runner.py` 只依赖接口而不依赖具体实现

### 4.4 飞书来源层

#### `sources/feishu_cli.py`

职责：

- 封装所有 `lark-cli` 交互
- 获取本人身份
- 找目标会话
- 拉取会话当日消息
- 按请求补前文和后文
- 做来源层去重、时间归一化、稳定排序

子职责建议：

- `get_self_identity()`
- `list_target_conversations()`
- `fetch_conversation_messages()`
- `fetch_related_messages()`
- 私有辅助：
  - CLI 命令执行
  - JSON 解析
  - 飞书原始结构到 `NormalizedMessage` 的转换
  - `send_time` 转 `Asia/Shanghai`
  - 去重与排序

注意点：

- source 层必须只返回目标日期内消息
- 补上下文时才允许跨天
- 需要把飞书原始数据差异收敛掉，不能向上暴露底层结构

### 4.5 内容解析层

#### `resolvers/feishu_message.py`

职责：

- 统一将消息转成分析文本
- 从消息中提取链接和附件元数据
- 按需补读附件文本

推荐拆成三部分：

1. 主文本提取
2. 链接提取与 `link_type` 判定
3. 附件补读

注意点：

- 默认不预读附件正文
- `attachment_text` 只在 LLM 请求后读取
- 附件失败要降级，不拖垮整日流程

### 4.6 切片与批处理流水线

#### `pipeline/filtering.py`

职责：

- 做零风险粗过滤

过滤范围只包含：

- 空消息
- 纯系统消息
- 无文本且无可解析内容
- 明确撤回、入群、改名等系统事件

#### `pipeline/slicing.py`

职责：

- 识别本人锚点
- 聚合锚点簇
- 构造基础窗口
- 补一层引用/回复关系
- 处理重叠合并
- 生成 `ConversationSlice`

这是首版最关键的确定性算法模块之一，建议内部明确拆成以下函数：

- `group_anchor_clusters(messages, self_open_id)`
- `build_base_window(messages, cluster, before_limit, after_limit)`
- `expand_direct_relations(messages, cluster, window_ids)`
- `merge_overlapping_slices(candidate_slices)`
- `trim_slice_by_priority(messages, max_base_limit)`

#### `pipeline/batching.py`

职责：

- 估算 token
- 按阈值组装 `AnalysisBatch`
- 处理超长单切片单独成批

#### `pipeline/context_expansion.py`

职责：

- 校验 `ContextRequest`
- 合并同一 `slice` 的多条请求
- 控制执行顺序：附件、前文、后文
- 维护已补消息和已补附件去重
- 为单个 `slice` 生成重跑输入

#### `pipeline/validation.py`

职责：

- 校验 analyzer 返回 JSON 契约
- 校验候选事件字段
- 对 `source_message_ids` 做本地归一化
- 拒绝非法来源、空来源和跨切片/跨会话错误引用

#### `pipeline/event_merge.py`

职责：

- 校验次轮 `MergedEventDraft`
- 按来源集合去重合并
- 生成稳定 `event_id`
- 使用确定性规则合并 `topic`、`content`、`result`

### 4.7 Codex 分析层

#### `analyzers/protocol.py`

职责：

- 定义首轮分析、次轮合并、总结生成的 JSON 协议
- 提供协议校验入口

#### `analyzers/prompts.py`

职责：

- 维护 prompt 模板
- 将详细设计中的口径稳定编码进提示词

建议至少拆三类 prompt：

- 首轮切片分析 prompt
- 次轮日内合并 prompt
- 上级汇报总结 prompt

#### `analyzers/codex.py`

职责：

- 真实发起 Codex 调用
- 承担 machine-readable JSON 请求与响应解析
- 区分命令不存在、未登录、超时、非法 JSON 等错误

注意点：

- preflight 要做最小真实探测调用
- 正式分析时只接受合法 JSON
- 不能把自由文本返回给 runner 当作成功结果

### 4.8 Markdown 存储层

#### `stores/markdown.py`

职责：

- 根据日期生成目录和文件路径
- 渲染 `DayDocument`
- 写临时文件
- 回读校验
- 原子替换正式文件
- 支持 `read_day`

建议拆分：

- `build_output_path(target_date)`
- `render_day_document(day_doc)`
- `parse_day_document(markdown_text)`
- `replace_day(target_date, events, manager_summary)`
- `read_day(target_date)`

注意点：

- `manager_summary` 和事件块要带 HTML 注释边界
- 写后校验优先依赖边界和 front matter
- 即使 `event_count = 0` 也要生成完整文档

### 4.9 通用工具层

#### `utils/dates.py`

职责：

- 日期校验
- 业务时区转换
- 判断消息是否属于目标日期

#### `utils/hashing.py`

职责：

- 按设计生成 `event_id`

#### `utils/json_io.py`

职责：

- 安全 JSON 读写
- 标准化 machine-readable 输出

#### `utils/text.py`

职责：

- 内容段落去重
- URL 去重
- 文本清洗

## 5. 编码任务拆解

这一节按“实际开发步骤”重写。每一步都明确说明：

- 这一阶段先做什么
- 要改哪些文件
- 文件里要落什么能力
- 做完后如何验收
- 下一步依赖它什么结果

建议按 `S1 -> S8` 顺序推进，不要跳着做。

### S1. 先搭项目骨架和统一数据模型

目的：

- 先把包结构、基础类型、配置和错误定义固定下来
- 让后续所有模块都基于同一套对象开发

前置依赖：

- 无

要新增或实现的文件：

- `src/worktrace/__init__.py`
- `src/worktrace/models.py`
- `src/worktrace/config.py`
- `src/worktrace/constants.py`
- `src/worktrace/errors.py`
- `src/worktrace/utils/json_io.py`
- `tests/unit/test_models.py`

这一阶段要完成的具体实现：

1. 创建 `src/worktrace/` 包和子目录占位文件
2. 在 `models.py` 里先实现最核心数据模型
3. 给每个模型补 `to_dict` / `from_dict`
4. 在 `config.py` 里固化首版阈值和时区配置
5. 在 `constants.py` 里定义固定状态和协议枚举
6. 在 `errors.py` 里定义统一异常类型
7. 在 `utils/json_io.py` 里实现安全 JSON 编解码辅助函数

这一阶段建议先实现的模型顺序：

1. `SelfIdentity`
2. `ConversationRef`
3. `LinkMeta`
4. `AttachmentMeta`
5. `NormalizedMessage`
6. `AttachmentTextBlock`
7. `ConversationSlice`
8. `AnalysisBatch`
9. `ContextRequest`
10. `SourceBackedEventDraft`
11. `BatchAnalysisResult`
12. `MergedEventDraft`
13. `WorkEvent`
14. `ManagerSummary`
15. `DayDocument`
16. `StoreWriteResult`
17. `DailyRunResult`

验收标准：

- 所有核心模型都能完成 `dict -> object -> dict`
- 主要枚举和异常能被其他模块直接 import
- `pytest tests/unit/test_models.py` 可覆盖基础序列化

下一步为什么依赖它：

- CLI、source、analyzer、store、runner 都需要用到这些对象，先定模型能避免后面来回改签名

### S2. 再做 CLI 入口和 preflight

目的：

- 先拿到一个可执行入口
- 即使后面的业务还没做完，也能先验证环境和输出契约

前置依赖：

- S1

要新增或实现的文件：

- `src/worktrace/cli.py`
- `src/worktrace/preflight.py`
- `src/worktrace/logging_utils.py`
- `tests/unit/test_preflight.py`
- `tests/integration/test_cli_contract.py`

这一阶段要完成的具体实现：

1. 在 `cli.py` 里实现 `--date` 参数解析
2. 把日期校验失败映射为 `invalid_input`
3. 在 `preflight.py` 里实现依赖检查总入口
4. 检查 Python 版本
5. 检查 `lark-cli` 命令是否存在
6. 检查 `lark-cli` 当前是否是可读取消息的 user 身份
7. 检查 Codex 最小真实探测调用
8. 检查 `data/` 目录能否创建和写入
9. 检查 `Asia/Shanghai` 时区是否可用
10. 在 `logging_utils.py` 里提供统一 logger 初始化
11. 让 CLI 最终固定输出 `DailyRunResult` JSON 到 `stdout`

这一阶段先不用做的事：

- 不需要接入真实 runner 业务流程
- 不需要拉飞书消息
- 不需要写 Markdown

验收标准：

- `python -m src.worktrace.cli --date 2026-06-22` 能返回合法 JSON
- 缺失 `--date` 或日期格式错误时退出码为 `2`
- preflight 失败时退出码为 `1`
- `stdout` 字段名与 `DailyRunResult` 完全一致

下一步为什么依赖它：

- 后续每实现一个模块，都可以通过 CLI 做增量联调，不必等全链路完成

### S3. 固定抽象接口和工厂

目的：

- 把“编排层”和“实现层”彻底解耦
- 避免后面把具体实现直接写死在 runner 里

前置依赖：

- S1

要新增或实现的文件：

- `src/worktrace/factories.py`
- `src/worktrace/sources/base.py`
- `src/worktrace/resolvers/base.py`
- `src/worktrace/analyzers/base.py`
- `src/worktrace/stores/base.py`

这一阶段要完成的具体实现：

1. 在各 `base.py` 中定义详细设计里的四类接口
2. 明确每个接口的输入输出都使用 `models.py` 中的对象
3. 在 `factories.py` 中提供默认实现装配入口
4. 让后续 `runner.py` 只依赖工厂和接口，不依赖具体类

验收标准：

- 可以从工厂拿到占位实现或真实实现
- 接口签名和详细设计保持一致

下一步为什么依赖它：

- 飞书来源、Codex 分析器、Markdown 存储器都要按这套接口落地

### S4. 先打通飞书取数和内容标准化

目的：

- 先解决“怎么稳定拿到统一消息结构”这个基础问题
- 这是后面切片、分析、存储的共同输入

前置依赖：

- S1
- S3

要新增或实现的文件：

- `src/worktrace/sources/feishu_cli.py`
- `src/worktrace/resolvers/feishu_message.py`
- `src/worktrace/utils/dates.py`
- `tests/unit/test_feishu_source.py`
- `tests/unit/test_message_resolver.py`

这一阶段要完成的具体实现：

1. 在 `FeishuCliChatSource` 里封装所有 `lark-cli` 调用
2. 实现 `get_self_identity`
3. 实现 `list_target_conversations`
4. 实现 `fetch_conversation_messages`
5. 实现 `fetch_related_messages`
6. 把飞书原始消息统一映射为 `NormalizedMessage`
7. 在 source 层完成去重
8. 在 source 层完成 `Asia/Shanghai` 时间归一化
9. 在 source 层保证 `send_time asc, message_id asc` 稳定排序
10. 在 `FeishuMessageContentResolver` 里实现 `to_text`
11. 实现链接提取和 `link_type` 判定
12. 实现附件元数据提取
13. 实现按需附件补读 `load_attachment_text_if_needed`

建议内部先做的小步骤：

1. 先只跑通 `get_self_identity`
2. 再跑通“列会话”
3. 再跑通“拉当日消息”
4. 最后做“补前文/后文”和附件补读

验收标准：

- 给一批原始飞书消息，能稳定产出统一 `NormalizedMessage`
- 同一输入重复执行排序结果一致
- 附件补读失败时返回告警而不是整段崩溃

下一步为什么依赖它：

- 切片算法只能基于统一消息对象实现，不能直接吃飞书原始结构

### S5. 再实现消息粗过滤、切片和分批

目的：

- 把“原始消息流”变成可送给 LLM 的 `ConversationSlice` 和 `AnalysisBatch`

前置依赖：

- S1
- S4

要新增或实现的文件：

- `src/worktrace/pipeline/filtering.py`
- `src/worktrace/pipeline/slicing.py`
- `src/worktrace/pipeline/batching.py`
- `tests/unit/test_filtering.py`
- `tests/unit/test_slicing.py`
- `tests/unit/test_batching.py`

这一阶段要完成的具体实现：

1. 在 `filtering.py` 中实现零风险粗过滤
2. 在 `slicing.py` 中实现本人锚点识别
3. 实现锚点簇分组
4. 实现基础窗口裁剪
5. 实现一层引用/回复关系补全
6. 实现重叠、包含、链路相连切片合并
7. 实现超长切片优先级裁剪
8. 生成稳定 `slice_id`
9. 在 `batching.py` 中实现 token 估算
10. 按 10 个 `slice` 和 token 阈值拆分 `AnalysisBatch`
11. 对超长单切片做单独成批处理

建议编码顺序：

1. 先把过滤做完
2. 再实现锚点簇
3. 再实现基础窗口
4. 再加引用/回复补全
5. 再加切片合并
6. 最后实现分批

验收标准：

- 同一消息输入多次运行能得到完全一致的 `ConversationSlice`
- 单元测试覆盖重叠窗口、他人消息插入、引用回复链、超长切片四类关键场景
- 每个 `AnalysisBatch` 满足 slice 数量和 token 预算约束

下一步为什么依赖它：

- analyzer 只能接收结构化批次，不能直接接一整天原始消息

### S6. 接入 Codex 协议、批量分析和补充重跑

目的：

- 先让 LLM 调用变成稳定协议
- 再让单个 `slice` 支持补前文、补后文和补附件后重跑

前置依赖：

- S1
- S3
- S5

要新增或实现的文件：

- `src/worktrace/analyzers/protocol.py`
- `src/worktrace/analyzers/prompts.py`
- `src/worktrace/analyzers/codex.py`
- `src/worktrace/pipeline/context_expansion.py`
- `src/worktrace/pipeline/validation.py`
- `tests/unit/test_analyzer_protocol.py`
- `tests/unit/test_context_expansion.py`
- `tests/unit/test_validation.py`

这一阶段要完成的具体实现：

1. 定义首轮 `analyze_batch` 的 JSON 输入输出协议
2. 定义次轮合并协议
3. 定义上级总结协议
4. 在 `prompts.py` 中维护三类 prompt 模板
5. 在 `codex.py` 中实现真实调用和响应 JSON 解析
6. 在 `validation.py` 中校验 `candidate_events`
7. 在 `validation.py` 中归一化 `source_message_ids`
8. 在 `context_expansion.py` 中校验 `ContextRequest`
9. 合并同一 `slice` 的多个请求
10. 按“附件 -> 前文 -> 后文”顺序执行补充
11. 维护某个 `slice` 已补消息和附件的去重状态
12. 实现单个 `slice` 最多 3 轮重跑

建议编码顺序：

1. 先定义协议对象和校验函数
2. 再写 prompt 模板
3. 再接真实 Codex 调用
4. 最后补 `slice` 级重跑

验收标准：

- analyzer 只返回结构化结果或结构化错误
- 非法 JSON 会被判定为失败
- 请求非法时可忽略并接受当前合法结果
- 同一 `slice` 补充重跑不会影响其他 `slice`

下一步为什么依赖它：

- 没有这一层，runner 还无法得到可信候选事项

### S7. 做次轮合并、稳定事件生成和 Markdown 存储

目的：

- 把候选事项收敛为最终 `WorkEvent`
- 再安全写入单日 Markdown 文件

前置依赖：

- S1
- S5
- S6

要新增或实现的文件：

- `src/worktrace/pipeline/event_merge.py`
- `src/worktrace/stores/markdown.py`
- `src/worktrace/utils/hashing.py`
- `src/worktrace/utils/text.py`
- `tests/unit/test_event_merge.py`
- `tests/unit/test_markdown_store.py`

这一阶段要完成的具体实现：

1. 校验次轮 `MergedEventDraft`
2. 归一化次轮 `source_message_ids`
3. 按来源集合完全一致做本地去重合并
4. 用确定性规则择优 `topic`、`content`、`result`
5. 按设计生成稳定 `event_id`
6. 在 `markdown.py` 中实现输出路径生成
7. 渲染 front matter
8. 渲染“给上级汇报的当日总结”区块
9. 渲染事件列表区块
10. 为总结区和事件区添加 machine-readable HTML 注释边界
11. 实现 `read_day`
12. 实现临时文件写入、回读校验、`os.replace` 原子替换

验收标准：

- 相同来源集合只保留一个 `WorkEvent`
- `event_id` 对相同输入稳定不变
- 写入失败时旧文件保持不变
- `event_count = 0` 时也能生成完整文档

下一步为什么依赖它：

- runner 最终要靠这些模块生成正式产物和成功状态

### S8. 最后总装 runner，跑完整链路

目的：

- 把前面所有能力真正串起来
- 形成首版端到端可运行版本

前置依赖：

- S1 到 S7

要新增或实现的文件：

- `src/worktrace/runner.py`
- `tests/integration/test_runner_happy_path.py`
- `tests/integration/test_runner_empty_day.py`
- `tests/integration/test_runner_slice_retry.py`
- `tests/integration/test_runner_failure_modes.py`

这一阶段要完成的具体实现：

1. 在 `runner.py` 中串起完整日处理流程
2. 统计 `conversation_count`
3. 统计 `message_count`
4. 统计 `slice_count`
5. 统计 `batch_count`
6. 统计 `event_count`
7. 统计 `skipped_slice_count`
8. 统计 `warning_count`
9. 区分 `success`、`success_with_warnings`、`failed`、`invalid_input`
10. 正确区分“正常空结果”和“失败导致空结果”
11. 出错时保证不写半成品
12. 成功时返回完整 `DailyRunResult`

验收标准：

- 能从 CLI 端到端执行完整链路
- 无本人发言时走“成功空覆盖”
- 次轮合并失败时整日失败且不覆盖旧文件
- 某个 `slice` 被跳过时返回 `success_with_warnings`

## 5.1 最小可执行开发顺序

如果只想尽快开始编码，建议按下面顺序逐个提交：

1. 提交 1：`S1` 项目骨架和模型
2. 提交 2：`S2` CLI 和 preflight
3. 提交 3：`S3` 接口和工厂
4. 提交 4：`S4` 飞书 source 和 resolver
5. 提交 5：`S5` filtering、slicing、batching
6. 提交 6：`S6` analyzer、validation、context expansion
7. 提交 7：`S7` event merge 和 markdown store
8. 提交 8：`S8` runner 总装和集成测试

## 5.2 每一步完成后应该能看到什么

- 做完 `S1`：工程结构成型，模型和配置可导入
- 做完 `S2`：CLI 可以返回结构化 JSON，即使业务还没接完
- 做完 `S3`：各层边界稳定，不会把实现耦死
- 做完 `S4`：可以稳定拿到统一 `NormalizedMessage`
- 做完 `S5`：可以从消息产出 `ConversationSlice` 和 `AnalysisBatch`
- 做完 `S6`：可以从批次产出候选事项，并支持补充重跑
- 做完 `S7`：可以得到最终 `WorkEvent` 并写入 Markdown
- 做完 `S8`：整条链路可正式运行

## 5.3 先后依赖关系

可以按下面理解阻塞关系：

- `S1` 是所有步骤的基础
- `S2` 只依赖 `S1`
- `S3` 只依赖 `S1`
- `S4` 依赖 `S1 + S3`
- `S5` 依赖 `S1 + S4`
- `S6` 依赖 `S1 + S3 + S5`
- `S7` 依赖 `S1 + S5 + S6`
- `S8` 依赖 `S1 ~ S7`

## 6. 推荐开发顺序

推荐按下面顺序推进，能最快形成“每一步都可验证”的增量实现：

1. `models.py`、`config.py`、`errors.py`
2. `cli.py`、`preflight.py`
3. 抽象接口与工厂
4. `sources/feishu_cli.py`
5. `resolvers/feishu_message.py`
6. `pipeline/filtering.py`、`pipeline/slicing.py`
7. `pipeline/batching.py`
8. `analyzers/protocol.py`、`analyzers/prompts.py`、`analyzers/codex.py`
9. `pipeline/context_expansion.py`、`pipeline/validation.py`
10. `pipeline/event_merge.py`
11. `stores/markdown.py`
12. `runner.py`
13. 端到端联调和回归测试

## 7. 测试模块拆解

建议测试目录同步按模块拆分：

```text
tests/
├── unit/
│   ├── test_models.py
│   ├── test_preflight.py
│   ├── test_slicing.py
│   ├── test_filtering.py
│   ├── test_batching.py
│   ├── test_validation.py
│   ├── test_event_merge.py
│   └── test_markdown_store.py
└── integration/
    ├── test_cli_contract.py
    ├── test_runner_happy_path.py
    ├── test_runner_empty_day.py
    ├── test_runner_slice_retry.py
    └── test_runner_failure_modes.py
```

### 7.1 单元测试重点

必须优先覆盖：

- 日期解析与非法输入
- `send_time` 归一化
- 锚点簇识别
- 切片重叠合并
- 一层引用/回复补全
- 超长切片截断优先级
- `source_message_ids` 归一化
- `event_id` 稳定生成
- Markdown 渲染与回读

### 7.2 集成测试重点

必须覆盖：

- preflight 成功/失败
- 当天无本人发言的成功空覆盖
- 全部非工作内容的成功空覆盖
- 单批分析失败重试
- 单个 `slice` 补充重跑成功
- 单个 `slice` 超上限被跳过
- 次轮合并失败导致整日失败
- 写存储失败不覆盖旧文件

## 8. 模块与详细设计章节映射

为减少实现时来回找文档，建议按下面映射开发：

- 运行入口与状态：详细设计第 6 节，对应 `cli.py`、`preflight.py`、`runner.py`
- 切片规则：详细设计第 8 节，对应 `pipeline/slicing.py`
- 接口与模型：详细设计第 9 节，对应 `models.py`、各 `base.py`
- 分析协议与重跑：详细设计第 10 节，对应 `analyzers/` 与 `pipeline/context_expansion.py`
- 补充与过滤：详细设计第 11 节，对应 `resolvers/` 与 `pipeline/filtering.py`
- 存储：详细设计第 12 节，对应 `stores/markdown.py`
- 错误与日志：详细设计第 13 节，对应 `errors.py`、`logging_utils.py`、`runner.py`

## 9. 首版里程碑定义

建议把编码阶段分成 4 个里程碑：

### 里程碑 A：可启动

范围：

- 项目骨架
- 模型
- CLI
- preflight

验收：

- 能返回结构化运行摘要

### 里程碑 B：可取数

范围：

- 飞书来源
- 内容解析
- 粗过滤
- 切片

验收：

- 能从目标日期产出稳定 `ConversationSlice`

### 里程碑 C：可分析

范围：

- 分批
- Codex 调用
- 补充请求
- 次轮合并

验收：

- 能从切片产出最终 `WorkEvent`

### 里程碑 D：可交付

范围：

- Markdown 存储
- Runner 总装
- 集成测试

验收：

- 能端到端生成日文档并返回正确状态

## 10. 建议先做的最小可用实现

如果希望尽快跑通第一版，不必一开始就把所有细节做到最满，建议按下面最小闭环优先：

1. 先实现 preflight、CLI 和固定模型
2. 再实现飞书取数和基础切片
3. 然后接入首轮分析和空结果存储
4. 最后补 `context_requests`、次轮合并、完整校验

这样可以更早拿到真实数据样本，避免在没有样本前把补充协议设计得过重。

## 11. 开发注意事项

- 不要让 `runner.py` 同时承担消息解析、切片算法和 Markdown 渲染。
- 不要在 analyzer 层做本地字段兜底修复；字段归一化应放在 `pipeline/validation.py`。
- 不要把飞书原始 JSON 结构直接透传到上层。
- 不要把 `source_message_ids` 写入最终 Markdown。
- 不要在首版引入自动调度逻辑。
- 不要为了省模块而把 prompt、协议、调用和重试都堆在同一个 analyzer 文件里。

## 12. 结论

首版最值得优先投入的是 4 个地方：

1. `NormalizedMessage` 统一模型
2. `ConversationSlice` 切片算法
3. Codex 结构化协议与重跑机制
4. Markdown 原子覆盖写入

只要这 4 个部分边界清楚，后续扩展更多聊天源、更多存储或自动调度时，整体结构都不用推倒重来。
