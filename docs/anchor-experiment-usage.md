# WorkTrace Anchor Experiment 使用说明

> 状态：独立实验入口。正式个人日报已经使用本人参与的聊天窗口，并在分段失败后直接从这些窗口提炼，但不会读取本实验的持久化缓存，也不会输出本实验的统计格式。

## 1. 文档目标

本文档说明如何运行当前隔离的 `anchor_experiment`，以及如何查看它生成的调试产物。

注意：

- 这是独立实验路径，不替代正式个人日报
- 当前覆盖锚点多轮识别、扩窗、缓存和实验统计
- 结果只输出 JSON，不写 Markdown 日报

## 2. 运行命令

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
- `cache_miss_count`：有多少锚点本轮仍然实际调用了 LLM
- `completion_mode_counts`：按锚点最终完成方式统计数量
- `cross_anchor_merge_count`：有多少锚点被标记为可能需要跨锚点 / 跨会话合并
- `context_request_count`：LLM 总共提出了多少条补充上下文请求
- `candidate_event_count`：当前实验总共识别出了多少候选事项
- `results_summary`：每个锚点的轻量检查视图，便于人工快速扫一遍
- `results`：完整明细；若使用 `--summary-only`，该字段会被省略

说明：

- `--summary-only` 仍然输出 JSON
- `--summary-table` 改为输出纯文本表格，不再输出 JSON

## 4. 调试目录结构

如果指定 `--dump-dir data/anchor-debug`，每个锚点会写到：

```text
data/anchor-debug/<target_date>/<safe_anchor_unit_id>/
```

当前每个锚点目录包含：

- `pass_01/input.json`
- `pass_01/prompt.txt`
- `pass_01/output.json`
- 如发生扩窗，还会有 `pass_02/`, `pass_03/` 等
- second pass 会额外写 `expansion.json`
- 如已补附件正文，会写 `attachment_texts.json`
- 如已补飞书文档 / wiki 正文，会写 `linked_file_texts.json`

这些调试文件可能包含：

- 锚点窗口消息与消息元数据
- 送给 analyzer 的 prompt
- 模型返回结果
- 按需补充的附件正文
- 按需补充的飞书文档 / wiki 正文
- 多轮扩窗时新增的请求与上下文

它们只在显式启用 `--dump-dir` 时落盘，不属于正式日处理主流程输出。

以 `2026-06-23` 的一次真实实验为例，目录结构类似：

```text
data/anchor-debug/2026-06-23/oc_xxx__om_xxx/
```

## 5. 如何检查一次实验是否正常

建议按下面顺序看：

1. 先看顶层 JSON 的 `status` 是否为 `success`
2. 再看 `status_counts` 是否出现了预期状态
3. 再看 `completion_mode_counts`，判断这次实验主要是缓存命中、首轮完成，还是 second pass 收敛
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
- second pass 扩窗执行
- 锚点级缓存复用
- 调试文件落盘
- 实验结果摘要统计
- reply / quote 关系摘要入模
- 飞书文档 / wiki 正文按需补读

当前实验路径不负责：

- 最终跨锚点 merge
- 正式主链的会话分段与片段组批
- 工作流归属校正
- Markdown 写入、文件证据聚合和飞书自发送

因此，现阶段它更适合：

- 观察锚点切分是否合理
- 观察首轮协议是否稳定
- 对比持久化缓存、多轮扩窗和正式主链行为
