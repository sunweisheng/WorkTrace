# WorkTrace 锚点分析协议

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

每个输入锚点必须按 `anchor_unit_id` 对应一项结果。Python 负责检查未知、缺失和重复锚点 ID。

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
- `workstream_key`
- `source_message_ids`
- `fact_items`（`field`、`text`、`evidence_message_ids`）
- `fact_risk_flags`

模型不负责生成可信的 `draft_id`、日期、会话 ID、slice ID 或 confidence；这些运行字段由 Python 根据当前锚点重建。

约束：

- 来源和本人证据 ID 必须来自当前锚点输入
- `self_relations` 的类型来自 `config/event_metadata.json`，每项证据必须属于当前锚点中的本人消息
- link/attachment ID 必须能在当前消息中解析
- 附件文件名只用于识别文件；消息明确表示发送、查看、审核、转交或处理附件时可以引用附件 ID，但不能据文件名推断附件正文
- `retention_reason` 必须是六个允许枚举之一
- `object_hint` 和 `retention_detail` 必须具体
- `workstream_key` 表达业务工作流归属，不是泛化标题
- `fact_items` 必须覆盖标题、正文、主要动作、具体对象、保留依据和非空工作流，并引用真实来源消息
- `fact_risk_flags` 只能使用 `config/retention_policy.json` 配置的风险类型

## 5. `context_requests`

锚点 batch schema 当前直接要求：

- `request_type`
- `target_message_ids`
- `target_attachment_ids`

领域层支持四种类型：

- `earlier_messages`
- `later_messages`
- `attachment_text`
- `linked_file_text`

锚点 expansion 的 prompt/解析层可携带 `target_link_ids`；Python 会校验请求类型与目标 ID 的组合，再决定是否扩展。

## 6. `needs_cross_anchor_merge`

只有候选事实明显可能延伸到其他锚点窗口或会话时返回 `true`。

当前正式个人日报不会用该布尔值直接筛掉全日 merge 输入；它主要保留协议意图和实验统计。正式跨会话阶段仍处理所有通过过滤的候选。

## 7. Python 验证与回退

Python 会验证：

- 顶层和每项结构
- 锚点 ID 覆盖
- status 合法性
- candidate/context 数组
- 所有来源引用
- 本人直接关联证据
- 扩窗后是否获得新信息

协议失败由调用方按 `anchor_batch_retry_limit` 重试；最终失败的锚点会跳过并写 warning，不允许模型输出绕过引用校验。

## 8. 代码落点

- `src/worktrace/analyzers/output_schemas.py`
- `src/worktrace/analyzers/prompts.py`
- `src/worktrace/analyzers/protocol.py`
- `src/worktrace/pipeline/validation.py`
- `src/worktrace/runner.py`
- `src/worktrace/anchor_experiment.py`
