# WorkTrace 无工作流事件分组设计

> 状态：现行设计。本文档取代个人日报中的工作流归属、工作流根事件和父子事件设计，并作为个人日报与多人汇总共同分组语义的依据。

## 1. 目标

个人日报不再先给事项命名为某个工作流，再依赖名称建立父子关系。新的处理顺序是：

```mermaid
flowchart LR
    A["候选事件"] --> B["全日事件分组"]
    B --> C["Python 完整性校验"]
    C --> D["强关联漏合并复核"]
    D --> E["生成最终事件"]
```

本设计同时解决两个问题：

- 删除工作流归属和未归属复核两类额外模型调用。
- 在模型返回偏保守的单例或小组时，只对存在明确强关联的局部范围再次判断。

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

全日分组继续使用已经通过临时协作复核和事实复核的 `SourceBackedEventDraft`。Python 内部候选仍保留完整来源字段，用于后续证据检查和强关联复核；发送给初始分组模型的每个候选只提供：

- `draft_id`
- 标题、正文、主要动作、具体对象和保留依据
- 来源消息 ID
- 文件、附件和链接引用

提示词不发送 `source_conversation_id` 和 `source_slice_id`，也不重新读取整段原始聊天。`config/event_grouping.json` 中完整的成立条件、排除条件和负面示例随请求发送。

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

## 5. 强关联漏合并复核

### 5.1 强关联来源

Python 只根据结构化关系建立关联边：

- 候选来自同一 `source_slice_id`。
- 一个候选的来源消息直接 reply/quote 另一个候选的来源消息。
- 候选共享来源消息。
- 候选共享同一文件、附件或链接的稳定文件标识。

仅来自同一天同一会话不构成强关联，也不单独触发复核。

### 5.2 复核范围

Python 先把全日合法分组视为不可拆分的节点，再根据强关联边建立连通范围。只有强关联跨越两个或以上现有分组时才发起局部复核。

每个复核请求包含一个连通范围内的完整现有组。复核可以：

- 保持原分组。
- 把两个或多个完整现有组合并。

复核不允许拆散已有合法组。返回结果继续使用全日分组的四个字段，Python 额外校验每个原组必须完整出现在一个新组内。

不同连通范围最多三路并行处理；同一范围内的重试保持顺序。并发数来自 `config/llm_retry.json`。

### 5.3 复核失败

局部复核结果非法时，按配置对当前范围反馈错误并重试。仍非法或技术调用失败时：

- 保留复核前的合法分组。
- 记录 warning、失败范围和尝试次数。
- 不影响其他复核范围和后续最终事件生成。

## 6. 共同语义配置

新增 `config/event_grouping.json`，包含：

- 个人全日分组任务说明。
- 个人强关联局部复核说明。
- 多人汇总继续使用的语义理由定义、成立条件和排除条件。
- 同一会话不同事项、宽泛目标和相似工作不得合并的负面示例。
- 同一目标下方案、汇报、任务分配和执行反馈可以合并的说明。
- 仅标题相似、同地区、同部门、同会话或同类工作不足以合并的说明。

`config/collected_merge.json` 只保留多人汇总的数量阈值、开关和高风险复核设置，不再保存共同语义定义，也不再包含 `review_workstream_conflicts`。

`config/retention_policy.json` 删除 `require_empty_workstream`。临时协作复核继续使用其余筛选条件。

## 7. 多人汇总调整

多人汇总继续保留当前协议：

- `semantic_reasons`
- `reason_detail`
- `member_connections`
- `risk_flags`
- Python 计算的共同消息和共同文件证据
- 高风险复核和正式正文覆盖校验

多人候选、提示词、结构化模型和高风险复核不再读取或比较工作流字段。原“不同非空工作流”复核触发条件删除，其他触发条件不变。

多人语义理由改从 `config/event_grouping.json` 读取，使个人与多人判断使用同一套业务说明，但两种 Function 输出保持各自协议，不强行统一字段。

## 8. 最终事件与 Markdown

`materialize_grouped_merged_drafts(...)` 直接使用校验后的最终分组：

- 采用 `primary_draft_id` 选择主候选。
- 合并组内不冲突的标题、正文、动作、对象、保留依据、来源消息和文件。
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
- `day_group_review.json`：强关联范围、每次局部复核结果、校验错误和保留决定。
- `resolved_groups.json`：最终合法组、稳定组编号、warning 和 Python 统计。

`llm_usage.json` 保留 `day_candidate_merge`，新增 `day_group_review`，不再出现 `workstream_assignment` 和 `unassigned_workstream_assignment`。

`runner.stage.completed` 使用：

- `day_group_review`：单个局部复核请求耗时，可并发，累计值不是实际阶段耗时。
- `day_group_review_all`：全部局部复核的墙钟耗时。
- `merge_day_candidates`：全日初始分组、校验、局部复核和修补的总墙钟耗时。

## 11. Python 统计

CLI JSON、`final_events.json` 和回放 `summary.json` 写入 `day_grouping_summary`：

```json
{
  "candidate_count": 20,
  "initial_group_count": 18,
  "final_group_count": 16,
  "review_component_count": 2,
  "review_request_count": 2,
  "validation_retry_count": 0,
  "codex_fallback_count": 0,
  "singleton_repair_candidate_count": 0,
  "warning_count": 0
}
```

所有数量和耗时由 Python 根据真实结果与日志计算，大模型不参与计算。

## 12. 调试脚本

- `replay_day_with_trace.py` 汇总新分组文件和 `day_grouping_summary`；旧 trace 缺少新文件时返回“不可用”，不补造数据。
- `report_replay_timings.py` 分开报告初始分组调用耗时、局部复核累计耗时、`day_group_review_all` 墙钟耗时和 `merge_day_candidates` 总墙钟耗时，并支持基线与当前 trace 对比。
- `report_replay_call_inputs.py` 展示全日分组和每次局部复核；旧 trace 中的工作流调用标记为“旧版工作流归属”。
- `report_event_grouping_comparison.py` 只做候选覆盖、分组集合、强关联复核、理由和证据的确定性比较，不判断业务语义是否正确，也不调用模型。
- `replay_collected_review_failures.py` 改从共同分组配置读取语义理由，并忽略旧 trace 的工作流字段。

## 13. 验收

自动测试必须覆盖：

- 全日分组覆盖、重复、未知 ID、主事件和证据校验。
- 全部单例合法，多事件组必须有理由和证据。
- 四类强关联、同一会话不触发、完整原组不可拆。
- Online 结果质量重试、Codex 备用、非法结果拆单修补和技术失败终止。
- 局部复核并发、失败保留原分组和统计。
- 多人汇总删除工作流依赖后仍保持现有语义、高风险和证据协议。
- 旧 Markdown/trace 可读，新文件无工作流字段。
- 调试脚本的新旧 trace、墙钟统计和对比输出。

真实验收保存 2026-07-22 的旧 trace 和 Markdown，再以相同日期、配置和输入范围执行调试回放。报告实际请求数、重试、备用线路、分组变化和各阶段墙钟耗时，不把历史单次耗时作为固定断言。
