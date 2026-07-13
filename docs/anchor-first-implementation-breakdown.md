# WorkTrace 锚点能力当前状态

> 状态：正式主链与独立实验的边界说明。

## 1. 已进入正式个人日报的能力

当前 `DailyTraceRunner` 已使用：

- 本人发言锚点
- 本人 reaction 锚点
- 锚点前后文窗口
- 锚点窗口的 LLM 会话分段
- 分段失败后直接从本人参与的聊天窗口提炼
- 锚点/片段级按需扩窗
- 附件和飞书文档正文按需补充
- reply/quote 和 reaction response signal

因此“锚点尚未进入主流程、主流程仍是一会话一个 ConversationSlice”的旧结论已经失效。

## 2. 仍只属于独立实验的能力

`src/worktrace/anchor_experiment.py` 仍独立提供：

- 持久化锚点级缓存
- `--ignore-cache` / `--refresh-cache`
- `summary-only` / `summary-table`
- `completion_mode_counts`
- 实验专用调试目录和逐轮结果

这些能力没有接入正式个人日报产物。正式 runner 只有单次运行内的分段窗口缓存，不会读取实验缓存。

## 3. 两条入口

正式个人日报：

```bash
python3 -m src.worktrace.cli --date YYYY-MM-DD
```

独立锚点实验：

```bash
python3 -m src.worktrace.anchor_experiment --date YYYY-MM-DD
```

实验输出 JSON/表格和调试文件，不写正式个人 Markdown，也不执行正式全日工作流校正、文件证据聚合和自发送链路。

## 4. 代码落点

正式主链：

- `src/worktrace/runner.py`
- `src/worktrace/pipeline/anchors.py`
- `src/worktrace/pipeline/conversation_segments.py`
- `src/worktrace/pipeline/anchor_expansion.py`

独立实验：

- `src/worktrace/anchor_experiment.py`
- `src/worktrace/cache/`

## 5. 相关文档

- [分段、扩窗与回退](conversation-slice-retry-design.md)
- [锚点协议](anchor-analysis-protocol.md)
- [锚点实验使用说明](anchor-experiment-usage.md)
- [锚点设计演进记录](anchor-first-multi-pass-design.md)
