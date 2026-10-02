# 全仓文档与代码核对记录

核对日期：2026-10-02。代码基准：WorkTrace 4.1.0，提交
`776faf6d1596b4e5ef25685c0ef1b7f9a3673b74`。
本次以代码和配置为准修正文档，不修改业务代码、测试或运行配置。

## 范围与方法

逐份读取仓库已有的 23 份 Markdown，核对正式 CLI、个人日报、多人汇总、
图片与附件、诊断、实验、维护脚本、缓存、输出和隐私边界。
同时检查 `.env.example`、`requirements.txt` 及安装脚本的配置提示。
历史测试与真实模型测量保留原日期和结果，不改写成此次验收；尚未实现的
能力与独立实验不当作正式流程能力。

| 文档 | 核对重点及结果 |
| --- | --- |
| [README](../../README.md) | 修正重试层级、单成员改写、续跑签名、诊断入口、删除范围与团队事实校验边界 |
| [WorkTrace 技能](../../SKILL.md) | 修正各阶段失败策略、错误反馈范围、显式回放模式和 Codex Schema 使用范围 |
| [详细设计](../detailed-design.md) | 按正式路由、复核、分组、缓存及输出更新流程与模块说明 |
| [实现拆解](../implementation-breakdown.md) | 核对当前文件和函数职责、模式工厂、正式缓存与实验缓存 |
| [员工说明](../employee-guide.md) | 补全默认 Codex 配置，区分模式自检、续跑和送达状态 |
| [隐私说明](../privacy-note.md) | 核对模型输入、隐藏来源信息及原始调试数据边界 |
| [分段与扩窗](../conversation-slice-retry-design.md) | 区分请求重试、阶段重试、分段失败回退及上下文扩展 |
| [跨会话合并](../cross-conversation-merge-design.md) | 删除过时当前指引，改成全日分组、标题发现与完整内容复核 |
| [事件分组](../workstream-free-event-grouping-design.md) | 核对两种模式、关系验证、所有最终组改写及阶段失败策略 |
| [多人汇总](../collected-people-merge-plan.md) | 修正 v3 来源资格、人工修订、预算、事实覆盖及现有 trace 限制 |
| [两级汇总](../two-level-collected-merge-improvement-plan.md) | 区分已实现能力、历史 V2 样本和仍需真实验收的 V3 场景 |
| [手工编辑](../manual-report-editing-design.md) | 修正删除遗忘范围、修订类型传播和新增事件的来源关系 |
| [Markdown 输出](../markdown-output-simplification-design.md) | 核对当前可见字段、v3 隐藏字段及旧格式兼容 |
| [文件关联](../personal-file-links-fix.md) | 核对链接保留、去重、补读证据和附件下载边界 |
| [Online 调用](../online-analyzer-usage.md) | 修正两种模式、启动探针、请求与质量重试以及协议差异 |
| [锚点协议](../anchor-analysis-protocol.md) | 核对实际字段、枚举、证据处理及正式回退与实验共用范围 |
| [锚点实验](../anchor-experiment-usage.md) | 修正批量成功/失败后的扩窗区别、独立缓存指纹和运行方式 |
| [锚点实现](../anchor-first-implementation-breakdown.md) | 区分正式主链、锚点降级和独立实验，修正状态声明 |
| [锚点多轮设计](../anchor-first-multi-pass-design.md) | 将设计意图与当前实际调用和缓存行为对应 |
| [提示词历史对照](../personal-grouping-prompt-comparison-2026-07-22.md) | 保留历史实测，明确不是当前两种模式及所有协议的验收 |
| [模式实施计划](2026-10-02-llm-routing-modes.md) | 补充已实现状态，限定正式入口、缓存保证及显式回放参数 |
| [模式验证记录](2026-10-02-llm-routing-modes-validation.md) | 保留实施时结果，明确五阶段请求次数来自仅 Online 模拟测试 |
| [汇总收件目录](../../merge_inbox/README.md) | 修正 v3/v2/人工修订资格，补充离线与仅 Online 模式 |

## 关键代码依据

- `src/worktrace/cli.py`：全局参数位置、配置加载、自检、普通重跑清理、
  `--resume` 和诊断附加条件。
- `src/worktrace/config.py`、`factories.py`、`preflight.py`：模式选择、配置
  优先级、预算选择及两种模式对应探针。
- `src/worktrace/analyzers/failover.py` 与 `runner.py`：请求层重试和各阶段
  外层循环并不相同；`_is_terminal_online_request_error` 仅适用于
  `online_only` 的 `request_failed` 失败。
- `src/worktrace/pipeline/llm_checkpoints.py`：正式续跑签名包含模式和
  序列化窗口/批次，不包含模型名、完整 prompt 或全部规则配置。
- `src/worktrace/anchor_experiment.py` 与 `cache/fingerprints.py`：独立实验
  使用另一套指纹；正常批量返回不自动进入逐锚点扩窗。
- `src/worktrace/collected_merge.py`、`stores/markdown.py`：来源资格、人工
  修订、组成员锁定及正文事实来源覆盖。Python 校验编号和结构，不逐句
  证明团队正文语义；删去当前事件不追溯删除旧文件或旧 trace。
- `scripts/replay_day_with_trace.py`：默认遵循配置，保留显式
  `--analyzer-backend online/codex` 到两种模式的映射。

## 现有代码限制

`src/worktrace/collected_merge.py` 的
`_refresh_collected_merge_trace_step` 在有 trace step 和用量记录器时引用
未定义的 `source_coverage_error`。默认双线路模式中，开启 trace 后在分组或
完整复核质量重试用尽后进入显式备用刷新分支时，可能触发 `NameError`。
仅 Online 没有该显式备用线路。

本次用隔离的 Python 对象和模拟 step 调用了该方法，确认得到
`NameError: name 'source_coverage_error' is not defined`；没有读取业务数据，
没有发起模型请求。该问题未在本次文档任务中修改，不能因为已有完整测试
通过就宣称此分支也经过验收。

另有一处配置检查与装配的差异：`preflight.ensure_reasoning_disabled`
会把非 `none` 的 Online 推理配置在默认模式报告为
`online_fallback=disabled`，但工厂没有读取该状态；
`load_online_llm_settings` 也没有拒绝其他推理值，因此后续仍可能创建
Online 备用。Online 请求体仅在生效值为 `none` 时发送关闭思考的字段。
仅 Online 模式的自检会因此失败。这项限制已按上述代码核对并同步到
使用说明和设计文档，本次同样未改实现。

## 本次验证结果与边界

- 完整自动测试：`python3 -m pytest -q`，`891 passed in 13.42s`。
- 文档契约测试：`python3 -m pytest -q tests/unit/test_docs_contract.py`，
  48 项通过，未修改测试断言。
- Skill 结构验证：`quick_validate.py` 返回 `Skill is valid!`。
- 23 份原文档均已列入核对表；Markdown 本地链接、代码块闭合和用词检查
  通过，`git diff --check` 通过。
- 仅修改 Markdown；业务代码、测试和运行配置未改动。

本次仅执行文档、结构与模拟测试检查，不重新调用真实模型，不生成正式
日报，也不发送飞书消息。模式实施阶段的真实文字和图片结果仍以对应
历史验证记录为准。
