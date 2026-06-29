---
name: worktrace
description: 帮员工从指定日期里与自己直接相关的飞书工作聊天中提炼工作事件，生成脱敏后的个人 Markdown，并把结果先发给员工自己；也支持管理人员把多人已生成的 WorkTrace Markdown 放入 merge_inbox 后做团队汇总合并。默认“跑某日数据”指个人日报，只有用户明确提到合并、收集、管理、团队汇总或 merge_inbox 时才执行多人合并。
---

# WorkTrace

## 用途

WorkTrace 用于从飞书聊天中提取指定日期里与本人直接相关的工作沟通内容，围绕本人发言上下文整理工作事件，调用兼容 OpenAI `Responses API` 的在线模型做结构化提炼，并将结果写入本地 Markdown 文件，随后通过飞书 CLI 把文件发给当前登录用户自己。

这个 skill 的目标是帮助员工整理自己的工作事件，不是为了抓取私人聊天，也不是为了直接替员工向上级发汇总。

WorkTrace 另有一个管理人员汇总模式：管理人员先收集多人已经生成的 WorkTrace Markdown，放入 `merge_inbox/YYYY/MM/DD/`，再执行多人事件合并，生成同目录 `_merged.md`。该模式不重新读取原始飞书聊天。

## 使用方式

当用户要求回顾某一天与自己直接相关的工作聊天、从飞书聊天生成个人工作事件记录、或将当天沟通整理成结构化事件时，使用本 skill。

意图判定规则：

- 用户只说“跑一下 29 日数据”“生成 29 日数据”“处理 2026-06-29”这类模糊说法时，默认执行个人日报流程。
- 用户明确说“合并 29 日收集的 MD”“管理人员汇总”“团队汇总”“多人合并”“merge_inbox 里的文件”“生成 _merged.md”时，执行管理人员汇总流程。
- 如果用户身份是管理人员，但表达仍然只是“跑一下 29 日数据”，不要直接推断为多人合并；应先确认是个人日报还是合并已收集 Markdown。
- 如果用户同时提到“收集的文件/多人 Markdown/合并”，即使没有写出 `merge-collected`，也按管理人员汇总流程处理。

每次使用前，都必须先检查用户是否已经提供本地在线模型配置；如果没有配置，必须中止执行并明确要求用户先提供：

- 必须在仓库本地单独配置在线模型参数
- 推荐使用仓库根目录 `.env`
- `WORKTRACE_LLM_BASE_URL`、`WORKTRACE_LLM_MODEL`、`WORKTRACE_LLM_API_KEY` 缺一不可
- `WORKTRACE_LLM_REASONING_EFFORT` 默认应为 `none`
- 这些内容不能提交到 git 仓库

优先按以下流程执行：

1. 确认目标日期。
2. 在仓库根目录执行 WorkTrace 的 Python 入口，而不是直接在 `SKILL.md` 中实现业务逻辑。
3. 让 Python 负责聊天抓取、窗口裁剪、批量组装、结果校验、跨会话合并、文件链接聚合、Markdown 写入和自送达。
4. 每日 Markdown 文件创建成功后，通过当前登录的飞书 CLI 身份把结果发给用户自己。
5. 仅在需要语义提取时调用在线模型做批量分析。
6. 主流程默认且强制使用 `/no_think` 与请求体关闭推理配置。

个人日报命令：

```bash
python -m src.worktrace.cli --date YYYY-MM-DD
```

管理人员汇总命令：

```bash
python -m src.worktrace.cli merge-collected --date YYYY-MM-DD
```

管理人员汇总模式约定：

- 输入目录固定为 `merge_inbox/YYYY/MM/DD/`。
- 来源文件名固定为 `YYYY-MM-DD-姓名.md`。
- 输出文件固定为同目录 `_merged.md`。
- `_merged.md` 会保留来源人员和来源事件 ID。
- 有 `config/merge_delivery.local.json` 时，上传到配置的飞书 Drive 文件夹下 `YYYY/MM/DD/` 目录；没有配置时只生成本地文件。
- 更多细节见 `docs/collected-people-merge-plan.md`。

## 约束

- 仓库根目录就是 skill 根目录，不使用单独的 `skill/` 子目录。
- 核心实现应放在 `src/`，测试放在 `tests/`，设计文档放在 `docs/`。
- 不要让 LLM 参与数据计算；统计或计算必须由 Python 完成。
- 原始聊天内容不应长期落盘，长期保留的只有结构化事件清单。
- 最终对员工可见的 Markdown 应优先保留 `日期`、`事件标题`、`事件内容`、`涉及文件链接`。
- 不应在员工最终产物中显示人名、群名、open_id、消息 ID、会话 ID 等隐私或内部标识。
- 管理人员汇总产物例外：允许显示来源人员和来源事件 ID，因为该产物用于团队事项审阅和来源追溯。
- 文档链接的主要用途是帮助员工以后回忆事件细节，不是让模型围绕链接做推理。
- 每次成功生成当天 Markdown 文件后，都应通过飞书 CLI 将结果发送给当前登录用户自己，作为自送达副本。
- 管理人员汇总模式不自送达给当前用户；只生成 `_merged.md`，并在配置存在时上传到指定飞书 Drive 文件夹。

## 隐私说明

使用这个 skill 时，应当明确向用户说明以下事实：

- WorkTrace 只处理目标日期内本人发过消息的会话
- WorkTrace 只尝试提取与本人直接相关的工作事项
- WorkTrace 默认不过滤不必要的人名和隐私标识到最终 Markdown
- WorkTrace 默认不长期保存原始聊天记录
- WorkTrace 会把经过裁剪和压缩后的必要上下文发送到用户自己配置的在线 LLM 服务
- WorkTrace 默认把结果先发送给员工自己，而不是自动发给领导

不要夸大安全性，也不要承诺系统当前做不到的事情。

## 参考

- 详细设计：`docs/detailed-design.md`
- 管理人员多人合并设计：`docs/collected-people-merge-plan.md`
- 项目说明：`README.md`
