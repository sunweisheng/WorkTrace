# 管理人员多人事件 Markdown 合并方案

## Summary

新增 `merge-collected` 子命令，用于管理人员把多人提交的 WorkTrace Markdown 放入 `merge_inbox/YYYY/MM/DD/` 后，把日期根目录和一级子目录分别作为独立合并范围，各自生成本目录 `YYYY-MM-DD-登录人姓名-merged.md` 团队汇总文件。

合并结果保持标准 WorkTrace Markdown 兼容，同时额外展示来源人员、来源事件 ID。相同原始 `event_id` 只有在标题和内容完全一致，或标题一致且内容一方包含另一方时，才先确定性归组；如果 `event_id` 相同但不满足该规则，则记录 warning 并交给 LLM 保守判断。其余事件也由 LLM 判断是否属于同一真实工作事件，并对每个合并组生成管理视角的综合描述。若某个合并组包含“当前登录用户自己的个人事件 MD”来源，则最终内容以该来源为主，其他来源仅作不冲突补充。读取来源事件和写入最终团队汇总文件前，都会执行与个人日报一致的结构化保留门槛。

## Key Changes

- 新增 CLI：`python -m src.worktrace.cli merge-collected --date YYYY-MM-DD`，旧命令 `python -m src.worktrace.cli --date YYYY-MM-DD` 行为不变。
- 输入固定读取 `merge_inbox/YYYY/MM/DD/` 及其一级子目录当前层的 `.md` 文件，支持普通个人日报和上游 `*-merged.md`，但仍跳过旧 `_merged.md`、当前目录本次输出同名 `YYYY-MM-DD-登录人姓名-merged.md`、隐藏文件、非 Markdown、格式错误文件。
- 人员名从文件名提取，只要能识别出日期和姓名成分即可，例如 `2026-06-29-张三.md`、`张三-2026-06-29.md`、`张三_2026-06-29.md` 都可识别为 `张三`。
- 若来源文件名中的姓名与当前登录用户姓名精确匹配，则该来源事件被标记为“合并人来源”；若当前目录没有匹配到合并人来源，则写 warning 并回退为普通多人合并。
- 相同 `event_id` 只有在标题和内容完全一致，或标题一致且内容一方包含另一方时才作为确定性合并组；其它情况不锁死，交给 LLM 判断，并在 JSON 结果里记录 warning。
- 多人合并 prompt 继续要求 LLM 不输出薪资、绩效、争吵、辱骂等敏感事项，并要求每个 group 输出 `object_hint`、`retention_reason`、`retention_detail`。
- 输出 `YYYY-MM-DD-登录人姓名-merged.md` 保留 WorkTrace 事件注释和标准字段，并新增来源人员、来源事件 ID 字段；保留元数据继续写入 Markdown，供追溯和后续汇总使用。
- 每个生成的团队汇总文件都会通过飞书 CLI 机器人身份发送给当前登录用户自己；发送失败只写 warning，不影响本地文件。

## Design Details

- 读取阶段
  - 输入目录由 `--date` 拆成 `merge_inbox/YYYY/MM/DD/`，例如 `2026-06-29` 对应 `merge_inbox/2026/06/29/`。
  - 日期根目录始终作为一个合并范围；日期目录下的每个一级子目录也作为独立合并范围。
  - 每个合并范围只读取当前目录下的 `.md` 文件；支持上游 `*-merged.md` 继续参与汇总，但跳过旧 `_merged.md`、当前目录本次输出同名 `YYYY-MM-DD-登录人姓名-merged.md`、隐藏文件、子目录和非 Markdown 文件，不递归更深层目录。
  - 文件名只要能识别出 `YYYY-MM-DD` 和姓名成分即可；顺序可前可后。个人日报和 `*-merged.md` 都可识别来源姓名；不匹配时跳过该文件并写 warning。
  - Markdown 解析复用现有 WorkTrace 标准格式：front matter 加 `<!-- worktrace:event:start event_id="..." -->` 事件块。坏文件跳过并写 warning，不影响其它文件。
  - 解析出的来源事件必须通过结构化保留门槛；缺少保留理由、保留依据或具体对象的事件会被过滤，不进入 LLM 合并 prompt。

- 事件身份与来源
  - 每条来源事件在本次合并中生成临时 `draft_id`，格式可由来源文件名、事件序号和原始 `event_id` 组成，用于 LLM 分组和结果校验。
  - 最终团队汇总文件的事件使用新的汇总 `event_id`，由日期、合并组内 `draft_id`、LLM 生成的标题和内容稳定生成。
  - 最终事件保留 `来源人员` 和 `来源事件 ID`。如果输入本身是上游 `*-merged.md`，则继续沿用其事件里已经写好的 `来源人员` / `来源事件 ID`，避免二次合并时只剩部门负责人名字。

- 确定性合并规则
  - 相同原始 `event_id` 是强信号，但不是无条件合并依据。
  - 只有满足以下任一条件时，才把相同 `event_id` 的事件锁定为确定性合并组：
    - 标题相同且内容完全相同。
    - 标题相同且内容一方包含另一方。
  - 如果相同 `event_id` 不满足上述规则，则不锁定合并，写入 warning：`Same event_id has divergent content: ...`，并交给 LLM 判断。

- LLM 合并协议
  - LLM 输入包含两部分：已锁定的 `deterministic_groups`，以及需要判断的剩余事件。
  - 每条来源事件都会带上是否为“合并人来源”的标记；LLM 仍负责判断哪些事项属于同一真实事件。
  - LLM 必须返回 `groups`，每个 group 包含 `group_id`、`draft_ids`、`title`、`content`、`object_hint`、`retention_reason`、`retention_detail`。
  - Prompt 明确要求：确定性组不能拆分；剩余事件拿不准就分开；每个输入 `draft_id` 必须且只能出现一次；不要编造未出现的信息。
  - 若某个 group 含有“合并人来源”，最终 `title`、`content`、`object_hint`、`retention_reason`、`retention_detail` 都以该来源为主，其它来源只能补充不冲突的信息，不能改写该来源中已明确的版本、结论、进展、结果或待办指向。
  - 如果 LLM 返回漏项、重复项、未知项或破坏确定性组，Python 侧修复为安全结果：有效组保留，漏项回退为单独事件，并记录 warning。
  - 普通约时间、互通信息、泛泛完成审核/审批但无具体对象和结论的事件不应输出；如果输出，Python 写入前会再次过滤。

- 敏感内容与失败策略
  - Prompt 继续按 `config/event_rules.json` 中的敏感关键词要求 LLM 不输出薪资、绩效、争吵、辱骂等敏感事项。
  - Python 侧不再按敏感关键词过滤最终事件；精确排除规则会在来源事件阶段和合并后事件阶段都检查。
  - Python 侧在读取来源事件后和写入团队汇总文件前都执行结构化保留门槛；被过滤的事件写 warning。
  - 空目录、无有效事件、全坏文件都生成空团队汇总文件，返回 `success_with_warnings`。
  - LLM 协议错误导致本地无法形成安全结果时返回 `failed`；上传失败不影响本地结果，只写 warning。

- 飞书自发送
  - 管理人员汇总与个人日报一样，都会通过飞书 CLI 机器人身份把结果文件发送给当前登录用户自己。
  - 若一次运行生成多个汇总文件，则根目录结果和各一级子目录结果都会分别发送。
  - 发送失败时 `self_delivery_status=failed`，本地文件保留，整体结果降级为 `success_with_warnings`。

## Public Interfaces

- CLI：
  `python -m src.worktrace.cli merge-collected --date YYYY-MM-DD`
- JSON 执行结果包含：
  `status`、`target_date`、`input_dir`、`output_path`、`source_file_count`、`source_event_count`、`merged_event_count`、`skipped_file_count`、`warning_messages`、`self_delivery_status`、`self_delivery_target`、`self_delivery_error`、`outputs`。
- `outputs` 逐项记录每个合并范围的 `input_dir`、`output_path`、`source_file_count`、`source_event_count`、`merged_event_count`、`skipped_file_count`、`warning_messages`、`self_delivery_status`、`self_delivery_target`、`self_delivery_error`。

## Test Plan

- 单元测试覆盖人员名提取、标准 Markdown 解析、坏文件跳过、旧 `_merged.md` 和当前目录本次输出同名文件不参与输入、上游 `*-merged.md` 可继续参与输入、相同 `event_id` 且内容完全一致或包含时确定性合并、相同 `event_id` 但不满足确定性规则时交给 LLM 并产生 warning、输出包含来源信息、多人合并结果会自发送。
- 单元测试覆盖“合并人来源”识别、未匹配到合并人来源时的 warning，以及相同事项中合并人版本优先信号会被送入 LLM prompt。
- 单元测试覆盖最终敏感兜底过滤、结构化保留门槛，以及多人合并 prompt 包含敏感事项和保留元数据要求。
- 集成测试覆盖 `merge-collected --date` 输出结构化 JSON、多个输入生成 `YYYY-MM-DD-登录人姓名-merged.md`、坏文件不阻断有效合并、多人合并自发送结果、旧 CLI 命令行为不变。
- 空目录、无有效事件、全坏文件均返回成功带 warning，并生成空汇总或空结果。

## Assumptions

- 输入来自各人员已生成的 WorkTrace Markdown，不重新读取原始聊天。
- 管理汇总允许展示来源人员名和来源事件 ID。
- v1 只处理同一天多人合并，不做跨天合并。
- “合并人自己的个人事件 MD” 识别规则固定为：来源文件名中的姓名与当前登录用户名精确匹配。
- LLM 合并规则保守，拿不准就分开。
