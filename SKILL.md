---
name: worktrace
description: 帮员工从指定日期里与自己直接相关的飞书工作聊天中提炼工作事件，生成脱敏后的个人 Markdown，并把结果先发给员工自己；也支持管理人员把多人已生成的 WorkTrace Markdown 放入 merge_inbox 后做团队汇总合并。用户提到运行报错、帮我排查、速度太慢、结果不对、生成诊断报告、用调试模式重新跑时，也使用本 skill 自动执行调试并生成安全 Markdown 报告。用户明确要求“生成并上传”时，生成完成后转用 worktrace-upload-md 技能上传，不调用已移除的 WorkTrace CLI 上传命令。默认“跑某日数据”指个人日报，只有用户明确提到合并、收集、管理、团队汇总或 merge_inbox 时才执行多人合并。
---

# WorkTrace

## 用途

WorkTrace 用于从飞书聊天中提取指定日期里与本人直接相关的工作沟通内容，围绕本人发言上下文整理工作事件，通过 Codex 主线路和可配置为 Responses 或 Chat Completions 的 Online 备用线路做结构化提炼，并将结果写入本地 Markdown 文件，随后通过飞书 CLI 机器人身份把文件发给当前登录用户自己。

这个 skill 的目标是帮助员工整理自己的工作事件，不是为了抓取私人聊天，也不是为了直接替员工向上级发汇总。

WorkTrace 另有一个管理人员汇总模式：管理人员先收集多人已经生成的 WorkTrace Markdown，放入 `merge_inbox/YYYY/MM/DD/`，再执行多人事件合并；日期根目录和一级子目录分别作为独立合并范围，默认各自生成本目录 `YYYY-MM-DD-登录人姓名-merged.md`。传入指定姓名时才使用该姓名；离线模式还会跳过飞书 CLI。该模式不重新读取原始飞书聊天。

## 使用方式

当用户要求回顾某一天与自己直接相关的工作聊天、从飞书聊天生成个人工作事件记录、或将当天沟通整理成结构化事件时，使用本 skill。

意图判定规则：

- 用户只说“跑一下 29 日数据”“生成 29 日数据”“处理 2026-06-29”这类模糊说法时，默认执行个人日报流程。
- 用户明确说“合并 29 日收集的 MD”“管理人员汇总”“团队汇总”“多人合并”“merge_inbox 里的文件”“生成部门事件MD”时，执行管理人员汇总流程。
- 如果用户身份是管理人员，但表达仍然只是“跑一下 29 日数据”，不要直接推断为多人合并；应先确认是个人日报还是合并已收集 Markdown。
- 如果用户同时提到“收集的文件/多人 Markdown/合并”，即使没有写出 `merge-collected`，也按管理人员汇总流程处理。
- 用户说“运行报错”“帮我排查”“速度太慢”“结果不对”“生成诊断报告”“用调试模式重新跑”时，按调试诊断流程执行。能从当前对话确定日期和个人/多人方式时直接运行；确实无法判断运行方式时只询问一次。
- 用户明确说“生成并上传”时，先按个人或多人规则完成生成并校验最终 CLI JSON 与 Markdown 文件，再使用 `worktrace-upload-md` 技能上传这个准确路径。只说“生成”时不得上传；只说“上传”时不得重新生成。

普通生成每次使用前都必须先检查仓库本地 `.env` 是否已经显式配置 Codex 主线路；缺少时中止普通生成并明确要求用户先提供：

- `WORKTRACE_CODEX_MODEL`、`WORKTRACE_CODEX_REASONING_EFFORT` 与五项 `WORKTRACE_CODEX_PROVIDER_*` 缺一不可
- 它们只从仓库本地 `.env` 读取，不继承个人 Codex 的模型或中转提供方配置；中转站 Key 仍由 Codex CLI 自己的认证存储提供
- 不硬编码模型名或推理强度枚举，由 `--preflight` 的正式严格 Schema 小探针验证组合
- Online 当前请求备用使用 `WORKTRACE_LLM_BASE_URL`、`WORKTRACE_LLM_MODEL` 和 `WORKTRACE_LLM_API_KEY`；`WORKTRACE_LLM_WIRE_API` 默认 `responses`，只在显式配置时切换为 `chat_completions`；缺少或不合法时禁用备用但不阻止 Codex 主线路
- 真实密钥和本地 `.env` 都不能提交到 git 仓库

诊断请求例外：不要因为配置缺失或 preflight 失败而由 Codex 提前中止。仍要执行带 `--debug-output` 的 CLI，让 WorkTrace 生成能够说明环境问题的安全诊断报告；正式日报流程是否继续仍由 CLI 自己决定。

优先按以下流程执行：

1. 确认目标日期。
2. 在仓库根目录执行 WorkTrace 的 Python 入口，而不是直接在 `SKILL.md` 中实现业务逻辑。
3. 让 Python 负责聊天抓取、窗口裁剪、批量组装、模型信号和证据校验、固定保留规则、统计、全日分组完整性、文件链接聚合、Markdown 写入和自送达。
4. 每日 Markdown 文件创建成功后，按 `config/self_delivery.json` 的 `enabled` 决定是否通过飞书 CLI 机器人身份发给当前登录用户自己；默认开启。
5. 仅在需要语义提取时调用 Codex 做批量分析；Online 只在当前请求的 Codex 技术失败后作为备用。
6. Online 备用保持 `/no_think` 与关闭思考：Responses 使用 `reasoning.effort=none`，Chat Completions 使用 `thinking.type=disabled`；Codex 不追加 `/no_think`，而是在完整契约提示词中明确 `strict=true` 与单次参数 JSON 提交要求。
7. 每个新请求先走 Codex。网络、超时、429、5xx、空结果和无效 JSON 时，按 `config/llm_retry.json` 的 `primary_request_retry_limit=1` 只对当前请求再试 Codex 1 次，仍失败才交给 Online 一次；下一个新请求重新优先 Codex。登录、权限、模型、推理强度、TLS 和配置错误不切换。两条线路都以 `WORKTRACE_LLM_TIMEOUT_SECONDS`（未配置时 180 秒）作为整次请求总时限；Codex 的全局同时调用上限为 3，文字、图片、表情补全和诊断报告共用。图片摘要也先走 Codex 普通文本任务，使用临时图片副本的 `--image`，不强制 Function Calling；Codex 返回工具调用等不合法图片结果，或命中 `config/image_summary.json` 配置的无法识别说法时，只把当前图片交给 Online 一次。正式流程和调试模式共用此线路，调试账本记录切换方向、原因和备用次数。
8. 调试模式只为正式日报或多人汇总增加 trace 和日志，不改变正式模型线路。任务结束后另有一次独立的诊断报告模型调用；报告模型只读取 Python 已脱敏并计算好的编号事实。除非用户明确要求做单独的 Codex 后端诊断，否则不得通过临时配置、包装命令或代码参数把整次个人日报或多人汇总强制切到 Codex。
9. Codex 成功返回、但结果未通过 Python 证据或结构校验时，先按程序现有配置用 Codex 局部重试当前请求，并把具体错误放入提示词；结果质量重试用尽后，只把当前请求交给 Online 一次。技术请求经过 Codex 重试和 Online 备用仍失败时直接执行该节点的失败策略，不消耗结果质量重试，也不重新从 Codex 开始一轮。Online 结果合法时继续流程，下一请求仍优先 Codex；普通结构化任务中 Online 失败或结果仍不合法时停止整次生成。全日分组的专用边界是：Online 技术调用失败时终止；Online 返回但仍非法时保留完全合法组，其余候选拆成单例并记录 warning。标题发现全部尝试失败时按没有候选继续并记录 warning；局部复核失败或持续非法时保留复核前分组并记录 warning。不得擅自增加重试次数或重新运行整次流程。

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
- `supported=true` 必须同时有合法证据支持非空标题、正文、具体对象和保留依据；候选只有局部表述缺少证据时先删除或改写局部内容，只有修订后任一必填字段仍无法得到支持时才返回 `supported=false`，Python 按配置删除该事件。
- 不同候选最多按 `config/llm_retry.json` 同时处理 3 条，同一候选内部重试保持顺序。复核结果缺失、重复、字段不完整、引用非法证据或覆盖不完整时只重试当前候选；技术失败或重试后仍错误时整次生成失败且不写文件。
- 选择数、确认数、修订数、无依据删除数、批次数和重试数由 Python 写入 `personal_fact_review_summary`；Markdown 不新增可见字段。
- 使用 `--debug-output` 时，两类局部复核分别写入 `retention_review.json` 和 `personal_fact_review.json`，保留每次成功或失败尝试的候选摘要、证据范围、模型返回和 Python 校验结果，但不额外复制整段原聊天。个人首次提炼、失败备用入口和全部最终事件正文改写的调试 JSON 使用 `personal_full`，事实复核使用 `personal_review_without_examples`；只记录事件生成配置版本、数量、模板方式和是否带案例，不复制完整规则或案例。调试回放的 `llm_usage_summary` 按调用类型汇总次数、token 和耗时，`event_generation_summary` 汇总配置状态；事实复核并发阶段必须用 `personal_fact_review_all` 的墙钟耗时判断，不能把各候选累计耗时当作实际运行耗时。

个人与团队事件生成统一读取 `config/event_generation.json`。个人首次提炼和失败备用入口使用完整事项边界、字段模板及脱敏正反例；个人事实复核只使用共同事实规则和字段模板，全部最终事件正式改写再使用完整案例。团队初步分组和完整复核只使用共同规则、团队事项边界及简短摘要模板，锁定成员后的全部最终事件正文才使用完整模板和案例。该配置不新增 Markdown 公开字段，不要求固定输出下一步、责任人或目标时间，也不建设跨日事项状态库；所有模板和案例继续计入当前 `model_input_batch_target_tokens` 输入估算。

个人全日事件分组约定：

- 新链路固定为“候选事件 -> 全日事件分组 -> Python 完整性校验 -> 全部组编号和标题候选发现 -> 完整内容局部复核 -> 最终事件”。初始分组提示词不发送会话 ID 和片段 ID，并完整发送 `config/event_grouping.json` 的成立条件、排除条件和正反示例。全日 Function 分别返回 `merged_groups` 和 `singleton_draft_ids`；多事件组必须给出具体共同对象、配置允许的理由和逐条覆盖全部成员及其各自证据的 `member_connections`，稳定组编号由 Python 生成。
- Python 检查两个数组完整且互斥，候选无遗漏、无重复，主事件属于组内，每个成员恰好说明一次且证据属于该成员；全部单例仍是合法结果。完整输入超过当前预算 profile 时按候选分批完成初步分组，再合并所有批次结果并统一校验、编号；不构造候选摘要，也不做摘要再次分组，跨批关系交给随后的全部组标题发现和完整内容复核。
- 初步分组后，`day_group_discovery` 单次提交全部组且每组严格只有 `group_id` 和 `title`；不发送日期、正文或其他分组阶段的正反例，组合标题由 Python 按稳定顺序覆盖初步组全部成员标题。模型必须逐组比较全部标题并完整返回 `group_checks`，每组列出可能相关的零个、一个或多个其他编号和非空理由；Python 校验全量覆盖后把重叠关系形成 `candidate_groups`。协议不包含特定日期、人员、业务关键词或固定长度片段。超过当前预算 profile 时不拆批、不跳过，仍整体提交并记录超限；全部尝试失败时放弃该节点，不阻止个人 Markdown 和本人送达。
- 同一来源片段、直接 reply/quote、共享来源消息、共享文件、按配置去除版本后缀后的同一非图片附件基础名称，以及标题发现候选建立复核范围；同一会话不单独触发。标题候选仍可包含任意多个组，重叠候选形成同一个完整范围，但模型实际指出的每条组间连接分别编号，允许同一范围内部分成立、部分分开。范围内每个原始候选都可脱离初步组重新组合。模型必须先逐条判断关系，再统一处理重叠关系并形成最终组，不得用初步组或预设的最终组反向解释关系。分开时可以返回关系两侧代表成员，必须分别说明两侧可独立汇报的目标或结果；Python 校验完整覆盖、关系逐条处理、成立成员真实同组、分开成员真实位于不同组、两侧证据和最终成员唯一性。每条关系只返回覆盖两侧判断所需的最少消息编号，Function 示例按关系两侧生成代表成员和代表证据，其中的决定只是字段结构占位，不代表当前关系结论。
- 最终单成员组和多成员组锁定后都使用 `personal_group_render` 重新生成覆盖全部成员的标题、正文和具体对象；最多同时处理 3 项，失败时使用 Python 确定性拼接结果并写 warning，不阻止个人 Markdown 和本人送达。
- 个人与多人分组语义说明统一读取 `config/event_grouping.json`；Python 不读取聊天文字判断业务含义。
- `--debug-output` 在 `_merge_day_candidates/` 写入 `input.json`、`prompt.txt`、`grouping_attempts.json`、`day_group_discovery.json`、`day_group_review.json`、`personal_group_render.json` 和 `resolved_groups.json`。完整回放成功后可用 `scripts/replay_failed_day_group_reviews.py` 只重放失败复核范围，结果写入 `day_group_review_replay.json`，不直接修改正式个人 MD。CLI 与回放 `summary.json` 的 `day_grouping_summary` 由 Python 计算。
- 旧 Markdown 和旧 trace 中的工作流字段允许读取但会丢弃；新 Markdown、缓存和 trace 不再生成这些字段。

个人日报命令：

```bash
python -m src.worktrace.cli --date YYYY-MM-DD
```

## 生成后上传

- WorkTrace CLI 只负责生成和本人送达，没有上传子命令。不得引用历史上传命令，也不得把 macOS 或某台机器的仓库路径写入技能或自动化。
- 用户明确要求“生成并上传”时，从本次最终 CLI JSON 取得实际 Markdown 路径和日报日期，然后转用已安装的 `worktrace-upload-md` 技能。该技能通过 HTTPS 接口上传，并分别使用 macOS 钥匙串或 Windows 当前用户 DPAPI 读取已保存的上传 Key。
- 上传前仍须校验文件名日期、文件大小和日报类型。首次请求不得主动覆盖；服务端要求覆盖时，必须取得用户对该日期的明确确认。
- 生成失败、文件不存在或候选不唯一时停止，不得尝试上传旧文件或猜测路径。

## 调试诊断执行规则

开始执行前用一句自然语言告知用户：调试会重新运行任务；自送达开启时，个人日报可能再次发送给本人，结束后还会增加一次报告模型调用；不要增加确认循环。

个人调试固定执行：

```bash
python3 -m src.worktrace.cli --date YYYY-MM-DD --debug-output
```

多人汇总调试固定执行：

```bash
python3 -m src.worktrace.cli --debug-output merge-collected --date YYYY-MM-DD
```

执行后必须：

1. 等待命令完全结束并读取最终 CLI JSON，不要只看中途日志。
2. 检查 `support_report.status`、`support_report.privacy_check` 和 `support_report.path` 指向的文件是否真实存在。
3. `generated_with_llm` 且 `privacy_check=passed` 时，把准确的 Markdown 路径交给用户。
4. `generated_after_llm_failure` 且 `privacy_check=passed` 时，说明基础报告可以发送，但大模型分析部分未完成。
5. `blocked` 或 `failed` 时，明确说明没有可发送的安全报告，绝不能把原始 trace 说成诊断报告或建议用户发送。
6. 不自动上传或发送报告，只把本地路径交给当前用户。不开启 `--debug-output` 时不要寻找或生成报告。

外发材料必须由 WorkTrace 报告生成器产生并通过隐私检查。不得自行读取原始聊天、prompt、模型返回或完整 trace 后整理外发内容；不得发送完整 `data/debug` 目录。报告只允许是 `data/debug/support_reports/worktrace-support-<随机编号>.md` 指向的单个 Markdown，不生成或索要 ZIP。

报告模型同样先走 Codex，再在可重试技术失败后使用 Online 一次；Online 配置缺失时不阻止报告使用 Codex。这个规则不改变个人日报或多人汇总的请求级主备边界。

管理人员汇总命令：

```bash
python -m src.worktrace.cli merge-collected --date YYYY-MM-DD
```

需要保留多人汇总 trace 时，使用全局 `--debug-output`；该参数必须放在
`merge-collected` 前，并沿用配置中的 `collected_merge_trace_root`：

```bash
python3 -m src.worktrace.cli --debug-output merge-collected --date YYYY-MM-DD
```

管理人员汇总模式约定：

- 输入目录固定为 `merge_inbox/YYYY/MM/DD/`。
- 来源文件名只要能识别出日期和姓名成分即可，例如 `YYYY-MM-DD-姓名.md`、`姓名-YYYY-MM-DD.md`、`姓名_YYYY-MM-DD.md`；上游 `YYYY-MM-DD-姓名-merged.md` 也支持继续参与汇总。
- 删除事件直接遗忘，不保存被删事件的编号、内容、指纹、删除数量或数量差值；只有损坏事件块才标记 `partial` 并告警。
- 有合法会话证据、当前人工修订类型或下级修订类型任一项即可参与多人汇总；有效 v1 事件三者都没有时整次停止并要求重新生成。
- Codex 或其他编辑器新增、修改的完整事件分别显示配置定义的人工新增、人工修改标记；隐藏信息损坏但正文完整时标记为类型无法确认，修订类型从部门继续传到中心。
- 部门负责人和中心负责人使用同一个命令。部门负责人先汇总个人 MD，中心负责人再人工收集各部门 `*-merged.md` 汇总；代码不自动编排层级。
- 个人 MD 与已经包含该人员的部门 MD 可以同时输入；程序不比较 `source_event_ids`，不拦截，也不提示重复来源，文件组合由负责人人工控制。
- 同一天同一会话只用于发现可能属于同一事项的候选，最终仍由模型结合内容确认，不自动强制合并。
- 多人候选阶段默认使用完整事件正文，并按当前 `model_input_batch_target_tokens` 的完整输入估算优先分批。固定结构任务使用同一份 `FunctionCallSpec`：Online 备用接收原生 Function Calling 的 `tools`、`tool_choice` 与 `strict:true`，Codex 主线路接收完整契约提示词和同一份 `--output-schema`。估算取“Online 示例、自检和 `/no_think` 加完整 tools 与 `tool_choice`”同“Codex 完整契约提示词加完整 output-schema”两者的较大值。超过目标时关系优先分批，各批初步组由 Python 直接合并。个人和多人候选阶段都不构造候选摘要、不做摘要再次分组；跨批关系交给全部组标题发现和完整内容复核。已经拆到最小必要输入仍超过目标时标记为 `oversized_singleton` 并发送，不设置额外的本地绝对上限。重试反馈使当前请求超限时标记 `oversized_retry` 后发送。
- `config/event_grouping.json` 是个人与多人分组语义说明的共同来源：每个 `group_reason_definitions` 项配置 `acceptance_rules` 和 `rejection_rules`，具体中文判断规则不得复制到 Python；`config/collected_merge.json` 只保留多人高风险复核开关和阈值。`same_deliverable_batch` 可用于同一批配套产物、同一交付物多个版本，以及不同时间、状态、地区或阶段形成但需要统一汇报或交付的连续产物。文件、版本、时期、状态、地区、项目归属、名称和格式只是上下文信息，任何单项都不能直接决定合并或拆分。模型能看到 Python 编号的 `MSG-xxx`、`FILE-xxx` 证据目录，但新结果只返回 `semantic_reasons`、`reason_detail`、逐条覆盖全部成员的 `member_connections` 和 `risk_flags`，不再返回 `evidence_relation_ids`，也不能直接声明 `shared_message`、`shared_file` 或内部 `group_reason`。Python 只使用端点都在当前组内的关系，按稳定目录顺序计算连接全部成员的最小证据集合，并恢复内部原因；局部证据只写审计，不作为全组合并依据。内部 `evidence_relation_ids` 仅保存 Python 计算结果并兼容旧 trace；同会话候选放入 `candidate_discovery_context`，不用输出分组字段或组编号。逐事件说明遗漏、重复、组外引用或空说明，以及重复事件编号和单成员合并组，都必须进入当前请求局部重试，不得静默修复。
- 初步分组后，`collected_group_discovery` 单次只提交全部组编号和组合标题，组合标题覆盖组内全部来源事件标题；模型逐组完整返回 `group_checks`，Python 校验后形成重叠候选。超过当前预算 profile 仍整体提交，全部失败时按没有标题候选继续并记录 warning。标题候选与共同消息、共同文件、同一附件基础名称、同日会话候选和现有高风险条件共同建立完整检查范围。
- 完整复核可以拆开初步组并跨组重新组合；同一 `event_id` 的相似重复来源是不可拆成员块，但整个成员块可以继续加入更大事件。标题发现的多组候选继续作为完整检查范围，但每条实际组间连接单独编号。模型先逐条判断待处理关系，再统一处理重叠关系并形成最终组；确认成立时最少关联成员必须真实同组，分开时可以返回两侧代表成员，并必须提供关系各侧证据和可独立汇报的目标或结果。复核结果持续非法时保留复核前分组并记录 warning，不让整次部门汇总失败。
- 正式正文对最终单成员组和多成员组逐事件生成，最多同时处理 3 项；必须完整覆盖锁定组并给出关键事实来源。调用失败或局部重试后仍不完整时，只回退当前组已经脱敏的来源内容并写 warning，其他团队事件和当前汇总范围继续生成。
- 日期根目录和每个一级子目录分别作为独立合并范围；更深层目录不递归处理。
- 每个合并范围输出本目录 `YYYY-MM-DD-登录人姓名-merged.md`。
- 团队汇总文件会公开保留来源人员，并在隐藏信息中逐级保留来源事件 ID；中心结果还公开显示从上游 `*-merged.md` 文件名提取并逐级保留的来源负责人。
- 缺少当前登录人的个人 MD 时静默执行普通汇总，不产生 warning。
- 输入/输出数量、字符数、覆盖率、校验错误、重试原因、复核触发和阶段耗时全部由 Python 计算，并进入 CLI JSON 与 trace summary；候选分组和完整复核 step 的事件生成标记为 `collected_summary_without_examples`，正式正文标记为 `collected_full`，总汇总只保存配置状态、版本和数量。阶段实际耗时看 `wall_clock_ms`，并发请求负载看 `request_accumulated_ms`，不能把请求累计耗时当作实际等待时间。调试记录不改变 Codex 局部重试和当前请求 Online 备用的正式线路；结束后独立执行报告模型调用，不要求每一级事件数必须减少。
- 多人汇总的 `--debug-output` 会直接开启 trace，默认写入 `data/debug/collected_merge/<target_date>/`；除原 step 和汇总外，还写入 `collected_group_discovery.json` 与 `collected_group_review.json`。如果 `.env` 配置了 `WORKTRACE_COLLECTED_MERGE_TRACE_ROOT`，继续使用该目录。也可通过 `WORKTRACE_COLLECTED_MERGE_TRACE=true` 长期开启。
- `python3 scripts/replay_collected_review_failures.py --trace-root <trace目录> --steps <编号列表> --output-dir <输出目录>` 可离线复盘候选分组和高风险复核。旧 trace 使用 `legacy_audit`，不补造 `member_connections`；新实验结果使用 `current` 完整执行新协议校验。该脚本不调用模型，也不生成正式 Markdown。
- 每个生成的团队汇总文件默认都会通过飞书 CLI 机器人身份发送给当前登录用户自己。将 `config/self_delivery.json` 的 `enabled` 设为 `false` 后不发送，结果 `self_delivery_status` 为 `disabled`。
- 更多细节见 `docs/collected-people-merge-plan.md`。

## 约束

- 仓库根目录就是 skill 根目录，不使用单独的 `skill/` 子目录。
- 核心实现应放在 `src/`，测试放在 `tests/`，设计文档放在 `docs/`。
- 不要让 LLM 参与数据计算；统计或计算必须由 Python 完成。
- 个人保留提示、既有业务词、临时协作与事实复核条件和语义信号说明维护在 `config/retention_policy.json`；个人和团队事件写作规则、完整事项边界、字段模板及脱敏案例维护在 `config/event_generation.json`；个人和多人分组理由的描述、成立条件和排除条件维护在 `config/event_grouping.json`；多人高风险复核开关和阈值维护在 `config/collected_merge.json`。不得在 Python 中新增聊天关键词或具体中文业务判断规则。
- 个人日报和多人汇总统一从 `config/model_input_budget.json` 按仓库本地 `.env` 的主模型和备用模型组合读取 `model_input_batch_target_tokens`，不另设多人合并字符阈值。配置不存在或没有精确匹配项时回退 `7000`，只在调试统计中记录 `profile_matched=false`，不增加日报 warning。分批和调用前检查必须调用同一个 Python 估算函数，并取 Responses、Chat Completions 和 Codex 三种实际请求结构估算的最大值；模型名、URL、API Key、timeout 和 stream 不计入。会话分段窗口、锚点降级批次、全日候选分组及多人汇总等仍可拆的组合输入必须继续拆分；最小必要输入仍超过目标时允许发送，并在调试记录中保存原有两条线路估算、目标值、超限原因、实际 token 和估算差。该值不是 HTTP 字节数或服务端上下文上限。
- 阈值变更必须使用 `scripts/benchmark_model_input_budget.py` 的脱敏个人与团队数据：主线路验证 `7000、12000、16000、20000、24000` 五档且每档重复两次，备用线路只验证 `7000` 旧基线和 `20000` 重点候选且各一次。备用线路不测试中间档位，但两种模式和 Python 校验仍必须全部通过；人工盲审通过后还要完成显式日期隔离验证，隔离验证成功才能写回当前 profile。评测默认不读飞书、不送达、不上传、不生成诊断报告；已有完整主线路结果时用 `--reuse-primary-existing` 只运行备用线路，显式日期隔离验证只写临时数据和缓存目录，不能覆盖正式 Markdown。
- `WORKTRACE_LLM_STREAM` 是 Online 文字和图片备用请求的唯一流式开关，默认 `false`。`WORKTRACE_LLM_WIRE_API` 同时控制结构化调用、图片备用和独立探针，不按模型名或地址判断。每次 Online 请求重新读取配置，创建并关闭独立 OpenAI 和 HTTP 客户端；固定结构 Online 请求强制且只允许调用一次预期 Function，显式开启流式时按调用编号拼接 Function 参数。Codex 每次都在空临时目录以 stdin 和 `--output-schema` 运行，读取 `--json` 事件；允许非执行型 `error` 提示，仍拒绝工具调用和未知项。Windows 上所有正式调用从 `PATH` 解析真实启动文件：`.cmd` 或 `.bat` 经 `COMSPEC` 启动，`.exe` 或 `.com` 直接启动；输出按 UTF-8 读取，并为 Codex 保留必要系统、用户和临时目录变量，同时继续排除凭据。请求级可重试错误按配置再试 Codex 1 次，仍失败才将当前请求交给 Online 一次。图片工具调用等不合法结果和配置定义的无法识别回复只触发当前图片的 Online 备用一次；备用失败时记一次 warning 并跳过该图，不停止整次生成。Python 校验失败先走 Codex 局部重试并反馈具体错误，结果质量重试用尽后再交给 Online 一次。下一请求重新优先 Codex。除图片摘要的局部失败外，Online 失败或结果仍不合法时停止整次生成。调试、诊断或一次运行失败都不构成整次切换后端的授权；需要改变重试次数时，先停止并取得用户明确同意。
- 原始聊天内容不应长期落盘，长期保留的只有结构化事件清单。
- 安全诊断报告的数量、耗时、排序、比例、token、重试、输入预算和送达统计必须由 Python 计算；大模型只解释编号事实并从 `config/support_report.json` 的允许项中选择判断和建议。普通 warning 和 `success_with_warnings` 不得写成运行失败；token 未上报时显示“服务端未上报”，部分上报时同时显示两类请求数。阶段占比只用墙钟耗时，并发请求累计耗时单独显示；没有细分阶段时不能把“完整运行”列为慢阶段。报告结论与 Python 事实冲突时局部重试，仍冲突则只输出 Python 基础报告；“无需产品改动”不得与具体建议同时出现。
- 报告模型不得读取目标日期、姓名、本机路径、飞书 ID、聊天正文、事件文字、文件名、URL、模型名称、模型地址、密钥、prompt、原始模型返回、原始错误或日志原文。
- 最终对员工可见的 Markdown 应优先保留 `日期`、`事件标题`、`内容`、`具体对象`、中文 `保留理由`、作为来源证据的 `保留依据`、`涉及文件`。
- 员工最终产物不应显示群名、open_id、消息 ID、会话 ID 或参与人名单；事件正文可在责任分工、任务指派、确认沟通对象等确有必要时保留姓名。
- 管理人员汇总产物例外：允许显示来源人员和上游来源负责人；来源事件 ID 只在隐藏信息中保留，用于团队事项追溯。
- 文档链接的主要用途是帮助员工以后回忆事件细节，不是让模型围绕链接做推理。
- 每次成功生成当天 Markdown 文件后，默认通过飞书 CLI 机器人身份将结果发送给当前登录用户自己，作为自送达副本。`config/self_delivery.json` 的 `enabled` 可关闭这一步，不影响文件生成。
- 管理人员汇总模式生成规范化的 `YYYY-MM-DD-登录人姓名-merged.md`，并在自送达开启时把每个结果文件发送给当前登录用户自己。

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
