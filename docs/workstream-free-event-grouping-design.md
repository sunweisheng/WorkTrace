# WorkTrace 无工作流事件分组设计

> 状态：现行设计。本文档取代个人日报中的工作流归属、工作流根事件和父子事件设计，并作为个人日报与多人汇总共同分组语义的依据。

## 1. 目标

个人日报不再先给事项命名为某个工作流，再依赖名称建立父子关系。新的处理顺序是：

```mermaid
flowchart LR
    A["候选事件"] --> B["全日事件分组"]
    B --> C["Python 完整性校验"]
    C --> D["全部组编号与标题候选发现"]
    D --> E["结构关系、同一基础文件名和标题候选完整复核"]
    E --> F["多成员内容重写"]
    F --> G["生成最终事件"]
```

本设计同时解决两个问题：

- 删除工作流归属和未归属复核两类额外模型调用。
- 在模型返回偏保守的单例或小组时，通过标题、结构关系和同一文件不同版本发现需要再次判断的局部范围。

Python 只检查结构、覆盖范围和证据边界，不读取聊天文字判断业务含义。具体语义说明统一来自 `config/event_grouping.json`。

## 2. 删除的概念

新生成的数据不再包含：

- `workstream_key`
- `workstream_name`
- `root_workstream_name`
- `parent_draft_id`
- 工作流归属 Function
- 未归属事件复核 Function

候选之间只有“是否属于同一事项”的分组关系，不再形成根事件和子事件树。

旧 Markdown、旧缓存和旧 trace 中出现这些字段时，兼容读取器允许解析，但立即丢弃，不参与新判断，也不写回新文件。

## 3. 个人全日分组协议

### 3.1 输入

全日分组继续使用已经通过临时协作复核和事实复核的 `SourceBackedEventDraft`。Python 内部候选仍保留完整来源字段，用于后续证据检查和完整内容复核；发送给初始分组模型的每个候选只提供：

- `draft_id`
- 标题、正文、主要动作、具体对象和保留依据
- 来源消息 ID
- 文件、附件和链接引用

提示词不发送 `source_conversation_id` 和 `source_slice_id`，也不重新读取整段原始聊天。`config/event_grouping.json` 中完整的成立条件、排除条件、正面示例和负面示例随请求发送。

### 3.2 Function 输出

模型分别返回多事件组和单例编号。多事件组结构为：

```json
{
  "draft_ids": ["draft-001", "draft-002"],
  "primary_draft_id": "draft-001",
  "common_object": "同一份具体交付物",
  "semantic_reasons": ["continuous_action"],
  "reason_detail": "第一条的确认结果是第二条执行反馈的输入。",
  "member_connections": [
    {
      "draft_id": "draft-001",
      "connection_detail": "确认该交付物的方案。",
      "evidence_message_ids": ["om_xxx"]
    },
    {
      "draft_id": "draft-002",
      "connection_detail": "依据已确认方案反馈执行结果。",
      "evidence_message_ids": ["om_yyy"]
    }
  ]
}
```

顶层 `singleton_draft_ids` 保存所有不合并的候选。模型不返回 `group_id`。Python 按候选原始顺序生成 `group-001`、`group-002` 等稳定内部编号。

### 3.3 Python 校验

Python 对一次完整分组执行以下检查：

- 每个输入候选恰好出现一次。
- 不允许未知、遗漏或重复 `draft_id`。
- `merged_groups` 每组至少包含两个候选，单例只能进入 `singleton_draft_ids`。
- `primary_draft_id` 必须属于当前组。
- `common_object` 和 `reason_detail` 必须非空，`semantic_reasons` 只能来自配置。
- `member_connections` 必须逐条覆盖组内所有候选，每个编号恰好一次。
- 每条成员说明必须非空，且 `evidence_message_ids` 只能引用该成员自己的来源消息。
- 多余字段、重复语义理由和跨数组重复编号都作为协议错误。

全部候选返回单例是合法结构，不因事件数没有减少而报错。

## 4. 全日分组失败处理

结果质量错误只处理当前全日分组请求：

1. Online 首次返回非法结果时，把具体缺失、重复、主事件或证据错误反馈给 Online，重试一次。
2. Online 再次非法时，把同一输入和最新错误交给 Codex 一次。
3. Codex 返回合法结果时继续。
4. Codex 成功返回但结果仍非法时，保留其中互不冲突且完全合法的组；所有受错误影响的候选拆成单例，并记录 warning。
5. Codex 技术调用失败时终止整次生成，不写 Markdown。

Online 和 Codex 的技术线路重试仍遵守通用 failover 约定；下一项模型请求重新优先 Online。

拆单修补不得让候选遗漏或重复，也不得把部分非法多事件组直接保留。

## 5. 漏合并候选发现与复核

### 5.1 标题发现

完整输入超过 7000 时，系统按候选分批完成初步分组，再由 Python 直接合并所有批次结果、校验全量覆盖并生成稳定编号。这里不构造候选摘要，也不做摘要再次分组；跨批可能漏掉的关系由下面面向全部组的标题发现和完整内容复核处理。

初步分组通过 Python 校验后，系统把全部稳定组编号和标题放入一个 `day_group_discovery` 请求。每个输入组严格只有：

- `group_id`
- `title`

标题由 Python 按稳定顺序组合主候选和组内全部成员标题，去除空值与完全重复值；如果组合后仍为空，再按现有文本选择规则取得可用标题。请求不包含日期、正文、对象、消息 ID、会话、附件、人员信息或初步分组正反例，字段仍严格只有 `group_id` 和 `title`。模型返回 `group_checks`：每个输入组必须按输入顺序恰好作为 `group_id` 返回一次，`related_group_ids` 可以为空，也可以包含一个或多个其他合法编号，`reason` 必须非空。Python 拒绝遗漏、重复检查、未知编号、自关联、关联编号重复、空理由和额外字段，再把所有单向关系当作无方向关系，按重叠关系形成可包含两个、三个或更多组的 `candidate_groups`。候选范围不限制组数，但进入完整复核后，每条实际指出的组间连接分别获得关系编号，避免同一范围内部分关系成立、部分关系不成立时只能给出一个含糊结论。所有 `related_group_ids` 都为空表示没有标题候选。

逐组完整检查是通用协议约束，不根据某一天、某个人或某类业务设置关键词或固定长度片段，也不在 Python 中推断标题语义。标题发现自己的提示只说明如何完整比较，不借用其他分组阶段的案例内容。它不能保证模型判断永远正确，但能把“只返回少量候选而没有证明其他组已检查”的合法缺口变成可校验的完整覆盖要求。

标题发现使用统一 Online/Codex 输入估算。完整编号和标题清单超过 7000 token 时仍作为不可拆输入整体提交并标记超限。技术失败或结果非法时只处理当前请求：Online 按现有机制重试，仍失败或非法时交给 Codex 一次；全部失败后按没有标题候选继续，记录 warning，不阻止个人 Markdown 写入和本人送达。

### 5.2 结构关系和同一基础文件名

Python 只根据结构化关系建立关联边：

- 候选来自同一 `source_slice_id`。
- 一个候选的来源消息直接 reply/quote 另一个候选的来源消息。
- 候选共享来源消息。
- 候选共享同一文件、附件或链接的稳定文件标识。
- 候选实际引用的非图片附件去除配置版本后缀后，基础名称和扩展名相同。

附件名称先执行 NFKC、空白和大小写统一，再按照 `config/event_grouping.json` 的版本后缀表达式反复移除末尾版本标记。月份、年份、编号等其他数字保留；不同扩展名不建立关系。该关系只触发复核，不直接合并。

仅来自同一天同一会话不构成待处理关系，也不单独触发复核。

### 5.3 复核范围与重新分组

Python 把结构关系、同一基础文件名关系和标题发现候选统一编号为待处理关系，再按关系重叠建立完整检查范围。检查范围以原始候选事件为成员，初步分组只作为复核前结果，不再作为不可拆边界。

每个复核请求可以保持原分组，也可以拆开初步组并与其他成员重新组合。模型返回：

- `merged_groups`
- `singleton_draft_ids`
- `relation_resolutions`

Python 要求全部原始候选完整且唯一覆盖。模型先逐条判断每项 `relation_resolutions`，再统一处理重叠关系并形成 `merged_groups` 和 `singleton_draft_ids`；不得先沿用初步组或预设最终组，再用组归属反向解释关系。每条关系必须恰好对应一个稳定 `relation_id`；确认成立时，`connected_draft_ids` 只填写证明关系成立所需的最少成员，直接两端关系必须包含左右两端，且所有关联成员必须真实进入同一最终组；决定分开时，`connected_draft_ids` 可以为空，也可以填写关系两侧的代表成员，填写后 Python 验证这些成员确实位于不同最终组。无论是否填写代表成员，都必须写明具体业务差异，并用最少必要的 `evidence_message_ids` 覆盖关系各侧。Function 示例按关系两侧生成代表成员和代表证据，不重复复制整个检查范围的消息清单。仅动作、人员、阶段或标题范围不同不足以证明应当拆开。

不同连通范围最多三路并行处理；同一范围内的重试保持顺序。并发数来自 `config/llm_retry.json`。

### 5.4 成员锁定后的内容重写

最终多成员组确定后，`personal_group_render` 只生成带证据的标题、正文和具体对象。生成结果必须覆盖组内每个候选；完整过程组使用能够概括全部成员的标题，只有紧密子集成立时才使用更具体的标题。保留理由、涉及文件、本人参与方式、消息覆盖和统计仍由 Python 处理。单成员组沿用原内容，不增加调用。

### 5.5 失败处理

局部复核结果非法时，按配置对当前范围反馈具体错误并重试；Online 结果重试后仍非法，再把带有最后校验错误的当前范围交给 Codex 一次。技术请求已经走完 Online 重试和 Codex 备用仍失败时，不再重新开始一轮 Online，也不占用结果质量重试。仍非法或技术调用失败时：

- 保留复核前的合法分组。
- 记录 warning、失败范围和尝试次数。
- 不影响其他复核范围和后续最终事件生成。

个人内容重写全部尝试失败时，Python 使用确定性拼接结果并记录 warning，继续 Markdown 写入和本人送达。

## 6. 共同语义配置

新增 `config/event_grouping.json`，包含：

- 个人全日分组任务说明。
- 个人标题发现规则、正面/负面示例和局部复核说明。
- 附件忽略类型和版本后缀表达式。
- 多人汇总继续使用的语义理由定义、成立条件和排除条件。
- 同一会话不同事项、宽泛目标和相似工作不得合并的负面示例。
- 同一目标下方案、汇报、任务分配和执行反馈可以合并的说明。
- 仅标题相似、同地区、同部门、同会话或同类工作不足以合并的说明。

`config/collected_merge.json` 只保留多人汇总的数量阈值、开关和高风险复核设置，不再保存共同语义定义，也不再包含 `review_workstream_conflicts`。

`config/retention_policy.json` 删除 `require_empty_workstream`。临时协作复核继续使用其余筛选条件。

## 7. 多人汇总调整

多人汇总继续保留当前分组协议，并增加与个人相同的标题发现和范围内重新分组：

- `semantic_reasons`
- `reason_detail`
- `member_connections`
- `risk_flags`
- Python 计算的共同消息和共同文件证据
- 高风险复核和正式正文覆盖校验
- `collected_group_discovery` 使用覆盖初步组全部来源事件标题的组合标题做全量编号与标题发现
- `relation_resolutions` 逐条处理待处理关系
- 同一 `event_id` 相似重复来源形成不可拆成员块，但可整体加入更大组

标题候选、共同消息、共同文件、同一附件基础名称、同日会话候选和原高风险条件共同建立检查范围。完整复核可以拆开初步组并跨组重新组合，并与个人复核一样先逐条判断关系，再统一形成最终组。多人候选、提示词、结构化模型和完整复核不读取或比较工作流字段。

多人语义理由改从 `config/event_grouping.json` 读取，使个人与多人判断使用同一套业务说明，但两种 Function 输出保持各自协议，不强行统一字段。

## 8. 最终事件与 Markdown

`materialize_grouped_merged_drafts(...)` 使用校验后的最终分组：

- 多成员组先通过 `personal_group_render` 生成覆盖全部锁定成员的标题、正文和具体对象。
- Python 合并组内动作、参与方式、保留依据、来源消息和文件。
- 继续执行敏感过滤、结构化保留、文件证据聚合和最终过滤。
- 不生成工作流名称或父子事件关系。

新个人和多人 Markdown 不显示“工作流”，隐藏元数据也不写工作流字段。旧 Markdown 读取器允许识别旧字段，但转换为 `WorkEvent` 后丢弃。

## 9. 缓存与兼容

LLM 缓存 schema 升级到 v3，避免新流程复用包含旧工作流字段的候选和分组结果。

兼容边界：

- 旧 Markdown：可读，工作流字段丢弃，新写入不保留。
- 旧 trace：调试脚本继续识别旧工作流文件，并标记为旧版调用。
- 旧缓存：不复用，不迁移。
- 新 trace：不生成任何 `workstream_resolution_*` 文件或请求类型。

## 10. 调试产物

个人调试目录继续使用 `_merge_day_candidates/`，新文件为：

- `input.json`：全日候选输入。
- `prompt.txt`：首次全日分组提示词。
- `grouping_attempts.json`：Online/Codex 每次返回、Python 校验错误、线路和修补结果。
- `day_group_discovery.json`：协议版本、全部编号和标题、两种 token 估算、超限状态、每次尝试、逐组检查、Python 形成的候选组或放弃原因。
- `day_group_review.json`：每个复核范围的来源、必须覆盖的候选编号、每次局部复核的解析前 Function 返回、校验错误和保留决定。
- `personal_group_render.json`：锁定成员、内容重写尝试、失败回退和最终文字。
- `day_group_review_replay.json`：失败范围按既有调试输入单独重放时的线路、耗时、初始校验错误、返回结果或放弃原因；不修改正式个人 MD。
- `resolved_groups.json`：最终合法组、稳定组编号、warning 和 Python 统计。

`llm_usage.json` 保留 `day_candidate_merge` 和 `day_group_review`，新增 `day_group_discovery` 与 `personal_group_render`，不再出现 `workstream_assignment` 和 `unassigned_workstream_assignment`。

`runner.stage.completed` 使用：

- `day_group_review`：单个局部复核请求耗时，可并发，累计值不是实际阶段耗时。
- `day_group_discovery_all`：标题发现从首次请求到成功或放弃的墙钟耗时。
- `day_group_review_all`：全部局部复核的墙钟耗时。
- `personal_group_render_all`：全部多成员内容重写的墙钟耗时。
- `merge_day_candidates`：全日初始分组、校验、标题发现、完整内容复核、多成员内容重写和修补的总墙钟耗时。

## 11. Python 统计

CLI JSON、`final_events.json` 和回放 `summary.json` 写入 `day_grouping_summary`：

```json
{
  "candidate_count": 20,
  "initial_group_count": 18,
  "final_group_count": 16,
  "review_component_count": 2,
  "review_request_count": 2,
  "cross_group_merge_count": 1,
  "split_group_count": 1,
  "relation_merged_count": 1,
  "relation_separate_count": 1,
  "review_failure_count": 0,
  "discovery_request_count": 1,
  "discovery_retry_count": 0,
  "discovery_checked_group_count": 25,
  "discovery_candidate_group_count": 2,
  "discovery_involved_group_count": 4,
  "discovery_failure_count": 0,
  "discovery_oversized_submission_count": 0,
  "content_render_request_count": 2,
  "content_render_retry_count": 0,
  "content_render_failure_count": 0,
  "validation_retry_count": 0,
  "codex_fallback_count": 0,
  "singleton_repair_candidate_count": 0,
  "warning_count": 0
}
```

所有数量和耗时由 Python 根据真实结果与日志计算，大模型不参与计算。

## 12. 调试脚本

- `python3 -m scripts.replay_day_with_trace --date YYYY-MM-DD` 汇总新分组文件和 `day_grouping_summary`，并区分完整内容复核的逻辑结果尝试数与实际模型请求数；旧 trace 缺少新文件时返回“不可用”，不补造数据。
- `replay_failed_day_group_reviews.py` 从完整回放 trace 中选择最后状态失败的复核范围，带入最后校验错误重新请求，只写独立重放产物。
- `report_replay_timings.py` 分开报告初始分组、标题发现、完整内容复核、多成员内容重写和 `merge_day_candidates` 墙钟耗时，并支持基线与当前 trace 对比。
- `report_replay_call_inputs.py` 展示全日分组、标题发现、完整内容复核、多成员内容重写和失败范围重放的输入；旧 trace 中的工作流调用标记为“旧版工作流归属”。
- `report_event_grouping_comparison.py` 比较候选覆盖、标题发现候选及其完整复核结果、最终分组、理由和证据，不判断业务语义是否正确，也不调用模型。
- `replay_collected_review_failures.py` 从共同分组配置读取语义理由；新 trace 还原初步组、待处理关系和不可拆成员块并完整校验，旧 trace 不补造这些字段。

## 13. 验收

自动测试必须覆盖：

- 全日分组覆盖、重复、未知 ID、主事件和证据校验。
- 全部单例合法，多事件组必须有理由和证据。
- 四类原有结构关系、同一文件不同版本、标题候选重叠组合、同一会话不触发、初步组可拆后跨组重新组合。
- 三个或更多成员的检查范围既允许全部合并，也允许按完整证据重新组合；任何已发现关系都必须明确处理，证据充分时允许相关成员保持分开。
- 多成员内容重写覆盖全部成员，失败时确定性回退。
- 标题发现严格只发送编号和标题，超过 7000 仍整体提交，全部失败后个人日报继续。
- Online 结果质量重试、Codex 备用、非法结果拆单修补和技术失败终止。
- 局部复核并发、失败保留原分组和统计。
- 部门标题发现、跨组重新组合、同一 `event_id` 不可拆成员块和正式内容严格覆盖。
- 旧 Markdown/trace 可读，新文件无工作流字段。
- 调试脚本的新旧 trace、墙钟统计和对比输出。

真实验收应选择包含已知漏合并现象的旧 trace 和 Markdown，再以相同日期、配置和输入范围执行调试回放。报告实际请求数、重试、备用线路、分组变化和各阶段墙钟耗时，不把任一日期的单次结果或耗时作为固定断言。
