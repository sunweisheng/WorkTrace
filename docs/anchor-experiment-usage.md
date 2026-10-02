# WorkTrace Anchor Experiment 使用说明

> 历史说明：旧工作流校正已被 [取消工作流概念并改进事件分组](workstream-free-event-grouping-design.md) 替代；下文命令与边界按当前代码说明。

> 状态：独立实验入口。正式个人日报已经使用本人参与的聊天窗口，并在分段失败后直接从这些窗口提炼，但不会读取本实验的持久化缓存，也不会输出本实验的统计格式。正式 `--resume` 使用的是另一套临时分段/提炼中间结果。

## 1. 文档目标

本文档说明如何运行当前隔离的 `anchor_experiment`，以及如何查看它生成的调试产物。

注意：

- 这是独立实验路径，不替代正式个人日报
- 当前优先批量首轮分析，批量失败或结果缺失时才逐锚点多轮识别与扩窗
- 结果输出 JSON 或显式选择的终端表格，不写 Markdown 日报

## 2. 运行命令

从仓库根目录运行。入口读取 `.env` 和进程环境中的 `WORKTRACE_LLM_MODE`，进程环境优先；未配置时为 `codex_with_fallback`，也可使用 `online_only`。自检和底层请求线路与正式配置一致：默认模式执行 Codex 探针，仅 Online 执行 Online Function Calling 探针。实验仍会读取飞书聊天并实际调用模型，详细配置见 [调用说明](online-analyzer-usage.md)。

基础命令：

```bash
python3 -m src.worktrace.anchor_experiment --date 2026-06-23
```

只跑前几个锚点，便于调试：

```bash
python3 -m src.worktrace.anchor_experiment --date 2026-06-23 --limit 3
```

同时输出调试文件：

```bash
python3 -m src.worktrace.anchor_experiment --date 2026-06-23 --limit 3 --dump-dir data/anchor-debug
```

忽略已有缓存，但允许本次重新写入缓存：

```bash
python3 -m src.worktrace.anchor_experiment --date 2026-06-23 --limit 3 --ignore-cache
```

先清空当天锚点缓存，再重新跑一遍：

```bash
python3 -m src.worktrace.anchor_experiment --date 2026-06-23 --limit 3 --refresh-cache
```

只输出顶层统计和 `results_summary`，省略完整 `results` 明细：

```bash
python3 -m src.worktrace.anchor_experiment --date 2026-06-23 --summary-only
```

直接把 `results_summary` 以终端表格输出：

```bash
python3 -m src.worktrace.anchor_experiment --date 2026-06-23 --summary-table
```

`--limit` 只截取已构造的锚点列表，不减少前面的会话发现和消息抓取；`anchor_unit_count` 仍为全部锚点数量。实验使用旧 `group_anchor_units(...)` 的前后各 `30` 条消息窗口，不使用正式主链的群聊时间间隔窗口或私聊整日窗口，也没有载入正式 reaction 目录补全。

首轮按 `anchor_batch_size`（当前为 `3`）组批，不执行正式 runner 的按 token 预算拆窗和装批。底层 analyzer 仍会检查输入预算，超限不代表实验会自动拆分成功。批量返回的已知锚点立即写缓存并结束，`needs_more_context` 或 `needs_attachment_text` 不会单独触发后续轮次；批量协议异常或缺失项才回退逐锚点分析，最多执行 `anchor_retry_limit` 轮（当前为 `3`，来自 `segmentation_retry_limit`）。

默认缓存路径为 `data/cache/anchors/YYYY/MM/YYYY-MM-DD/<anchor_id_sha256>/<fingerprint>.json`，目录名由 `anchor_unit_id` 的 UTF-8 字节计算 SHA-256，兼容 Windows 文件名限制；设置 `cache_root` 时使用对应根目录。旧目录不会自动迁移，首次读取可能未命中，但文件不会被自动删除。`--ignore-cache` 只禁止读取，仍写新结果；`--refresh-cache` 删除当天全部实验缓存后禁止读取，删除范围不受 `--limit` 限制。

实验指纹包含消息、锚点信号、直接关联 ID、附件/链接正文和固定协议版本标记，不包含 `llm_mode`、模型名称或完整规则配置。因此切模式、换模型或改规则不能保证缓存自动失效，对照时应使用 `--ignore-cache` 或 `--refresh-cache`。逐锚点扩窗后的结果按扩展输入指纹保存，下次启动按初始输入查找，也不保证命中。缓存命中仍需要自检和消息抓取，且不为该锚点重新写调试文件。

## 3. 输出 JSON 结构

当前实验返回的顶层字段包括：

- `target_date`
- `status`
- `conversation_count`
- `message_count`
- `anchor_unit_count`
- `analyzed_anchor_count`
- `status_counts`
- `cache_bypass_enabled`
- `cache_refresh_count`
- `cache_hit_count`
- `cache_miss_count`
- `completion_mode_counts`
- `cross_anchor_merge_count`
- `context_request_count`
- `candidate_event_count`
- `results_summary`
- `results`
- `error_summary`

其中新增摘要字段的用途如下：

- `status_counts`：按 `anchor_status` 统计本轮识别结果数量
- `cache_bypass_enabled`：本轮是否启用了“忽略缓存读取”模式
- `cache_refresh_count`：若使用 `--refresh-cache`，本轮开始前清掉了多少个当天缓存文件
- `cache_hit_count`：有多少锚点直接复用了本地缓存
- `cache_miss_count`：有多少锚点结果未从缓存复用；它按锚点计数，不是模型请求次数
- `completion_mode_counts`：按锚点最终完成方式统计数量
- `cross_anchor_merge_count`：有多少锚点被标记为可能需要跨锚点 / 跨会话合并
- `context_request_count`：各锚点最终结果中还保留多少条上下文请求，不累计前面轮次已处理的请求
- `candidate_event_count`：各锚点最终结果中的候选总数，未做跨锚点去重或正式保留过滤
- `results_summary`：每个锚点的轻量检查视图，便于人工快速扫一遍
- `results`：完整明细；若使用 `--summary-only`，该字段会被省略

说明：

- `--summary-only` 仍然输出 JSON
- `--summary-table` 改为输出纯文本表格，不再输出 JSON
- 两个摘要开关只改变终端输出，不减少模型调用，也不减少缓存或显式调试文件
- `status=success` 和退出码 `0` 表示实验正常结束，未完成状态仍可能存在；自检、聊天来源或逐锚点协议失败时返回 `failed` 和退出码 `1`

## 4. 调试目录结构

如果指定 `--dump-dir data/anchor-debug`，每个锚点会写到：

```text
data/anchor-debug/<target_date>/<safe_anchor_unit_id>/
```

当前每个锚点目录包含：

- `pass_01/input.json`
- `pass_01/prompt.txt`
- `pass_01/output.json`
- 仅逐锚点路径发生扩窗时才有 `pass_02/`, `pass_03/` 等
- 每个后续 `pass_*` 目录额外写 `expansion.json`
- 如已补附件正文，对应 `pass_*` 目录写 `attachment_texts.json`
- 如已补飞书文档 / wiki 正文，对应 `pass_*` 目录写 `linked_file_texts.json`

成功批量首轮的 `prompt.txt` 是程序按该锚点重建的单锚点提示词，不是本次实际发送的整批提示词；不能据此还原精确批量请求。逐锚点路径的 `prompt.txt` 才是交给 analyzer 的该轮基础提示词，后端还会添加契约说明。缓存命中不会创建这些文件，复用旧 dump 目录时也不能把旧文件误认为本轮新产物。

这些调试文件可能包含：

- 锚点窗口消息与消息元数据
- 送给 analyzer 的 prompt
- 模型返回结果
- 按需补充的附件正文
- 按需补充的飞书文档 / wiki 正文
- 多轮扩窗时新增的请求与上下文

这些调试文件只在显式启用 `--dump-dir` 时落盘，不属于正式日处理主流程输出。实验缓存则默认持久化，包含结构化候选、上下文请求和来源 ID；它与正式 Markdown 成功写入后清理的临时分段/提炼缓存不同。

历史 `2026-06-23` 实验的目录结构示例如下；当前未重新读取或验证该目录：

```text
data/anchor-debug/2026-06-23/oc_xxx__om_xxx/
```

## 5. 如何检查一次实验是否正常

建议按下面顺序看：

1. 先看顶层 JSON 的 `status` 和退出码，确认实验是否正常结束
2. 再看 `status_counts` 是否出现了预期状态
3. 再看 `completion_mode_counts`，判断这次实验主要是缓存命中、首轮完成，还是逐锚点后续轮次完成
4. 再看 `context_request_count` 和 `candidate_event_count` 是否符合当天聊天特点
5. 最后抽查单个锚点目录里的 `prompt.txt` 与 `output.json`

`results_summary` 当前每项包含：

- `anchor_unit_id`
- `completion_mode`
- `cache_hit`
- `pass_count`
- `anchor_status`
- `candidate_event_count`
- `context_request_count`
- `needs_cross_anchor_merge`

重点关注：

- 模型是否经常直接给出 `completed`
- 是否能稳定识别 `needs_attachment_text`
- 是否把明显无关聊天判成 `not_work_related`
- `needs_cross_anchor_merge` 是否只在真正可能跨窗口时出现
- 对照实验时，确认 `--ignore-cache` 下 `cache_hit_count` 为 `0`
- 对照实验时，确认 `--refresh-cache` 后 `cache_refresh_count` 大于等于 `0`

`completion_mode_counts` 当前可能出现的值：

- `cache_hit`
- `first_pass_completed`
- `multi_pass_completed`
- `not_work_related`
- `first_pass_unresolved`
- `multi_pass_unresolved`

## 6. 当前边界

当前实验路径已经具备：

- 锚点级输入构造
- 首轮协议化识别
- 批量失败或结果缺失后的逐锚点扩窗执行
- 锚点级缓存复用
- 调试文件落盘
- 实验结果摘要统计
- reply / quote 关系摘要入模
- 逐锚点扩窗时飞书文档 / wiki 正文按需补读

当前实验路径不负责：

- 最终跨锚点 merge
- 正式主链的会话分段与片段组批
- 正式候选保留、临时协作复核、个人事实复核、全日初步分组、全部组标题发现、完整内容复核和全部最终组正文重写
- Markdown 写入、文件证据聚合和飞书自发送

因此，现阶段它更适合：

- 观察锚点切分是否合理
- 观察首轮协议是否稳定
- 对比持久化缓存、多轮扩窗和正式主链行为
