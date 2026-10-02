# WorkTrace 锚点能力当前状态

> 历史说明：旧工作流校正已被 [取消工作流概念并改进事件分组](workstream-free-event-grouping-design.md) 替代；下文按当前代码核对正式与实验能力。

> 状态：正式主链与独立实验的边界说明。

## 1. 已进入正式个人日报的能力

当前 `DailyTraceRunner` 已使用：

- 本人发言锚点
- 本人 reaction 锚点
- 群聊确定性锚点窗口和私聊整日窗口
- 锚点窗口的 LLM 会话分段
- 分段失败后直接从本人参与的聊天窗口提炼
- 锚点/片段级按需扩窗
- 附件和飞书文档正文按需补充
- reply/quote 和 reaction response signal
- 候选保留过滤、临时协作复核和个人事实复核
- 全日分组后对全部最终组重新生成正文，包含单成员组

模型线路由 `WORKTRACE_LLM_MODE` 选择：未配置时为 `codex_with_fallback`，也支持 `online_only`。锚点能力不依赖固定 Codex 路线。

因此“锚点尚未进入主流程、主流程仍是一会话一个 ConversationSlice”的旧结论已经失效。

## 2. 仍只属于独立实验的能力

`src/worktrace/anchor_experiment.py` 仍独立提供：

- 持久化锚点级缓存
- `--ignore-cache` / `--refresh-cache`
- `summary-only` / `summary-table`
- `completion_mode_counts`
- 实验专用调试目录和逐轮结果

这些实验能力没有接入正式个人日报产物，正式 runner 不会读取实验缓存。正式 CLI 另有一套临时中间结果：话题切分和事件提炼按 `llm_mode` 加对应 `AnchorUnit.to_dict()` / `SegmentAnalysisBatch.to_dict()` 的输入指纹写入 `data/cache/llm/...`，仅在 `--resume` 时复用，Markdown 成功写入后清理；它不是实验锚点缓存。正式指纹未包含完整提示词、模型名和规则配置，改这些内容不能保证缓存失效；切换模式或旧格式缺少模式字段时不命中。

实验使用 `group_anchor_units(...)` 的前后各 `30` 条消息窗口和空 reaction 目录，未采用正式初始窗口规则。实验先对未命中缓存的锚点组批，正常批量结果直接保存；只有批量失败或某项缺失才回退逐锚点多轮扩窗。实验缓存指纹不含模式和模型，对照运行应显式忽略或刷新缓存。

## 3. 两条入口

正式个人日报：

```bash
python3 -m src.worktrace.cli --date YYYY-MM-DD
python3 -m src.worktrace.cli --date YYYY-MM-DD --resume
```

独立锚点实验：

```bash
python3 -m src.worktrace.anchor_experiment --date YYYY-MM-DD
```

实验输出 JSON/表格和显式启用的调试文件，不写正式个人 Markdown，也不执行正式候选保留与事实复核、全日初步分组、全部组标题发现、完整内容复核、全部最终组正文重写、文件证据聚合和自发送链路。

## 4. 代码落点

正式主链：

- `src/worktrace/runner.py`
- `src/worktrace/pipeline/initial_windows.py`
- `src/worktrace/pipeline/conversation_segments.py`
- `src/worktrace/pipeline/anchor_expansion.py`
- `src/worktrace/pipeline/llm_checkpoints.py`

独立实验：

- `src/worktrace/anchor_experiment.py`
- `src/worktrace/cache/`

## 5. 相关文档

- [分段、扩窗与回退](conversation-slice-retry-design.md)
- [锚点协议](anchor-analysis-protocol.md)
- [锚点实验使用说明](anchor-experiment-usage.md)
- [锚点设计演进记录](anchor-first-multi-pass-design.md)
