# WorkTrace

WorkTrace 是一个面向个人工作回顾的自动化记录项目，目标是从飞书聊天中提取与工作相关的沟通内容，通过 LLM 做语义分析，整理成结构化工作事件清单，并写入本地 Markdown 文件。当前默认语义已经收敛为：只提取目标日期内与本人直接相关的工作事项，而不是本人参与会话中的全部工作事项。首版聚焦“手动指定日期执行”的日处理链路，不承诺真正的定时调度。

仓库根目录同时作为 Codex skill 根目录使用，安装时应链接整个仓库目录，而不是单独链接某个 `skill/` 子目录。

## 当前范围

当前已经落地的能力包括：

- 飞书聊天源，基于 `lark-cli`
- Hook / Codex analyzer 通道
- Markdown 本地存储
- 会话级首轮事件提炼
- 会话内扩窗重跑与附件正文按需补充
- 同日全量候选事件的跨会话合并
- 同日重跑覆盖
- 默认通过兼容 OpenAI `Responses API` 的在线模型服务执行 hook 分析
- 本地 `.env` / 环境变量私有配置在线模型参数，不随 git 提交
- 默认只保留与本人直接相关的事项：本人发起、本人负责、本人审批、本人催办、本人汇报、本人跟进，或他人明确要求本人推进/处理
- 只保留工作相关结果，零风险无效消息由 Python 粗过滤，其余是否属于工作事项由 LLM 判断
- 最终直接输出事件清单，不再生成管理者总结

## 当前主流程

当前代码的日处理主流程如下：

1. 拉取目标日期内本人参与且本人当日发过消息的飞书会话
2. Python 过滤明显无效消息
3. 按会话构造 `ConversationSlice`
4. `1 个会话 = 1 次首轮 LLM`，只提炼该会话中与本人直接相关的 `candidate_events`
5. 若首轮返回 `context_requests`，则在同一会话内自动补充更早消息、更晚消息或附件正文，并重跑直到收敛、无新信息或达到重跑上限
6. 汇总全日所有会话的 `candidate_events`
7. 一次性调用 `merge_day_candidates(...)`，对全日候选事件做跨会话分组
8. Python 根据分组结果物化 `MergedEventDraft`
9. Python 构建最终 `WorkEvent`
10. 将事件列表写入当天 Markdown 文件

## 仓库结构

当前仓库结构如下：

```text
.
├── SKILL.md
├── README.md
├── docs/
├── data/
├── src/
└── tests/
```

说明：

- 根目录既是通用脚本项目根目录，也是 Codex skill 根目录。
- `SKILL.md` 放在仓库根目录，作为 skill 入口说明。
- `src/` 承载可复用的 Python 逻辑。

## 核心设计摘要

- Python 负责确定性流程，包括抓取、过滤、切片、扩窗、校验、合并物化和存储。
- LLM 负责语义提炼与跨会话分组，不负责确定性流程控制。
- 默认 hook 链路为 `HookAnalyzer -> hook_runner.py --mode responses-http -> 在线 Responses API provider`。
- 旧 `codex-stdin` 路径仍保留，但仅作为显式回退和排障模式。
- LLM 不参与数据计算，计算必须由 Python 完成。
- Python CLI 固定为 `python -m src.worktrace.cli --date YYYY-MM-DD`，`stdout` 返回 machine-readable JSON 执行摘要。
- `event_id` 由 Python 基于目标日期内归一化后的来源消息集合稳定生成。
- 正式执行默认不长期落盘原始聊天内容，最终只保留结构化事件清单。
- 存储目标为按年月目录组织的每日 Markdown 文件。

## 依赖说明

当前依赖如下：

- `python3`
- `lark-cli`
- 一个兼容 OpenAI `Responses API` 的在线模型服务
- 已登录且具备消息读取权限的飞书 user 身份

当前环境默认的 Codex skill 安装目录为 `~/.codex/skills`。

## 环境要求

建议至少满足以下环境要求：

- `python3` 已安装，且版本满足项目要求
- `lark-cli` 已安装，并可在 `PATH` 中直接调用
- `lark-cli` 已登录为具备消息读取权限的飞书 user 身份
- 本地已单独配置在线模型调用参数
- 当前用户对仓库目录及 `data/` 结果目录具备可创建、可写权限
- 运行环境可使用 `Asia/Shanghai` 作为业务时区

## 本地私有模型配置

WorkTrace 当前默认通过 `hook_runner.py` 直连一个兼容 OpenAI `Responses API` 的在线模型服务。

这些配置必须单独保存在本地，不能和代码一起提交到 git。推荐做法：

1. 复制 [.env.example](/Users/sunweisheng/Documents/GitHub/WorkTrace/.env.example) 为本地 `.env`
2. 填入你自己的服务地址、模型名和 API Key
3. 保持 `.env` 只存在于本地工作区

示例：

```bash
cp .env.example .env
```

`.env` 至少需要包含：

```dotenv
WORKTRACE_LLM_BASE_URL=https://your-openai-compatible-endpoint.example/v1
WORKTRACE_LLM_MODEL=your-model-name
WORKTRACE_LLM_API_KEY=your-api-key
```

可选项：

```dotenv
WORKTRACE_LLM_TIMEOUT_SECONDS=180
```

环境变量会覆盖 `.env` 中的同名项，因此也可以在 CI 或个人 shell 中单独注入。

如果缺少 `WORKTRACE_LLM_BASE_URL`、`WORKTRACE_LLM_MODEL` 或 `WORKTRACE_LLM_API_KEY`，当前 preflight 会直接失败，并明确提示这些参数需要单独配置在本地、不能提交到 git。

## Codex Skill 安装说明

当前推荐使用本地软链接方式安装 skill，便于一边开发一边调试。安装时链接整个仓库目录。

1. 确保仓库根目录存在 `SKILL.md`
2. 创建软链接到 Codex skill 目录
3. 重启 Codex 以加载新 skill

示例命令：

```bash
mkdir -p ~/.codex/skills
ln -s /path/to/WorkTrace ~/.codex/skills/worktrace
```

说明：

- `~/.codex/skills` 是默认本地 skill 目录。
- `worktrace` 是建议的安装名。
- 如果同名目录已经存在，请先手动清理或改用其他名字。
- 安装完成后需要重启 Codex，Codex 才会重新扫描并识别新 skill。

远程 GitHub 安装方式不是当前主流程，后续如需公开分享，可再补充相应安装说明。

## 使用方式

当前使用形态如下：

- 在 Codex 对话中触发 WorkTrace skill
- 指定一个目标日期执行
- 读取目标日期内本人至少发过 1 条消息的飞书会话
- 输出与本人直接相关的结构化工作事件清单
- 将结果写入本地 Markdown 文件

具体触发提示词与参数约定由根目录 `SKILL.md` 定义，当前 README 不固定写死最终触发语句。

## 调试落盘说明

正式日处理主流程 `python -m src.worktrace.cli --date YYYY-MM-DD` 默认不会把原始聊天消息、prompt 或附件正文长期写入本地结果目录。

当前只有锚点实验路径在显式启用 `--dump-dir` 时，会把调试产物写到本地目录中，用于排查和比对模型行为。当前可能落盘的内容包括：

- `input.json`：单个锚点窗口输入消息与元数据
- `prompt.txt`：送给 analyzer 的 prompt
- `output.json`：模型返回结果
- `attachment_texts.json`：按需补充的附件正文
- 扩窗相关的请求与补充消息调试文件

这些文件只用于本地调试，不属于正式主流程产物。

## 命令行执行

直接执行某一天的主流程：

```bash
python3 -m src.worktrace.cli --date 2026-06-23
```

成功时，`stdout` 会返回类似下面的 JSON 摘要：

```json
{
  "status": "success",
  "target_date": "2026-06-23",
  "conversation_count": 12,
  "message_count": 188,
  "slice_count": 12,
  "batch_count": 12,
  "event_count": 26,
  "warning_count": 0,
  "skipped_slice_count": 0,
  "output_path": "/abs/path/to/data/2026/06/2026-06-23.md",
  "error_summary": ""
}
```

当前 Markdown 输出只保留结构化事项列表，不再额外生成“给上级汇报的当日总结”段落。

当前每日 Markdown 文件包含两个阅读层：

- `我的日报`：面向人直接阅读的简明日报视图，按最终事件逐条展示日期、事件、事件内容和结果
- `事项列表`：保留原有结构化事件块，继续作为机器可读主产物

## 运行前检查

在首次运行或环境变更后，建议先确认以下项目：

- `python3 --version` 可正常返回版本信息
- `lark-cli` 可正常执行
- 当前 `lark-cli` 登录身份为目标飞书 user，而不是 bot 或未登录状态
- 本地 `.env` 或环境变量中已配置在线模型参数
- 仓库内 `data/` 目录可创建或可写

设计上，WorkTrace CLI 在正式处理前也应执行一次 preflight 检查；若关键依赖缺失、版本不满足、未登录或目录不可写，应直接失败并返回明确原因，而不是在处理中途报错。

## 设计文档

当前相关设计说明见：

- [conversation-slice-retry-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/conversation-slice-retry-design.md)
- [cross-conversation-merge-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/cross-conversation-merge-design.md)
- [markdown-output-simplification-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/markdown-output-simplification-design.md)
- [detailed-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/detailed-design.md)

## 开发说明

- 优先修改 `src/` 中的通用逻辑。
- 仓库根目录就是 skill 根目录，不再单独维护 `skill/` 子目录。
- 任何统计或计算必须由 Python 执行，不由 LLM 执行。
