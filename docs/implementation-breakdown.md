# WorkTrace 当前实现拆解

## 1. 文档定位

本文档是“从业务步骤找到代码”的模块索引。完整流程和数据边界见 [detailed-design.md](detailed-design.md)。

## 2. 入口层

| 文件 | 当前职责 |
| --- | --- |
| `src/worktrace/cli.py` | 解析个人日报、`merge-collected`、`sync-reaction-catalog`，加载配置，输出 JSON 和退出码 |
| `src/worktrace/preflight.py` | 检查 Python、lark-cli user 身份、在线模型、Codex 备用命令、数据目录和时区 |
| `src/worktrace/config.py` | 合并 `RuntimeConfig`、`.env`、进程环境变量及各类 JSON 配置 |
| `src/worktrace/factories.py` | 装配聊天源、内容解析器、analyzer、store 和投递通道 |

个人日报会执行 preflight；两个子命令有各自的前置依赖，不复用个人日报整套 preflight。

## 3. 飞书输入与内容解析

| 文件 | 当前职责 |
| --- | --- |
| `sources/feishu_cli.py` | 当前用户身份、本人发言/reaction 会话发现、消息分页、消息标准化、附件和 reaction 解析 |
| `reaction_catalog.py` | 加载本地 reaction 目录并补充名称、说明、语义 |
| `reaction_catalogs/feishu.py` | 显式同步飞书 reaction 目录与图片资源 |
| `resolvers/feishu_message.py` | 文本清洗、链接标题、飞书 Docx/Wiki 正文、文本附件和图片摘要接入 |
| `attachments.py` | 按配置提取受支持的小型文本附件 |
| `vision.py` | 通过当前 Responses API 模型生成图片工作内容摘要 |

## 4. 个人日报编排

`src/worktrace/runner.py` 是主编排器，当前默认在线链路依次执行：

```mermaid
flowchart LR
    A["采集消息"] --> B["本地过滤"] --> C["确定性初始窗口"] --> D["LLM 分段并保存中间结果"]
    D --> E["片段组批并提炼动作/参与方式"] --> F["上下文重试"] --> G["候选与证据校验"]
    G --> H["临时协作局部复核"] --> I["个人事实局部复核"] --> J["跨会话初始分组"]
    J --> K["工作流权威分组"] --> L["增强事件物化"]
    L --> M["消息/会话指纹 + 文件标识"] --> N["Markdown + 自发送"]
```

关键方法：

- `DailyTraceRunner.run(...)`：单日总流程
- `_analyze_segmented_conversations(...)`：锚点、分段、组批和回退总入口
- `_analyze_segment_batch_with_retry(...)`：片段批量分析与协议重试
- `_retry_segment_context(...)`：按片段补消息、附件和链接正文
- `_analyze_anchor_fallback(...)`：分段失败后，直接从本人参与的聊天窗口提炼
- `_review_retention_candidates(...)`：复核配置命中的临时协作边界候选，并由 Python 应用固定保留规则
- `_review_personal_event_facts(...)`：把高风险个人事件拆成单候选请求，最多 3 路并发复核事实证据；每个候选内部重试保持顺序
- `_review_personal_fact_batch_with_retry(...)`：执行单候选事实复核、协议校验和有限重试
- `_resolve_workstream_groups(...)`：通过独立 assignment 生成工作流权威分组；失败时回退到初始模型组整合
- `_attach_event_file_links(...)`：按显式引用或精确附件文件名证据附加文件

## 5. Pipeline 模块

| 文件 | 当前职责 |
| --- | --- |
| `pipeline/filtering.py` | 系统消息、撤回、群事件和空消息过滤 |
| `pipeline/initial_windows.py` | 正式主链的群聊锚点聚合、私聊整日窗口和直接关系上下文 |
| `pipeline/anchors.py` | 固定前后条数的兼容/实验锚点窗口 |
| `pipeline/conversation_segments.py` | response signal、硬边界、分段校验、主消息去重、片段组批 |
| `pipeline/context_expansion.py` | earlier/later、附件和链接正文扩窗 |
| `pipeline/required_image_context.py` | 首轮前补本人发送或本人 reply/quote 关联的图片摘要 |
| `pipeline/llm_checkpoints.py` | 按精确输入指纹临时保存分段与事件提炼结果 |
| `pipeline/validation.py` | analyzer 返回 ID、参与类型、本人证据和分组覆盖校验/修复 |
| `pipeline/direct_relation_filter.py` | 旧 analyzer 和分段失败后直接提炼路径的本人关联检查 |
| `pipeline/sensitive_filter.py` | 三阶段配置关键词过滤 |
| `pipeline/retention_filter.py` | 具体对象、保留理由、保留依据和低价值类型门槛 |
| `pipeline/retention_review.py` | 临时协作边界候选选择、组批、模型结果校验和固定保留规则 |
| `pipeline/personal_fact_review.py` | 个人事实风险选择、组批、事实证据校验和修订应用 |
| `pipeline/cross_conversation_merge.py` | 分组归并、主草稿选择，以及工作流、动作、参与方式的 `MergedEventDraft` 物化 |
| `pipeline/workstream_resolution.py` | 根据结构化工作流分配结果生成校正分组 |
| `pipeline/event_merge.py` | 最终 `WorkEvent` 构建、稳定 ID 和消息证据指纹 |

`pipeline/conversation_first_pass.py` 仍用于不支持分段批处理的 analyzer 兼容路径；它不是当前默认 Online analyzer 的主入口。

## 6. Analyzer

| 文件 | 当前职责 |
| --- | --- |
| `analyzers/base.py` | 分段、片段批处理、临时协作复核、个人事实复核、旧批处理、分段失败后直接提炼、日级合并和多人合并接口 |
| `analyzers/online.py` | OpenAI Python SDK + Responses API 在线文字实现；固定结构使用任务专用 Function Calling，每次请求独立创建和关闭客户端 |
| `analyzers/codex.py` | Codex CLI 文字实现，使用线程安全的 0-1 秒调用间隔 |
| `analyzers/failover.py` | 在线文字请求首次发生可切换错误时，仅把当前请求改由 Codex 执行 |
| `analyzers/prompts.py` | 所有语义任务 prompt |
| `analyzers/function_calls.py` | `FunctionCallSpec`、任务专用 Function、动态 ID 枚举、典型参数示例和多人证据编号合同 |
| `analyzers/output_schemas.py` | Function 参数与 Codex output-schema 共用结构；事实复核按当前候选动态限制 `draft_id` 和合法证据 ID |
| `analyzers/protocol.py` | 模型 JSON 到领域对象的解析与引用恢复；事实复核从唯一一份 `fact_items` 派生六个文字字段 |

当前默认 `OnlineLLMAnalyzer` 实现 `segment_conversation(...)`、`analyze_segment_batch(...)`、`review_retention_candidates(...)` 和 `review_personal_event_facts(...)`，因此 `runner` 走分段及两类局部复核主链。是否支持分段和事实复核由能力检查决定，不通过配置字符串猜测。

## 7. 输出与投递

| 文件 | 当前职责 |
| --- | --- |
| `stores/markdown.py` | Markdown 新旧字段往返、隐藏合并信息、内部 ID 隐藏和 URL 敏感参数脱敏 |
| `delivery/feishu_cli.py` | 规范化发送文件名，通过 bot 把文件发送给当前 user |
| `models.py` | 消息、锚点、片段、候选、合并草稿、事件和运行结果模型 |

## 8. 多人汇总

`src/worktrace/collected_merge.py` 负责完整的 `merge-collected` 链路：

1. 根目录和一级子目录分别建立 merge scope
2. 当前层 Markdown 解析、来源姓名识别、尾部残缺事件部分恢复和坏文件跳过
3. 来源事件配置关键词过滤与保留门槛
4. 全 scope 校验 v2 同日会话指纹，相同 `event_id` 建立确定性组
5. Python 按共同消息、文件和同日会话建立关系集合，模型使用完整事件正文返回候选组、候选摘要、语义理由、`member_connections` 和 `risk_flags`；新模型输出不再返回 `evidence_relation_ids`
6. Python 在模型分组后排除组外证据端点，并按稳定目录顺序计算连接全组的最小消息/文件证据集合；大 prompt 按关系集合优先分批，跨批只协调共享消息指纹或文件的候选
7. 单条组直接保留；多条组达到配置阈值、跨批、分组修复、工作流冲突、无完整证据且对象不一致或标记 `broad_object` 时，最多三路高风险复核。复核使用独立的保守拆分 Function 示例
8. 正式内容按锁定候选组分批生成，并返回完整 `covered_draft_ids` 和带来源的 `fact_items`
9. 可切换在线错误立即由 Codex 重做当前请求，不重复在线调用；结果质量错误按 Online 首次请求、Online 局部重试 1 次、Codex 当前请求备用 1 次执行，最后仍失败则终止本次合并且不写文件
10. 聚合工作流、动作、协作方式、消息指纹、会话指纹、文件标识、来源人员、事件 ID 和上一级负责人
11. Python 计算 scope 和整次运行的 `quality_summary`，团队 `WorkEvent` 最终过滤、写入和自发送

相关专题见 [collected-people-merge-plan.md](collected-people-merge-plan.md)。

## 9. 配置来源

| 来源 | 内容 |
| --- | --- |
| `RuntimeConfig` | 流程阈值、目录、analyzer backend 和默认运行参数；`model_input_batch_target_tokens` 统一控制个人日报和多人合并的分批目标 |
| `.env` / 环境变量 | 在线模型和多人汇总 trace/retry 覆盖项 |
| `config/event_rules.json` | 敏感、排除和本人指派关键词 |
| `config/event_metadata.json` | 本人参与方式英文键、中文显示名和排序 |
| `config/conversation_blacklist.json` | 整会话排除 |
| `config/conversation_window.json` | 群聊锚点聚合、初始上下文和按需扩窗阈值 |
| `config/llm_retry.json` | 分段/提炼重试、流式首次返回超时、Codex 间隔，以及切分、提炼、个人事实复核和多人高风险复核并发数 |
| `config/retention_policy.json` | 个人事件保留提示、结构化业务词、临时协作复核、事实复核条件和模型信号定义 |
| `config/collected_merge.json` | 多人汇总高风险复核开关、事件数/文件数阈值、对象冲突与宽泛对象复核条件，以及合并原因的描述、`acceptance_rules` 和 `rejection_rules` |
| `config/attachment_text.json` | 文本附件提取限制 |
| `config/image_summary.json` | 图片摘要限制和提示词 |
| `config/reaction_catalogs/*.json` | reaction 本地语义目录 |

可调整的敏感、普通排除、本人指派、个人保留业务词、复核信号说明和多人合并中文判断规则必须进入配置文件，不应继续写在代码中。`retention_filter.py` 负责结构化保留门槛；两类个人复核模块负责模型信号、事实证据的校验和固定处理规则；多人合并 Python 只比较编号、证据连接和标准化结构字段，不根据新增聊天关键词判断语义。

## 10. 调试入口

- 个人日报：`--debug-output`，目录 `data/debug/conversations/<date>/`；失败轮次保存 `failure.json`，单片段回退使用 `fallback-01/`，直接提炼回退使用 `_anchor_fallback/`；`retention_review.json` 和 `personal_fact_review.json` 保存两类局部复核尝试及 Python 校验结果，`llm_usage.json` 保存按调用类型的 token 和耗时，`final_events.json` 保存最终草稿、事件和过滤 warning
- 回放报告：`replay_day_with_trace.py` 在执行前写入 `run_status.json`，实时显示并保存子进程阶段日志，结束后更新 `success/failed`，同时把 `llm_usage.json` 汇总到 `llm_usage_summary`；`diagnose_collected_merge_rolling.py` 在每次模型调用前写入 `running` 步骤并在完成或异常后更新结果；`report_replay_timings.py` 分开输出事实复核候选累计耗时和 `personal_fact_review_all` 墙钟耗时；`report_replay_call_inputs.py` 分开统计文字调用与图片摘要
- 多人汇总：`WORKTRACE_COLLECTED_MERGE_TRACE=true`，目录默认 `data/debug/collected_merge/<date>/`；候选和复核 step 写入 `grouping_protocol_version: 2`、`evidence_audit`、`semantic_audit`、`python_validation.errors`、输入、Function 与提示词，`summary.json` 和 `summary.md` 保存 Python 计算的校验错误、重试原因、复核触发和质量统计，失败也生成 summary
- 候选/复核离线回放：`scripts/replay_collected_review_failures.py --trace-root <trace目录> --steps <编号列表> --output-dir <输出目录>` 可直接回放候选分组和高风险复核；旧 trace 使用 `legacy_audit` 且不补造 `member_connections`，新实验结果使用 `current` 完整校验。汇总按阶段列出旧结果问题、新规则处理和是否仍需模型复核，并明确 `model_call_count=0`；脚本不调用模型、不生成正式 Markdown
- 锚点独立实验：`python3 -m src.worktrace.anchor_experiment ...`

独立锚点实验用于对比协议和缓存行为，不等同于正式日报；正式日报虽然已经使用本人参与的聊天窗口，并在分段失败后直接从这些窗口提炼，但不使用实验入口生成最终 Markdown。正式 `--resume` 只读取 `pipeline/llm_checkpoints.py` 保存的临时分段/提炼结果，不读取实验锚点缓存。
