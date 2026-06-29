# 管理人员多人事件 Markdown 合并方案

## Summary

新增 `merge-collected` 子命令，用于管理人员把多人提交的 WorkTrace Markdown 放入 `merge_inbox/YYYY/MM/DD/` 后，生成同目录 `_merged.md` 团队汇总文件。

合并结果保持标准 WorkTrace Markdown 兼容，同时额外展示来源人员、来源事件 ID。相同原始 `event_id` 只有在标题和内容完全一致，或标题一致且内容一方包含另一方时，才先确定性归组；如果 `event_id` 相同但不满足该规则，则记录 warning 并交给 LLM 保守判断。其余事件也由 LLM 判断是否属于同一真实工作事件，并对每个合并组生成管理视角的综合描述。

## Key Changes

- 新增 CLI：`python -m src.worktrace.cli merge-collected --date YYYY-MM-DD`，旧命令 `python -m src.worktrace.cli --date YYYY-MM-DD` 行为不变。
- 输入固定读取 `merge_inbox/YYYY/MM/DD/` 下 `.md` 文件，跳过 `_merged.md`、隐藏文件、非 Markdown、格式错误文件。
- 人员名从文件名提取，固定格式为 `YYYY-MM-DD-姓名.md`，例如 `2026-06-29-张三.md` 提取为 `张三`。
- 相同 `event_id` 只有在标题和内容完全一致，或标题一致且内容一方包含另一方时才作为确定性合并组；其它情况不锁死，交给 LLM 判断，并在 JSON 结果里记录 warning。
- 多人合并 prompt 继续要求 LLM 不输出薪资、绩效、争吵、辱骂等敏感事项；本地最终结果可复用现有敏感关键词过滤作为兜底。
- 输出 `_merged.md` 保留 WorkTrace 事件注释和标准字段，并新增来源人员、来源事件 ID 字段。
- 新增 `config/merge_delivery.example.json`；实际配置 `config/merge_delivery.local.json` 加入 `.gitignore`。
- 有本地飞书 Drive 文件夹配置时，在该文件夹下创建 `YYYY/MM/DD/` 目录结构并上传原始 `_merged.md`；无配置时只生成本地文件；上传失败只写 warning。

## Design Details

- 读取阶段
  - 输入目录由 `--date` 拆成 `merge_inbox/YYYY/MM/DD/`，例如 `2026-06-29` 对应 `merge_inbox/2026/06/29/`。
  - 只读取当前目录下的普通 `.md` 文件；跳过 `_merged.md`、隐藏文件、子目录和非 Markdown 文件。
  - 文件名必须匹配 `YYYY-MM-DD-姓名.md`。不匹配时跳过该文件并写 warning。
  - Markdown 解析复用现有 WorkTrace 标准格式：front matter 加 `<!-- worktrace:event:start event_id="..." -->` 事件块。坏文件跳过并写 warning，不影响其它文件。

- 事件身份与来源
  - 每条来源事件在本次合并中生成临时 `draft_id`，格式可由来源文件名、事件序号和原始 `event_id` 组成，用于 LLM 分组和结果校验。
  - 最终 `_merged.md` 的事件使用新的汇总 `event_id`，由日期、合并组内 `draft_id`、LLM 生成的标题和内容稳定生成。
  - 最终事件保留 `来源人员` 和 `来源事件 ID`。普通个人日报不渲染空来源字段，避免影响旧命令输出。

- 确定性合并规则
  - 相同原始 `event_id` 是强信号，但不是无条件合并依据。
  - 只有满足以下任一条件时，才把相同 `event_id` 的事件锁定为确定性合并组：
    - 标题相同且内容完全相同。
    - 标题相同且内容一方包含另一方。
  - 如果相同 `event_id` 不满足上述规则，则不锁定合并，写入 warning：`Same event_id has divergent content: ...`，并交给 LLM 判断。

- LLM 合并协议
  - LLM 输入包含两部分：已锁定的 `deterministic_groups`，以及需要判断的剩余事件。
  - LLM 必须返回 `groups`，每个 group 包含 `group_id`、`draft_ids`、`title`、`content`。
  - Prompt 明确要求：确定性组不能拆分；剩余事件拿不准就分开；每个输入 `draft_id` 必须且只能出现一次；不要编造未出现的信息。
  - 如果 LLM 返回漏项、重复项、未知项或破坏确定性组，Python 侧修复为安全结果：有效组保留，漏项回退为单独事件，并记录 warning。

- 敏感内容与失败策略
  - Prompt 继续要求 LLM 不输出薪资、绩效、争吵、辱骂等敏感事项。
  - Python 侧在最终结果上复用现有敏感关键词过滤作为兜底；被过滤的事件写 warning。
  - 空目录、无有效事件、全坏文件都生成空 `_merged.md`，返回 `success_with_warnings`。
  - LLM 协议错误导致本地无法形成安全结果时返回 `failed`；上传失败不影响本地结果，只写 warning。

- 飞书上传
  - `config/merge_delivery.local.json` 存放真实 Drive 目标目录，不提交 Git。
  - 配置存在时，以配置目录为汇总根目录，按日期创建 `YYYY/MM/DD/` 三级目录，再上传原始 `_merged.md` 文件。
  - 配置不存在时 `upload_status=skipped`；上传或建目录失败时 `upload_status=failed`，本地 `_merged.md` 保留。

## Public Interfaces

- CLI：
  `python -m src.worktrace.cli merge-collected --date YYYY-MM-DD`
- 本地配置示例：

```json
{
  "feishu_drive_folder_url": "https://example.feishu.cn/drive/folder/..."
}
```

- JSON 执行结果包含：
  `status`、`target_date`、`input_dir`、`output_path`、`source_file_count`、`source_event_count`、`merged_event_count`、`skipped_file_count`、`warning_messages`、`upload_status`、`upload_target`、`upload_error`。

## Test Plan

- 单元测试覆盖人员名提取、标准 Markdown 解析、坏文件跳过、`_merged.md` 不参与输入、相同 `event_id` 且内容完全一致或包含时确定性合并、相同 `event_id` 但不满足确定性规则时交给 LLM 并产生 warning、输出包含来源信息、无上传配置不调用飞书。
- 单元测试覆盖最终敏感兜底过滤，以及多人合并 prompt 包含敏感事项排除要求。
- 集成测试覆盖 `merge-collected --date` 输出结构化 JSON、多个输入生成 `_merged.md`、坏文件不阻断有效合并、飞书配置存在时创建 `YYYY/MM/DD/` 并上传、旧 CLI 命令行为不变。
- 空目录、无有效事件、全坏文件均返回成功带 warning，并生成空汇总或空结果。

## Assumptions

- 输入来自各人员已生成的 WorkTrace Markdown，不重新读取原始聊天。
- 管理汇总允许展示来源人员名和来源事件 ID。
- v1 只处理同一天多人合并，不做跨天合并。
- v1 只上传原始 `.md` 文件到飞书 Drive 文件夹，不导入为在线文档。
- LLM 合并规则保守，拿不准就分开。
