# WorkTrace 锚点实验当前状态

## 1. 文档目标

本文档说明当前仓库里的锚点实验路径已经落地到什么程度，只描述当前代码，不保留过期的任务拆解。

## 2. 当前已有能力

当前隔离实验路径位于 [src/worktrace/anchor_experiment.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/anchor_experiment.py:1)，已经具备：

- `AnchorUnit` 构造
- 锚点首轮识别协议
- 多轮扩窗执行
- 按需补附件正文
- 按需补飞书文档 / wiki 正文
- reply / quote 关系摘要入模
- 锚点级缓存
- 调试产物落盘
- `summary-only` / `summary-table` 输出

## 3. 当前仍是实验路径

当前主流程仍使用会话级 `ConversationSlice`，锚点路径尚未替换主流程：

- 主流程入口在 [src/worktrace/runner.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/runner.py:33)
- 锚点实验入口在 [src/worktrace/anchor_experiment.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/anchor_experiment.py:706)

## 4. 当前与主流程的边界

- 锚点实验结果默认不写 Markdown 日报
- 锚点实验主要用于观察协议稳定性、扩窗效果和缓存行为
- 当前实验支持 second pass 执行，不再是“只有协议壳子”

## 5. 相关文档

- [anchor-analysis-protocol.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/anchor-analysis-protocol.md)
- [anchor-experiment-usage.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/anchor-experiment-usage.md)
- [anchor-first-multi-pass-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/anchor-first-multi-pass-design.md)
