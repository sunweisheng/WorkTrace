# WorkTrace 锚点优先方案编码拆解

## 1. 文档目标

本文基于 [anchor-first-multi-pass-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/anchor-first-multi-pass-design.md) ，把“锚点优先、多轮扩窗、局部缓存、最终合并”的方案拆成可执行编码任务，作为下一阶段开发清单。

## 2. 实施顺序

建议按以下顺序推进：

1. 先加锚点级缓存基础设施
2. 再让现有 `ConversationSlice` 流程复用缓存能力
3. 然后引入 `AnchorUnit`
4. 再把首轮分析从 `slice-first` 切到 `anchor-first`
5. 最后收窄最终 merge 范围

这样做的原因是：

- 先拿到缓存收益，不必一次性重写整条链路
- 先让“输入 fingerprint / 输出复用 / 局部失效”这些关键机制稳定下来
- 再改分析单元，风险更低

## 3. 阶段拆解

### A1. 锚点级缓存基础设施

目标：

- 新增缓存模型
- 新增缓存路径规划
- 新增缓存读写 Store
- 新增输入 fingerprint 计算

建议文件：

- `src/worktrace/cache/__init__.py`
- `src/worktrace/cache/base.py`
- `src/worktrace/cache/filesystem.py`
- `src/worktrace/cache/fingerprints.py`

建议能力：

- `build_anchor_cache_key(...)`
- `build_anchor_input_fingerprint(...)`
- `AnchorCacheStore.read(...)`
- `AnchorCacheStore.write(...)`
- `AnchorCacheStore.invalidate_day(...)`

交付标准：

- 可对单个分析输入生成稳定 fingerprint
- 可把锚点级结果读写到 `data/cache/anchors/...`
- 同输入重复写入不会产生不一致键

### A2. 让现有 slice 流程先接入缓存

目标：

- 不改主分析单元，先让当前 `ConversationSlice` 流程具备“命中即跳过 LLM”的能力

建议改动：

- `runner.py`
- `pipeline/batching.py`
- `models.py`

建议行为：

- 对每个 `slice` 计算 fingerprint
- 首轮分析前先查缓存
- 命中则直接复用 `BatchAnalysisResult` 中属于该 `slice` 的结果
- 未命中才进入 `analyze_batch`

交付标准：

- 同一天重复执行时，已命中 `slice` 不再重新请求 LLM
- `DailyRunResult` 或日志里能看到 cache hit/miss

### A3. 新增 `AnchorUnit`

目标：

- 在不删除 `ConversationSlice` 的前提下，先把锚点级输入对象建起来

建议改动：

- `models.py`
- `pipeline/anchors.py`

建议能力：

- `group_anchor_units(...)`
- `build_anchor_base_window(...)`
- `expand_anchor_direct_relations(...)`

交付标准：

- 能从 `NormalizedMessage` 稳定产出 `AnchorUnit`
- 单测覆盖：连续本人消息、夹杂他人回复、reply/quote 直连

### A4. 首轮分析切到 `anchor-first`

目标：

- 首轮输入从大 `slice` 换成小 `AnchorUnit`

建议改动：

- `runner.py`
- `analyzers/prompts.py`
- `pipeline/context_expansion.py`

建议行为：

- 首轮只送最小锚点窗口
- 输出 `anchor_status`
- 仅对需要更多信息的锚点继续扩窗

交付标准：

- 首轮单次输入体积显著缩小
- 能区分 `completed / needs_more_context / needs_attachment_text / not_work_related`

### A5. 收窄最终 merge

目标：

- 不再把整天全部候选都送给 merge

建议改动：

- `pipeline/event_merge.py`
- `runner.py`

建议行为：

- 先用 Python 做预分桶
- 只把需要跨锚点 / 跨会话合并的桶送给 LLM

交付标准：

- 最终 merge 的候选数量显著少于当前全量模式

## 4. 当前最小可实施步骤

当前最值得先做的是 `A1`：

- 改动小
- 不影响主链路
- 马上能为后续 `anchor-first` 做基础铺垫
- 就算后面暂时不切 `AnchorUnit`，缓存机制也已经能复用

## 5. 本阶段建议验收方式

每做完一个阶段，都至少看三件事：

1. 单测是否通过
2. 真实日期重复运行是否复用更多结果
3. 日志里是否能看见新机制确实生效

## 6. 当前实验进度补充

截至当前这轮开发，已经先落地了一个不影响主链路的隔离实验路径：

- `src/worktrace/anchor_experiment.py`
- `docs/anchor-analysis-protocol.md`
- `docs/anchor-experiment-usage.md`

当前状态可以理解为：

1. `A3` 的锚点输入对象已经落地
2. `A4` 的首轮锚点识别协议已经落地到实验路径
3. second pass 先补了协议壳子，但还没有接执行器

也就是说，实施顺序文档仍然保留其长期建议价值，但代码演进上已经有一条“先把锚点实验独立跑通”的并行分支。
