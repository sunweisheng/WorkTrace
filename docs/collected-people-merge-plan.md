# WorkTrace 多人事件 Markdown 汇总设计

> 状态：正式 `merge-collected` 实现说明，不是未来计划。

## 1. 目标与边界

该链路面向已经收集到多人个人日报的管理人员。它只读取 WorkTrace Markdown 事件块，不重新读取员工聊天，也不做跨天合并。

正式命令：

```bash
python3 -m src.worktrace.cli merge-collected --date YYYY-MM-DD
```

该子命令独立执行，不复用个人日报的整套 preflight；运行中仍需要当前飞书 user 身份、在线 analyzer 配置、输入目录写权限和 bot 自发送能力。

## 2. 输入目录与 scope

```text
merge_inbox/YYYY/MM/DD/
├── YYYY-MM-DD-张三.md
├── 李四-YYYY-MM-DD.md
├── 上游-YYYY-MM-DD-merged.md
└── 项目A/
    ├── YYYY-MM-DD-王五.md
    └── 更深目录/              # 不递归
```

scope 规则：

- 日期根目录始终单独合并
- 每个非隐藏一级子目录单独合并
- scope 只读取当前层 `.md`
- 二级及更深目录不读取
- 隐藏文件、本次输出同名文件和旧 `_merged.md` 跳过
- 其他规范的上游 `*-merged.md` 可继续作为来源

每个 scope 在本目录生成：

```text
YYYY-MM-DD-登录人姓名-merged.md
```

### 2.1 两级人工收集方式

部门汇总和中心汇总复用同一命令，代码不会根据文件内容自动判断当前属于哪一级。文件名中的负责人来自运行命令时当前飞书登录人。

部门负责人运行时：

```text
merge_inbox/YYYY/MM/DD/
├── YYYY-MM-DD-员工甲.md
├── YYYY-MM-DD-员工乙.md
└── YYYY-MM-DD-部门负责人.md       # 建议保留，用于明确冲突时采用负责人版本
```

生成 `YYYY-MM-DD-部门负责人-merged.md`。中心负责人运行时，人工收集各部门结果：

```text
merge_inbox/YYYY/MM/DD/
├── YYYY-MM-DD-部门负责人甲-merged.md
├── YYYY-MM-DD-部门负责人乙-merged.md
└── YYYY-MM-DD-中心负责人.md       # 可选；需要中心负责人本人事项或冲突优先级时加入
```

再次运行后生成 `YYYY-MM-DD-中心负责人-merged.md`。个人 MD 与已经包含该人员的部门 MD 可以同时存在，程序会把两份都正常读入，不比较 `source_event_ids`，不拦截，也不写重复来源 warning；文件组合由负责人人工控制。日期根目录和一级子目录只是并列 scope，不会自动串成部门、中心两级流水线。

## 3. 总流程

```mermaid
flowchart TD
    A["发现 merge scopes"] --> B["解析当前 user 身份"]
    B --> C["逐 scope 读取 Markdown"]
    C --> D["解析姓名、正文和隐藏合并信息"]
    D --> E{"每条事件都有 v2 会话指纹?"}
    E -->|"否"| X["全部 scope 停止并列出来源文件"]
    E -->|"是"| F["关键词过滤 + 保留门槛"]
    F --> G["标记 merge-owner source"]
    G --> H["相同 event_id 的确定性组"]
    H --> I["共同消息、文件和同日会话建立关系集合"]
    I --> J["LLM 使用事件正文发现候选组"]
    J --> K{"候选、复核或内容 prompt 超阈值?"}
    K -->|"是"| L["关系优先分批并汇合组摘要"]
    K -->|"否"| R{"命中高风险复核条件?"}
    L --> R
    R -->|"是"| V["复核并在不确定时拆组"]
    R -->|"否"| M["按锁定候选组生成正式内容"]
    V --> M
    M --> N["字段与来源覆盖检查、局部重试"]
    N --> O["补齐非正文字段元数据"]
    O --> P["检查工作流边界并整合不同视角"]
    P --> Q["物化团队 WorkEvent 和增强信息"]
    Q --> Z["最终关键词过滤 + 保留门槛"]
    Z --> S["覆盖写 merged Markdown"]
    S --> T["飞书 bot 自发送"]
```

## 4. 来源文件解析

支持能解析出日期和姓名的文件名，例如：

- `YYYY-MM-DD-姓名.md`
- `姓名-YYYY-MM-DD.md`
- `姓名_YYYY-MM-DD.md`
- 规范化 `*-merged.md`

每个文件通过 `MarkdownEventStore.parse_day_document(...)` 回读。多人汇总允许尾部部分恢复：若最后一个事件块损坏但前面已有完整事件，保留完整事件、跳过残缺事件并记录文件名、事件 ID、声明数量和解析数量，且不修改来源文件。没有完整事件、文件名无姓名、front matter 非法或读取失败时仍整份跳过，不影响同 scope 其他有效文件。

## 5. 来源过滤

每条来源 `WorkEvent` 在进入 LLM 前执行：

1. `filter_work_events(...)`：敏感词和排除词匹配，包含文件标题与 URL
2. `filter_retained_work_events(...)`：具体对象、保留理由和保留依据门槛

被过滤来源不会进入 prompt。

过滤诊断记录阶段、类别、来源文件、来源人员、事件 ID 和标题，不记录命中的具体关键词或完整敏感正文。

## 6. 合并人来源

当前 `lark-cli auth status` 的用户名是 merge owner。来源文件名解析出的姓名与其精确匹配时，该来源事件标记为 `is_merge_owner_source=true`。

同一真实事项包含 merge-owner source 时：

- 没有明确冲突时，正常整合所有人的不冲突事实、动作、结果、风险和待办
- 只有版本号、结论、状态、结果或待办方向明确冲突时，才采用 merge owner 来源
- 模型返回 `merge_owner_conflict` 和冲突说明，Python 写入运行 warning
- 最终正文只显示整合结果，不按人员逐条展示贡献

scope 有来源事件但没有匹配到 merge owner 时，直接执行普通多人合并，不写 warning。输出文件仍以当前飞书登录人命名。

## 7. 增强合并信息与边界

个人 Markdown 的每条事件可提供：

- `workstream_name`
- `action_labels`
- `self_relations`
- `evidence_fingerprints`
- `conversation_fingerprints`
- `file_keys`
- `source_report_owners`，旧 V2 文件缺失时按空列表读取
- 现有标题、内容、具体对象、保留依据、文件、来源人员和来源事件 ID

合并规则：

- 两条事件都有非空工作流且名称不同：默认拆开；只有共享同日会话或共同消息且模型确认属于同一事项时允许合并
- 工作流相同：只表示可能属于同一工作范围，不能直接合并
- Python 对消息指纹和文件指纹去重后，计算事件对的共同数量及非空集合是否完全相同，只把存在共同项的 `evidence_relations` 发送给模型
- Python 按同日会话指纹形成 `conversation_groups`，只发送临时组编号和 draft ID，不发送会话哈希或原始会话 ID
- `evidence_relations`、相同具体对象或连续动作是强证据，即使指纹集合完全相同也不能自动合并，仍由模型结合内容确认
- 同一会话只表示可能属于同一事项，大群中的不同事项仍必须由模型分开
- 工作流为空的事件，可在共同消息、共同文件或明确业务对象支持下并入命名工作流
- 只有标题相似、时间接近或部门相同，不作为合并依据
- 来源负责人相同不能单独作为合并依据

模型 prompt 不包含原始长指纹数组；原始指纹继续保留在 Markdown 和调试 trace 的 `input_events` 中用于追溯。隐藏信息升级为 v2，普通读取仍兼容 v1，但多人合并发现任一事件缺少会话指纹时会在全部模型调用前停止，列出文件和缺失事件数量并要求重新生成。损坏的隐藏信息由普通读取忽略并写 warning。

因此，仓库中现有 v1 收集文件只能用于解析规模、旧输出和旧 trace 复盘，不能直接验证当前依赖同日会话关系的候选分组。当前流程的最终效果必须用重新生成的 v2 多人来源文件验证，不能通过给 v1 文件补空字段或伪造指纹替代。

## 8. 确定性预分组

Python 按稳定 `event_id` 聚合来源。相同 ID 的事件只有在标题/内容满足相似性规则时才锁定为确定性组；同 ID 但内容明显分歧时写 warning，并交给 LLM 判断。

确定性组是给模型的约束，不代表跳过后续验证。

## 9. 两阶段合并与关系优先分批

候选、复核和正式内容请求统一受 `max_model_input_tokens=6200` 限制。

第一阶段发送内部草稿 ID、来源人员、来源负责人、标题、具体对象、工作流、动作和完整事件正文，由模型返回候选分组、`group_reason` 和 `risk_flags`；多成员组在同一次调用中同时返回候选标题、候选内容和候选对象，单成员组由 Python 保留原事件。同一会话、共同消息和共同文件只提供候选关系，不锁定模型结果。

候选组在正式正文生成前执行高风险判定。配置文件 `config/collected_merge.json` 的默认条件是：

- 来源事件数达到 10 条
- 来源文件数达到 4 个
- 候选来自跨批再次判断
- 候选成员经过 Python 修复
- 存在不同非空工作流

命中任一条件时增加一次模型复核。确认同一事项时保留原组；发现不同事项时拆组；拿不准时拆开。复核必须完整覆盖原组全部 draft ID，遗漏、未知或重复 ID 只重试当前复核，仍无效则当前 scope 失败。

第二阶段展开回原始事件，按已确认组发送完整内容并生成正式汇总。模型必须返回与锁定组完全一致的 `covered_draft_ids`，并用 `fact_items.source_draft_ids` 标明关键事实来源。Python 检查整批 draft 分配、锁定组、正文覆盖和事实来源；失败时只重试当前内容组，重试后仍不完整则不写文件。单条组直接保留，不增加模型调用。

- 候选发现估算输入不超过 `max_model_input_tokens` 时一次处理
- 超过统一 token 上限时按消息、文件和会话关系集合优先分批，再用组摘要做跨批汇合
- 不可继续拆开的候选输入按来源文件平均分配内容空间，短内容完整保留，长内容同时保留开头和结尾
- 高风险复核超限时同样按关系分批，单条正文仍过长时复用正文切片和分层摘要
- 正式内容生成按锁定候选组装箱，不拆开能放入统一 token 上限的关系组
- 单个正式组或单条来源事件超过统一 token 上限时按完整句子拆分并分层汇总，任何真实模型调用都不得超限

所有中间结果持续保留：

- 来源人员
- 来源事件 ID
- 来源负责人
- merge-owner source 标记
- 文件链接
- 保留元数据
- 工作流名称、主要动作和协作方式
- 消息证据指纹、同日会话指纹和文件标识

中间结果只在内存中存在，最终只写一次规范化汇总文件。

每个批次对可重试模型错误默认等待 2 秒后重试 1 次，只重试当前批次。429、HTTP 5xx、连接、超时、流式 JSON、空返回和无效 JSON 属于可重试错误；鉴权、权限、TLS 和参数错误立即失败。错误重试与字段缺失重试分别计数。

## 10. 字段检查、重试与修复

模型返回后先统计 group 缺少或泛化的字段：

- title
- content
- object_hint
- retention_reason
- retention_detail

缺失比例达到 `collected_merge_missing_field_retry_ratio` 且未超过 `collected_merge_missing_field_retry_limit` 时，重新请求。环境变量可覆盖比例和次数。

候选阶段保留既有的 draft 修复能力，并把修复后的组标记为高风险后复核。正式正文阶段不再用来源正文修补覆盖缺口：

- `covered_draft_ids` 必须与锁定组完全一致
- `fact_items.source_draft_ids` 只能引用当前组成员，并覆盖当前组全部来源
- 正文为空或覆盖不完整时只重试当前组，重试后仍失败则当前 scope 不落盘
- 标题、具体对象、保留理由和保留依据缺失时仍可从来源补齐
- 正文不再通过 `merge_content_texts(...)` 把全部来源原文追加到模型结果
- 明确冲突仍只采用合并人来源，并记录冲突说明

## 11. 输出与追溯

团队 `WorkEvent` 在个人字段之外额外保留：

- `source_people`
- `source_event_ids`
- `source_report_owners`
- `workstream_name`
- `action_labels`
- `self_relations`，Markdown 显示为“协作方式”
- `evidence_fingerprints`
- `conversation_fingerprints`
- `file_keys`

输入文件是规范 `*-merged.md` 时，Python 从文件名提取上一级负责人，并与事件中已有的 `source_report_owners` 合并去重。中心结果公开显示 `来源负责人`；个人输入生成的第一级部门结果没有上游负责人时不显示该字段。

最终事件再次执行关键词过滤和保留门槛，再通过 `MarkdownEventStore` 写入当前 scope，并由飞书 bot 发给当前登录用户自己。

空目录、无有效文件或所有事件被过滤时，scope 可以生成空汇总并以 warning 说明原因。

## 12. Trace

开启：

```dotenv
WORKTRACE_COLLECTED_MERGE_TRACE=true
WORKTRACE_COLLECTED_MERGE_TRACE_ROOT=data/debug/collected_merge
WORKTRACE_COLLECTED_MERGE_RETRYABLE_ERROR_LIMIT=1
WORKTRACE_COLLECTED_MERGE_RETRY_DELAY_SECONDS=2
```

每个 scope 会记录 `source-audit.json`、step JSON、对应 prompt、`summary.json` 和 `summary.md`。新运行先清理当前 scope 的旧 trace 文件，不影响其他子 scope。模型调用前先写 step，失败也会生成 summary。内容包括：

- prompt 估算 token、`max_model_input_tokens` 上限和辅助字符数
- 每个来源的完整/实际发送字符数、是否缩短及候选摘要来源
- 来源文件/事件指标
- 本轮阶段类型、完整 `input_events` 和 `deterministic_groups`；候选发现 prompt 只包含临时 `conversation_groups` 和 Python 计算后的 `evidence_relations`
- 原始 group 和字段缺失统计
- 高风险复核触发原因、复核前后分组和来源覆盖
- 正文覆盖错误、局部重试和元数据回填 warning
- 跨工作流放行或拆组产生的 `boundary_warnings`
- 敏感/排除过滤和保留过滤结果
- 最终保留事件
- 每个 step 的阶段、状态、批次、尝试次数、重试原因和错误摘要

`summary.json` 和 `summary.md` 还包含 Python 计算的 `quality_summary`：输入/过滤后/输出事件数、单条组和多来源组数量、单组最大来源数、输入/输出正文字符数、两类输出输入比例、来源事件覆盖率、来源负责人数量、高风险/已复核/被拆组数量、正文重试数、提示缩短数和是否需要复核。比例只用于人工检查，不作为强制减少门槛；一个人部门或当天没有重复事项时，输出事件数允许等于输入事件数。

## 13. JSON 结果

`CollectedMergeRunResult` 主要包含：

- `target_date`
- `input_dir`
- `output_path`
- `source_file_count`
- `source_event_count`
- `merged_event_count`
- `skipped_file_count`
- `partial_file_count`
- `quality_summary`
- `warning_messages`
- `self_delivery_status`
- `self_delivery_target`
- `self_delivery_error`

有一级子目录时，根 scope 和子 scope 的结果会统一反映在本次运行摘要中。

## 14. 当前代码落点

- `src/worktrace/collected_merge.py`
- `src/worktrace/analyzers/prompts.py`
- `src/worktrace/analyzers/output_schemas.py`
- `src/worktrace/pipeline/sensitive_filter.py`
- `src/worktrace/pipeline/retention_filter.py`
- `src/worktrace/stores/markdown.py`
- `src/worktrace/delivery/feishu_cli.py`
- `config/collected_merge.json`
