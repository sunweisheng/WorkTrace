# WorkTrace Hook Runner 当前实现说明

## 1. 文档目标

本文档说明当前 `HookAnalyzer + hook_runner.py` 链路已经落地的行为，不再保留旧的升级设计草案。

## 2. 当前默认链路

当前默认分析链路为：

`HookAnalyzer -> hook_runner.py --mode responses-http -> 在线 Responses API provider`

默认命令定义在 [src/worktrace/config.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/config.py:8)。

## 3. 当前已实现能力

- `HookAnalyzer` 会按任务传入 `output_schema`
- `HookAnalyzer` 会把 schema 写入临时文件，并通过 `WORKTRACE_HOOK_SCHEMA_PATH` 传给 hook runner
- `hook_runner.py` 支持 `responses-http` 和 `codex-stdin` 两种模式
- `hook_runner.py` 会读取 `WORKTRACE_HOOK_SCHEMA_PATH`
- `hook_runner.py` 会把响应归一化为干净 JSON 后写回 `stdout`

对应代码见：

- [src/worktrace/analyzers/hook.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/analyzers/hook.py:64)
- [src/worktrace/hook_runner.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/hook_runner.py:13)
- [src/worktrace/analyzers/output_schemas.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/analyzers/output_schemas.py:1)

## 4. 当前 schema 范围

当前共享 schema 包括：

- `batch_output_schema()`
- `anchor_batch_output_schema()`
- `merge_output_schema()`

## 5. 当前失败语义

- 外部命令返回码非 `0` 时，`HookAnalyzer` 直接失败
- `stdout` 为空时直接失败
- 归一化后不是合法 JSON 时直接失败
- 业务结构是否合法，继续由 protocol parser 校验

## 6. 维护原则

如果后续 Hook 链路再次发生结构变化，应直接修改本说明文档，不再新增“升级设计但未落地”的平行文档。
