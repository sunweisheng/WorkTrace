# WorkTrace 当前实现拆解

## 1. 文档目标

本文档只描述当前仓库里已经在使用的主流程实现，避免历史设计稿和现有代码混在一起。

## 2. 当前主流程

当前日处理链路如下：

1. `cli.py` 解析 `--date`
2. `preflight.py` 做依赖、身份和在线模型配置检查
3. `runner.py` 拉取目标日期内本人参与且本人当日发过消息的会话
4. Python 过滤明显无效消息
5. `conversation_first_pass.py` 按会话生成 `ConversationSlice`
6. 每个会话切片单独调用一次 analyzer，必要时按 `context_requests` 扩窗重跑
7. 汇总全日 `candidate_events`
8. `merge_day_candidates(...)` 做跨会话分组
9. Python 物化 `MergedEventDraft` 并构建最终 `WorkEvent`
10. `stores/markdown.py` 覆盖写入 `data/YYYY/MM/YYYY-MM-DD.md`

## 3. 当前关键模块

- `src/worktrace/cli.py`
  负责参数解析、preflight、退出码映射和 JSON 输出。
- `src/worktrace/runner.py`
  负责单日主流程编排。
- `src/worktrace/pipeline/conversation_first_pass.py`
  负责当前主流程使用的会话级切片构造。
- `src/worktrace/pipeline/context_expansion.py`
  负责 `context_requests` 校验、上下文补充和单切片重跑输入构造。
- `src/worktrace/pipeline/cross_conversation_merge.py`
  负责把跨会话分组结果物化为 `MergedEventDraft`。
- `src/worktrace/pipeline/event_merge.py`
  负责最终事件构建与去重。
- `src/worktrace/stores/markdown.py`
  负责 Markdown 读写。

## 4. 当前配置

当前主流程仍在使用的关键配置位于 [src/worktrace/config.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/config.py:115)：

- `timezone = "Asia/Shanghai"`
- `analyzer_backend = "hook"`
- `hook_command = "python3 -m src.worktrace.hook_runner --mode responses-http"`
- `slice_base_limit = 150`
- `max_model_input_tokens = 100000`
- `slice_retry_limit = 3`
- `prompt_slice_message_limit = 40`
- `prompt_message_char_limit = 300`
- `prompt_attachment_char_limit = 800`
- `analyzer_timeout_seconds = 180`
- `codex_stdin_mode = False`

以下配置当前只在锚点实验路径使用：

- `anchor_retry_limit = 3`
- `anchor_batch_size = 3`

## 5. 当前接口约束

- `EventStore.replace_day(...)` 当前签名是 `replace_day(target_date, events)`。
- `Analyzer` 当前只承担会话分析、锚点批量分析和日级 merge。
- 主流程当前只输出结构化事件，不再生成管理者总结。

## 6. 已移除的旧路径

以下旧路径已不再保留或已从当前实现中清理：

- 基于旧 `pipeline/slicing.py` 的多 slice 会话切分
- 基于旧 `pipeline/batching.py` 的多 slice 批处理组批
- 基于 bucket / cross-bucket 的旧跨会话合并链路
- 额外的管理者总结产出层

如果后续需要新的实现方案，应新增对应设计文档，而不是继续维护与当前代码脱节的旧拆解稿。
