---
name: worktrace
description: 帮员工从指定日期里与自己直接相关的飞书工作聊天中提炼工作事件，生成脱敏后的个人 Markdown，并把结果先发给员工自己；也支持管理人员把多人已生成的 WorkTrace Markdown 放入 merge_inbox 后做团队汇总合并。默认“跑某日数据”指个人日报，只有用户明确提到合并、收集、管理、团队汇总或 merge_inbox 时才执行多人合并。
---

# WorkTrace

## 用途

WorkTrace 用于从飞书聊天中提取指定日期里与本人直接相关的工作沟通内容，围绕本人发言上下文整理工作事件，调用兼容 OpenAI `Responses API` 的在线模型做结构化提炼，并将结果写入本地 Markdown 文件，随后通过飞书 CLI 机器人身份把文件发给当前登录用户自己。

这个 skill 的目标是帮助员工整理自己的工作事件，不是为了抓取私人聊天，也不是为了直接替员工向上级发汇总。

WorkTrace 另有一个管理人员汇总模式：管理人员先收集多人已经生成的 WorkTrace Markdown，放入 `merge_inbox/YYYY/MM/DD/`，再执行多人事件合并；日期根目录和一级子目录分别作为独立合并范围，各自生成本目录 `YYYY-MM-DD-登录人姓名-merged.md`。该模式不重新读取原始飞书聊天。

## 使用方式

当用户要求回顾某一天与自己直接相关的工作聊天、从飞书聊天生成个人工作事件记录、或将当天沟通整理成结构化事件时，使用本 skill。

意图判定规则：

- 用户只说“跑一下 29 日数据”“生成 29 日数据”“处理 2026-06-29”这类模糊说法时，默认执行个人日报流程。
- 用户明确说“合并 29 日收集的 MD”“管理人员汇总”“团队汇总”“多人合并”“merge_inbox 里的文件”“生成部门事件MD”时，执行管理人员汇总流程。
- 如果用户身份是管理人员，但表达仍然只是“跑一下 29 日数据”，不要直接推断为多人合并；应先确认是个人日报还是合并已收集 Markdown。
- 如果用户同时提到“收集的文件/多人 Markdown/合并”，即使没有写出 `merge-collected`，也按管理人员汇总流程处理。

每次使用前，都必须先检查用户是否已经提供本地在线模型配置；如果没有配置，必须中止执行并明确要求用户先提供：

- 必须在仓库本地单独配置在线模型参数
- 推荐使用仓库根目录 `.env`
- `WORKTRACE_LLM_BASE_URL`、`WORKTRACE_LLM_MODEL`、`WORKTRACE_LLM_API_KEY` 缺一不可
- `WORKTRACE_LLM_REASONING_EFFORT` 默认应为 `none`
- 这些内容不能提交到 git 仓库

优先按以下流程执行：

1. 确认目标日期。
2. 在仓库根目录执行 WorkTrace 的 Python 入口，而不是直接在 `SKILL.md` 中实现业务逻辑。
3. 让 Python 负责聊天抓取、窗口裁剪、批量组装、模型信号和证据校验、固定保留规则、统计、全日分组完整性、文件链接聚合、Markdown 写入和自送达。
4. 每日 Markdown 文件创建成功后，通过飞书 CLI 机器人身份把结果发给当前登录用户自己。
5. 仅在需要语义提取时调用在线模型做批量分析。
6. 主流程默认且强制使用 `/no_think` 与请求体关闭推理配置。
7. 在线文字请求不等待；发生网络、超时、429、5xx、流式 JSON 异常、空结果或无效 JSON 时，按 `config/llm_retry.json` 的 `online_request_retry_limit=1` 只对当前请求再试 Online 1 次，仍失败才切到 Codex，下一请求仍优先在线。Codex 按同一配置的 `0-1` 秒范围调用；图片摘要只走在线图片能力。
8. 调试模式只增加 trace 和日志，不改变模型线路。除非用户明确要求做单独的 Codex 后端诊断，否则不得通过临时配置、包装命令或代码参数把整次个人日报或多人汇总强制切到 Codex。
9. 在线请求成功返回、但结果未通过 Python 证据或结构校验时，先按程序现有配置用 Online 局部重试当前请求；结果质量错误在局部重试用尽后，只把当前请求交给 Codex 再执行一次。Codex 结果合法时继续流程，下一请求仍优先 Online；普通结构化任务中 Codex 失败或结果仍不合法时停止整次生成。全日分组的专用边界是：Codex 技术调用失败时终止；Codex 返回但仍非法时保留完全合法组，其余候选拆成单例并记录 warning。局部强关联复核失败或持续非法时保留复核前分组并记录 warning。不得擅自增加重试次数、重新运行整次流程或把整次流程切换到 Codex。

个人日报临时协作复核约定：

- 首次流程先确认本人是否真实参与；本人参与本身不等于事件值得保留。
- 只有 `config/retention_policy.json` 配置命中的边界候选才增加局部模型复核；没有候选时不增加调用。
- 模型读取候选对应的原聊天，只返回临时协作信号、实质工作信号和真实消息 ID，不返回最终保留/删除决定，也不计算数量。
- Python 不根据聊天文字判断语义，不增加全局排除词；只校验模型结果是否完整、信号类型是否合法、证据是否属于当前候选，再执行配置确定的固定规则。
- 任一合法实质工作信号优先保留；只有临时协作信号时删除；两类合法信号都没有时按当前配置删除。
- 模型结果缺失、重复、字段不完整或证据非法时只重试当前批次；技术失败或重试后仍错误时整次生成失败且不写文件。
- 正常删除不产生 warning，复核数量和删除数量全部由 Python 写入 CLI JSON 的 `retention_review_summary`。
- 旧个人 MD 和部门汇总不追溯处理，必须重新生成个人日报后才应用新规则。

个人事件事实复核约定：

- 首次提炼为标题、正文、主要动作、具体对象和保留依据返回 `fact_items`，每项引用真实来源消息 ID；语义风险只使用 `config/retention_policy.json` 的 `fact_risk_flags`。
- 事实证据不完整、来源消息或参与人数达到配置阈值，或命中配置风险信号时，在全日分组前增加一次局部模型事实复核；没有候选时不增加调用。
- 模型读取原聊天判断对比案例、地点、对象、责任人、建议和结论是否得到支持，可以确认、删除或改写无证据内容；复杂、多步骤事件本身不是删除理由。
- 每个事实复核请求只包含一个候选；模型只返回一次 `supported`、`fact_items` 和 `removed_claims`，Python 从 `fact_items` 派生标题、正文、主要动作、具体对象和保留依据，不要求模型在外层重复这些文字字段。
- 请求 Function 参数结构将 `draft_id` 固定为当前候选，并把证据消息 ID 限制为当前候选的合法枚举；Python 不阅读聊天文字判断事实含义，只检查唯一候选是否返回、事实字段是否完整覆盖，再执行配置中的无依据事件处理规则。
- `supported=true` 必须同时有合法证据支持非空标题、正文、具体对象和保留依据；任一必填字段无法得到支持时返回 `supported=false`，Python 按配置删除该事件。
- 不同候选最多按 `config/llm_retry.json` 同时处理 3 条，同一候选内部重试保持顺序。复核结果缺失、重复、字段不完整、引用非法证据或覆盖不完整时只重试当前候选；技术失败或重试后仍错误时整次生成失败且不写文件。
- 选择数、确认数、修订数、无依据删除数、批次数和重试数由 Python 写入 `personal_fact_review_summary`；Markdown 不新增可见字段。
- 使用 `--debug-output` 时，两类局部复核分别写入 `retention_review.json` 和 `personal_fact_review.json`，保留每次成功或失败尝试的候选摘要、证据范围、模型返回和 Python 校验结果，但不额外复制整段原聊天。调试回放的 `llm_usage_summary` 按调用类型汇总次数、token 和耗时；事实复核并发阶段必须用 `personal_fact_review_all` 的墙钟耗时判断，不能把各候选累计耗时当作实际运行耗时。

个人全日事件分组约定：

- 新链路固定为“候选事件 -> 全日事件分组 -> Python 完整性校验 -> 强关联漏合并复核 -> 最终事件”。初始分组提示词不发送会话 ID 和片段 ID，并完整发送 `config/event_grouping.json` 的成立条件、排除条件和负面示例。全日 Function 分别返回 `merged_groups` 和 `singleton_draft_ids`；多事件组必须给出具体共同对象、配置允许的理由和逐条覆盖全部成员及其各自证据的 `member_connections`，稳定组编号由 Python 生成。
- Python 检查两个数组完整且互斥，候选无遗漏、无重复，主事件属于组内，每个成员恰好说明一次且证据属于该成员；全部单例仍是合法结果。
- 同一来源片段、直接 reply/quote、共享来源消息和共享文件建立强关联；同一会话不单独触发。强关联跨越现有分组时最多三路并行局部复核，只能合并完整现有组，不能拆散已合法组。
- 个人与多人分组语义说明统一读取 `config/event_grouping.json`；Python 不读取聊天文字判断业务含义。
- `--debug-output` 在 `_merge_day_candidates/` 写入 `input.json`、`prompt.txt`、`grouping_attempts.json`、`day_group_review.json` 和 `resolved_groups.json`。CLI 与回放 `summary.json` 的 `day_grouping_summary` 由 Python 计算。
- 旧 Markdown 和旧 trace 中的工作流字段允许读取但会丢弃；新 Markdown、缓存和 trace 不再生成这些字段。

个人日报命令：

```bash
python -m src.worktrace.cli --date YYYY-MM-DD
```

管理人员汇总命令：

```bash
python -m src.worktrace.cli merge-collected --date YYYY-MM-DD
```

管理人员汇总模式约定：

- 输入目录固定为 `merge_inbox/YYYY/MM/DD/`。
- 来源文件名只要能识别出日期和姓名成分即可，例如 `YYYY-MM-DD-姓名.md`、`姓名-YYYY-MM-DD.md`、`姓名_YYYY-MM-DD.md`；上游 `YYYY-MM-DD-姓名-merged.md` 也支持继续参与汇总。
- 多人汇总只接受带 v2 会话指纹的新版个人或上游汇总 Markdown；任一事件缺少会话证据时整次停止，必须先重新生成来源文件。
- 部门负责人和中心负责人使用同一个命令。部门负责人先汇总个人 MD，中心负责人再人工收集各部门 `*-merged.md` 汇总；代码不自动编排层级。
- 个人 MD 与已经包含该人员的部门 MD 可以同时输入；程序不比较 `source_event_ids`，不拦截，也不提示重复来源，文件组合由负责人人工控制。
- 同一天同一会话只用于发现可能属于同一事项的候选，最终仍由模型结合内容确认，不自动强制合并。
- 多人候选阶段默认使用完整事件正文，并按 `model_input_batch_target_tokens=5200` 的完整输入估算优先分批。固定结构的 Online 请求使用任务专用 Function Calling；估算取“最终提示词、当前合法参数示例、证据编号、重试错误和 `/no_think` 加完整 tools 与 `tool_choice`”同“相同提示词加 Codex 完整 output-schema”两者的较大值。超过目标时关系优先分批；已经拆到最小必要输入仍超过目标时标记为 `oversized_singleton` 并发送，不设置额外的本地绝对上限。重试反馈使当前请求超限时标记 `oversized_retry` 后发送。
- `config/event_grouping.json` 是个人与多人分组语义说明的共同来源：每个 `group_reason_definitions` 项配置 `acceptance_rules` 和 `rejection_rules`，具体中文判断规则不得复制到 Python；`config/collected_merge.json` 只保留多人高风险复核开关和阈值。模型能看到 Python 编号的 `MSG-xxx`、`FILE-xxx` 证据目录，但新结果只返回 `semantic_reasons`、`reason_detail`、逐条覆盖全部成员的 `member_connections` 和 `risk_flags`，不再返回 `evidence_relation_ids`，也不能直接声明 `shared_message`、`shared_file` 或内部 `group_reason`。Python 只使用端点都在当前组内的关系，按稳定目录顺序计算连接全部成员的最小证据集合，并恢复内部原因；局部证据只写审计，不作为全组合并依据。内部 `evidence_relation_ids` 仅保存 Python 计算结果并兼容旧 trace；同会话候选放入 `candidate_discovery_context`，不用输出分组字段或组编号。逐事件说明遗漏、重复、组外引用或空说明，以及重复事件编号和单成员合并组，都必须进入当前请求局部重试，不得静默修复。
- 多条候选在来源事件达到 10 条、来源文件达到 4 个、跨批、分组修复、同一会话连接多个无共同消息/文件的部分、无完整共同证据且非空 `object_hint` 不一致，或模型标记 `broad_object` 时触发复核，单条候选直接保留。跨批只协调存在共同消息指纹或共同文件的候选；多事件子组仍需合法依据和自己的 `reason_detail`。最多三路复核并保持原候选顺序。高风险复核 Function 示例采用保守拆分，不预填原组或 `same_object`。拆组时只需一条顶层 `split_reason` 说明整体业务差异；旧记录中任一子组已有理由也兼容接受，完全没有理由时拒绝拆分、保留原组并记录告警。
- 正式正文必须完整覆盖锁定组并给出关键事实来源；局部重试后仍不完整时不写文件。单条事件组直接保留，不增加模型调用。
- 日期根目录和每个一级子目录分别作为独立合并范围；更深层目录不递归处理。
- 每个合并范围输出本目录 `YYYY-MM-DD-登录人姓名-merged.md`。
- 团队汇总文件会公开保留来源人员，并在隐藏信息中逐级保留来源事件 ID；中心结果还公开显示从上游 `*-merged.md` 文件名提取并逐级保留的来源负责人。
- 缺少当前登录人的个人 MD 时静默执行普通汇总，不产生 warning。
- 输入/输出数量、字符数、覆盖率、校验错误、重试原因和复核触发统计全部由 Python 计算，并进入 CLI JSON 与 trace summary；调试模式只增加记录，不改变 Online 局部重试 1 次和当前请求 Codex 备用 1 次的线路；不要求每一级事件数必须减少。
- `python3 scripts/replay_collected_review_failures.py --trace-root <trace目录> --steps <编号列表> --output-dir <输出目录>` 可离线复盘候选分组和高风险复核。旧 trace 使用 `legacy_audit`，不补造 `member_connections`；新实验结果使用 `current` 完整执行新协议校验。该脚本不调用模型，也不生成正式 Markdown。
- 每个生成的团队汇总文件都会通过飞书 CLI 机器人身份发送给当前登录用户自己。
- 更多细节见 `docs/collected-people-merge-plan.md`。

## 约束

- 仓库根目录就是 skill 根目录，不使用单独的 `skill/` 子目录。
- 核心实现应放在 `src/`，测试放在 `tests/`，设计文档放在 `docs/`。
- 不要让 LLM 参与数据计算；统计或计算必须由 Python 完成。
- 个人保留提示、既有业务词、临时协作与事实复核条件和语义信号说明维护在 `config/retention_policy.json`；个人和多人分组理由的描述、成立条件和排除条件维护在 `config/event_grouping.json`；多人高风险复核开关和阈值维护在 `config/collected_merge.json`。不得在 Python 中新增聊天关键词或具体中文业务判断规则。
- 个人日报和多人汇总统一使用默认值为 `5200` 的 `model_input_batch_target_tokens` 作为模型输入估算目标，不另设多人合并字符阈值。分批和调用前检查必须调用同一个 Python 估算函数，并取 Online Function 定义与 `tool_choice`、Codex 完整 output-schema 两种估算的较大值；模型名、URL、API Key、timeout 和 stream 不计入。会话分段窗口、锚点降级批次、全日候选分组及多人汇总等仍可拆的组合输入必须继续拆分；最小必要输入仍超过目标时允许发送，并在调试记录中保存两条线路估算、目标值、超限原因、实际 token 和估算差。该值不是 HTTP 字节数或服务端上下文上限。
- `WORKTRACE_LLM_STREAM` 是 Online 文字和图片请求的唯一流式开关，默认 `false`。每次请求重新读取配置，创建并关闭独立 OpenAI 和 HTTP 客户端；固定结构请求强制且只允许调用一次预期 Function，显式开启流式时按调用 ID 拼接 Function 参数。普通文字或图片理解不强制 Function Calling。请求级可重试错误按配置再试 Online 1 次，仍失败才将当前请求交给 Codex；Python 校验失败则先用 Online 局部重试当前请求并反馈具体错误，结果质量局部重试用尽后再将当前请求交给 Codex 一次。下一请求重新优先 Online。Codex 失败或结果仍不合法时停止整次生成。调试、诊断或一次运行失败都不构成整次切换后端的授权；需要改变全局后端或重试次数时，先停止并取得用户明确同意。
- 原始聊天内容不应长期落盘，长期保留的只有结构化事件清单。
- 最终对员工可见的 Markdown 应优先保留 `日期`、`事件标题`、`内容`、`具体对象`、中文 `保留理由`、作为来源证据的 `保留依据`、`涉及文件`。
- 员工最终产物不应显示群名、open_id、消息 ID、会话 ID 或参与人名单；事件正文可在责任分工、任务指派、确认沟通对象等确有必要时保留姓名。
- 管理人员汇总产物例外：允许显示来源人员和上游来源负责人；来源事件 ID 只在隐藏信息中保留，用于团队事项追溯。
- 文档链接的主要用途是帮助员工以后回忆事件细节，不是让模型围绕链接做推理。
- 每次成功生成当天 Markdown 文件后，都应通过飞书 CLI 机器人身份将结果发送给当前登录用户自己，作为自送达副本。
- 管理人员汇总模式生成规范化的 `YYYY-MM-DD-登录人姓名-merged.md`，并把每个结果文件发送给当前登录用户自己。

员工可以直接说：

- `帮我生成 2026-07-06 的个人事件MD`
- `跑一下 2026-07-06 的 WorkTrace 个人日报`

管理人员可以直接说：

- `帮我合并 2026-07-06 的部门事件MD`
- `把 merge_inbox 里 2026-07-06 的多人日报合并成部门事件MD`

员工得到规范化的 `YYYY-MM-DD-姓名.md`；管理人员得到规范化的 `YYYY-MM-DD-登录人姓名-merged.md`。

## 隐私说明

使用这个 skill 时，应当明确向用户说明以下事实：

- WorkTrace 只处理目标日期内本人发过消息或做过 reaction 的会话
- WorkTrace 只尝试提取与本人直接相关的工作事项
- WorkTrace 不输出群名、内部 ID 或参与人名单，只在责任分工等确有必要时保留姓名
- WorkTrace 默认不长期保存原始聊天记录
- WorkTrace 为补齐 reply/quote 直接关系或模型请求的相邻上下文，可能临时读取目标日期之外的直接关联消息，但事件日期仍是目标日期
- WorkTrace 会把经过裁剪和压缩的必要消息正文、会话名、发送者信息、消息和会话标识、链接 URL/标题、附件文件名，以及启用的图片或按需读取的附件/文档正文发送到用户自己配置的在线 LLM 服务
- 最终 Markdown 隐藏群名和内部 ID，不代表在线模型输入不包含这些上下文元数据
- WorkTrace 默认把结果先发送给员工自己，而不是自动发给领导

不要夸大安全性，也不要承诺系统当前做不到的事情。

## 参考

- 详细设计：`docs/detailed-design.md`
- 管理人员多人合并设计：`docs/collected-people-merge-plan.md`
- 项目说明：`README.md`
