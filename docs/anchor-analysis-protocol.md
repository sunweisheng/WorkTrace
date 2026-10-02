# WorkTrace 锚点分析协议

> 历史说明：旧版协议中的工作流字段已被 [取消工作流概念并改进事件分组](workstream-free-event-grouping-design.md) 替代；下文只描述当前仍在使用的锚点输入与回退协议。

> 状态：正式主链在分段失败后直接提炼时，与独立 `anchor_experiment` 共用的聊天窗口分析协议。正式主链优先使用“聊天窗口分段 + 片段批处理”。

## 1. 输入

锚点批量分析按 `AnchorUnit` 发送：

- `target_date`
- `anchor_unit_id`
- `conversation_name`
- 压缩消息及真实消息 ID
- reply/quote 摘要
- 附件和链接元数据
- 已补充的附件/链接正文
- 当前已知 reaction/anchor signal

模型看到的消息 ID、附件 ID 和链接 ID 都来自 Python 输入，后续引用必须限制在该输入范围。

## 2. 批量输出

正式回退使用 `anchor_batch_output_schema()`：

```json
{
  "results": [
    {
      "anchor_unit_id": "anchor-id",
      "analysis": {
        "anchor_status": "completed",
        "candidate_events": [],
        "context_requests": [],
        "needs_cross_anchor_merge": false
      }
    }
  ]
}
```

每个输入锚点必须按 `anchor_unit_id` 对应一项结果。正式 runner 会检查未知、缺失和重复锚点 ID；独立实验只按已知 ID 建立结果映射，未知项忽略、重复项以后项覆盖，缺失项转入逐锚点调用。两条入口的验证强度不同。

## 3. `anchor_status`

模型可返回：

- `completed`
- `needs_more_context`
- `needs_attachment_text`
- `not_work_related`
- `uncertain`

Python 领域模型还包含 `pending`、`failed`、`skipped`，用于运行状态和失败处理，不是模型正常完成值。

## 4. `candidate_events`

模型输出字段与当前 schema 一致：

- `topic`
- `content`
- `action_label`
- `object_hint`
- `retention_reason`
- `retention_detail`
- `referenced_link_ids`
- `referenced_attachment_ids`
- `self_evidence_message_ids`
- `self_relations`（参与方式及其本人证据消息 ID）
- `source_message_ids`
- `fact_items`（`field`、`text`、`evidence_message_ids`）
- `fact_risk_flags`

输出 Schema 不包含 `draft_id`、日期、会话 ID、slice ID 或 confidence。正式回退会补齐当前锚点的会话和片段来源，并按合法来源消息生成缺失的日期、编号；confidence 使用领域模型默认值。独立实验只保存解析结果，不执行正式候选物化，不能要求其 JSON 已补齐这些运行字段。

约束：

- 来源和本人证据 ID 必须来自当前锚点输入
- `self_relations` 的类型来自 `config/event_metadata.json`，每项证据必须属于当前锚点中的本人消息
- link/attachment ID 必须能在当前消息中解析
- 附件文件名只用于识别文件；候选已按个人保留规则确认可提炼后，若实质业务事实明确涉及发送、查看、审核、转交或处理附件，才可以引用附件 ID；仅发送、查看或确认附件本身不能使候选成为事件，也不能据文件名推断附件正文
- `retention_reason` 必须是六个允许枚举之一
- `object_hint` 和 `retention_detail` 必须具体
- `fact_items` 必须覆盖标题、正文、主要动作、具体对象和保留依据，并引用真实来源消息
- `fact_risk_flags` 只能使用 `config/retention_policy.json` 配置的风险类型

## 5. `context_requests`

锚点 batch schema 当前直接要求：

- `request_type`
- `target_message_ids`
- `target_attachment_ids`
- `target_link_ids`

领域层支持四种类型：

- `earlier_messages`
- `later_messages`
- `attachment_text`
- `linked_file_text`

正式 runner 会校验请求类型与三类目标 ID 的组合，再决定是否扩展：每种请求都必须引用当前窗口中的消息；正文请求还必须引用这些消息实际附带的附件或链接，前后消息请求不能夹带附件或链接 ID。独立实验逐锚点路径直接把解析后的请求交给扩窗函数，不执行正式 runner 的这一层过滤。

## 6. `needs_cross_anchor_merge`

只有候选事实明显可能延伸到其他锚点窗口或会话时返回 `true`。

当前正式个人日报不会用该布尔值直接筛掉全日 merge 输入；它主要保留协议意图和实验统计。正式跨会话阶段仍处理所有通过过滤的候选。

## 7. Python 验证与回退

请求 Schema 约束模型状态、候选字段和上下文请求的结构；解析器将返回值转换为领域对象。正式 runner 进一步检查锚点 ID 覆盖，再过滤非法来源、本人证据、附件/链接引用、参与方式和事实风险类型。候选必须包含当天来源消息，不能仅凭扩窗得到的上下文产生当天事件。这里包含过滤和规范化，不等同于所有非法引用都触发重试。

正式锚点回退按 `anchor_batch_retry_limit`（当前为 `1`）额外尝试同一批；仍有部分缺失时只处理缺失项，整批失败时继续拆小，最终单锚点仍失败才跳过并写 warning。当前同批重试不把具体验证错误追加到提示词。仅 Online 的终止请求错误会直接传播，不进入这一层循环；默认双线路的请求错误仍可能进入外层重试。

正式回退的合法上下文请求最多扩展 `anchor_retry_limit` 轮（由 `config/llm_retry.json` 的 `segmentation_retry_limit` 载入，当前为 `3`）；达到上限或没有新增上下文时跳过该锚点并写 warning。

独立实验采用另一套控制：缓存未命中后先批量调用一次，批量失败或缺少结果才逐锚点处理；正常批量返回即使仍请求上下文，也直接保存并返回。逐锚点路径总轮数最多为 `anchor_retry_limit`，在 `completed`、`not_work_related` 或无上下文请求时结束，不执行正式引用过滤及“没有新增信息”判断。逐锚点协议异常由实验入口返回失败 JSON，不按正式 runner 的规则逐项跳过。

两条入口的底层调用都遵循 `WORKTRACE_LLM_MODE`；模式和请求级重试见 [调用说明](online-analyzer-usage.md)。

## 8. 代码落点

- `src/worktrace/analyzers/output_schemas.py`
- `src/worktrace/analyzers/prompts.py`
- `src/worktrace/analyzers/protocol.py`
- `src/worktrace/pipeline/validation.py`
- `src/worktrace/runner.py`
- `src/worktrace/anchor_experiment.py`
