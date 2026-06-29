# WorkTrace

WorkTrace 是一个面向普通员工的个人工作事件整理工具。它的目标不是监控个人隐私，而是帮助员工把“今天和自己直接相关的工作沟通”整理成一份可以自己先审阅、再决定是否转发的每日 Markdown 记录，减少过几天或过几周后回忆不清当时发生了什么的问题。

当前阶段，WorkTrace 主要服务使用 `Codex` 或兼容 `Codex Skill` 的 Agent 的员工用户。输入来源默认是飞书聊天，输出是一份当天的本地 Markdown 文件，并通过飞书 CLI 发给员工自己。

仓库根目录同时作为 Codex skill 根目录使用，安装时应链接整个仓库目录，而不是单独链接某个 `skill/` 子目录。

## 项目目标

当前阶段的目标是：

- 帮员工提取目标日期内与自己直接相关的工作事件
- 自动过滤明显无关内容与部分敏感内容
- 输出只适合转发和回顾的结构化 Markdown
- 默认把结果先发给员工自己，而不是直接发给领导
- 从第一天起就让输出格式兼容后续人工逐级汇总

当前阶段不负责：

- 自动给领导、部门或公司做汇总
- 自动把结果上传到公司统一数据库
- 处理员工所有聊天或围观会话
- 跨天事项合并
- 真正的定时调度
- 其他 IM 工具或其他 Agent 的正式实现

## 隐私说明

这部分很重要。WorkTrace 应该让员工清楚知道它做什么，也知道它不做什么。

WorkTrace 的设计目标不是抓取私人聊天，不是做个人画像，也不是把员工看不到的数据悄悄发给别人。它当前只尝试整理“目标日期内本人发过消息、且与本人直接相关的工作事件”。

当前默认的隐私保护边界如下：

- 只处理目标日期内本人发过消息的会话
- 只保留与本人直接相关的工作事项
- 默认过滤薪资、绩效、争吵、辱骂等敏感内容
- 最终输出默认不显示人名、群名、open_id、消息 ID、会话 ID、发送时间点
- 正式主流程默认不长期保存原始聊天内容
- 长期保留的主产物是结构化 Markdown，而不是原始聊天记录
- 生成成功后默认只通过飞书 CLI 发给员工自己

同时也要诚恳说明客观边界：

- WorkTrace 会通过本地 `lark-cli` 读取飞书聊天
- WorkTrace 会把经过裁剪和压缩后的必要上下文发送到用户自己配置的在线 LLM 服务
- 因此，是否使用、使用什么模型服务、配置什么地址和密钥，应由当前用户或组织明确决定
- WorkTrace 不能承诺“任何第三方永远不会接触输入内容”，除非后续改造成纯本地模型链路

如果要打消员工疑虑，说明文件里必须坚持两点：

- 不夸大，不写“绝对安全”
- 告诉员工哪些数据会经过哪些系统，以及系统默认如何减少隐私暴露

## 当前范围

当前已经落地或将作为本阶段正式交付的能力包括：

- 飞书聊天源，基于 `lark-cli`
- Online / Codex analyzer 通道
- Markdown 本地存储
- 会话级首轮事件提炼
- 会话内扩窗重跑与附件正文按需补充
- 同日全量候选事件的跨会话合并
- 同日重跑覆盖
- 默认通过内置 Online analyzer 调用兼容 OpenAI `Responses API` 的在线模型服务
- 本地 `.env` / 环境变量私有配置在线模型参数，不随 git 提交
- 默认只保留与本人直接相关的事项
- 只保留工作相关结果，零风险无效消息由 Python 粗过滤，其余是否属于工作事项由 LLM 判断
- 默认不提炼咨询类事件、流程审核类事件、团建活动组织类事件、技能培训类事件，这类事项不作为公司级长期事件沉淀重点
- 最终直接输出事件清单，不再生成管理者总结
- 默认通过飞书 CLI 将当天 Markdown 文件发给当前登录用户自己

## 当前主流程

当前代码与本阶段目标对应的主流程如下：

1. 拉取目标日期内本人参与且本人当日发过消息的飞书会话
2. Python 过滤明显无效消息
3. 按会话构造 `ConversationSlice`
4. `1 个会话 = 1 次首轮 LLM`，只提炼该会话中与本人直接相关的 `candidate_events`
5. 若首轮返回 `context_requests`，则在同一会话内自动补充更早消息、更晚消息或附件正文，并重跑直到收敛、无新信息或达到重跑上限
6. 汇总全日所有会话的 `candidate_events`
7. 一次性调用 `merge_day_candidates(...)`，对全日候选事件做跨会话分组
8. Python 根据分组结果物化 `MergedEventDraft`
9. Python 构建最终 `WorkEvent`
10. Python 聚合每个最终事件对应的显式文档链接
11. 将事件列表写入当天 Markdown 文件
12. 通过飞书 CLI 将当天 Markdown 文件发送给当前登录用户自己

## 输出原则

当前阶段最终面向员工和后续人工汇总的 Markdown，只保留下面 4 类公共信息：

- 日期
- 事件标题
- 事件内容
- 涉及文件链接

其中“涉及文件链接”的用途是给员工未来回忆当时事件细节时使用，不是给 LLM 做复杂推理用的。真实链接应该尽量留在最终 Markdown 里给人看，而不是在 prompt 里展开给模型推理。

## LLM 调用原则

当前主流程中的 LLM 调用默认且强制使用两层约束：

- prompt 末尾统一追加 `/no_think`
- 请求体里显式要求关闭推理过程，默认配置 `WORKTRACE_LLM_REASONING_EFFORT=none`

这意味着当前链路更偏向“直接抽取、直接归并、直接结构化输出”，而不是依赖模型展示推理过程。

同时，当前 prompt 中对链接的表达也应尽量弱化：

- 可以保留 `[飞书文档]`
- 可以保留 `[飞书文档: 标题]`
- 不应把完整 URL 列表直接展开给模型

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

- Python 负责确定性流程，包括抓取、过滤、切片、扩窗、校验、合并物化、链接聚合、投递和存储。
- LLM 负责语义提炼与跨会话分组，不负责确定性流程控制。
- 默认在线链路为 `OnlineLLMAnalyzer -> OpenAI Python SDK -> 在线 Responses API provider`。
- `codex` analyzer 仍保留为非默认备选路径。
- LLM 不参与数据计算，计算必须由 Python 完成。
- Python CLI 固定为 `python -m src.worktrace.cli --date YYYY-MM-DD`，`stdout` 返回 machine-readable JSON 执行摘要。
- 如需排查真实运行中的提炼和 merge 行为，可追加 `--debug-output`，把调试文件写入本地目录。
- `event_id` 由 Python 基于目标日期内归一化后的来源消息集合稳定生成，但不应作为员工最终看到的主要字段。
- 正式执行默认不长期落盘原始聊天内容，最终只保留结构化事件清单。
- 存储目标为按年月目录组织的每日 Markdown 文件。

## 依赖说明

当前依赖如下：

- `python3`
- `lark-cli`
- `openai` Python SDK
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

WorkTrace 当前默认通过进程内 `OnlineLLMAnalyzer` 和 `openai` 官方 Python SDK 调用一个兼容 OpenAI `Responses API` 的在线模型服务。

这些配置必须由当前用户单独保存在本地，不能和代码一起提交到 git。推荐做法：

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
WORKTRACE_LLM_REASONING_EFFORT=none
```

可选项：

```dotenv
WORKTRACE_LLM_TIMEOUT_SECONDS=180
WORKTRACE_LLM_STREAM=false
WORKTRACE_LLM_TLS_VERIFY=false
WORKTRACE_LLM_SLEEP_MIN_SECONDS=1
WORKTRACE_LLM_SLEEP_MAX_SECONDS=2
```

如果需要精确排除某些事件标题或内容特征，请单独维护规则文件 [config/event_rules.json](/Users/sunweisheng/Documents/GitHub/WorkTrace/config/event_rules.json)：

```json
{
  "excluded_event_topics": [
    "代码同步",
    "工作面谈安排",
    "故障数据同步"
  ],
  "excluded_event_content_signatures": [
    "git pull",
    "聆听大老板电话",
    "本周发给哈尔滨的故障数据"
  ]
}
```

这两项都适合后续手动增删。这里建议只放“精确匹配、且确认应排除”的词，避免误伤正常工作事件。

如果需要彻底排除某些会话，不让它们进入会话信息提取链路，请单独维护 [config/conversation_blacklist.json](/Users/sunweisheng/Documents/GitHub/WorkTrace/config/conversation_blacklist.json)：

```json
{
  "excluded_conversation_ids": [
    "oc_be07388984c344d1b2d68c4a92b74c81"
  ]
}
```

命中黑名单的会话会在“目标会话发现”阶段被直接跳过，不会继续进入消息拉取、会话切片、LLM 提炼和跨会话合并。

环境变量会覆盖 `.env` 中的同名项，因此也可以在 CI 或个人 shell 中单独注入。

如果缺少 `WORKTRACE_LLM_BASE_URL`、`WORKTRACE_LLM_MODEL` 或 `WORKTRACE_LLM_API_KEY`，当前 preflight 会直接失败，并明确要求用户先在本地补齐配置；未配置时不得继续运行 WorkTrace。

## 员工使用说明

如果你是第一次使用 WorkTrace，建议先读这份面向员工的小白说明：

- [employee-guide.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/employee-guide.md)
- [privacy-note.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/privacy-note.md)

这份说明会重点覆盖：

- Windows 优先的安装步骤
- macOS 补充步骤
- 首次 `.env` 配置
- 首次使用前自检
- 正式运行
- 隐私边界与常见问题

## 使用方式

当前推荐的使用形态如下：

- 在 Codex 或兼容 Codex Skill 的 Agent 对话中触发 WorkTrace
- 指定一个目标日期执行
- 读取目标日期内本人至少发过 1 条消息的飞书会话
- 输出与本人直接相关的结构化工作事件清单
- 将结果写入本地 Markdown 文件
- 再将当天 Markdown 文件通过飞书 CLI 发给员工自己

具体触发提示词与参数约定由根目录 `SKILL.md` 定义。

## 安装入口

当前阶段推荐优先使用仓库自带安装脚本：

- Windows：`powershell -ExecutionPolicy Bypass -File .\scripts\install_worktrace.ps1`
- macOS：`bash ./scripts/install_worktrace.sh`

安装脚本当前负责：

- 检查 Python
- 安装 `requirements.txt` 中的 Python 依赖
- 初始化本地 `.env`
- 检查 `lark-cli` 是否存在
- 安装 WorkTrace skill 链接

安装脚本不会替你做这些事：

- 填写真实模型密钥
- 登录飞书账号
- 选择具体模型服务
- 重启 Codex

## 调试落盘说明

正式日处理主流程 `python -m src.worktrace.cli --date YYYY-MM-DD` 默认不会把原始聊天消息、prompt 或附件正文长期写入本地结果目录。

如果需要排查真实运行中的问题，可以显式开启：

```bash
python3 -m src.worktrace.cli --date 2026-06-23 --debug-output
```

开启后，WorkTrace 会把会话级分析和日级 merge 的调试产物写到：

```text
data/debug/conversations/<target_date>/
```

其中日级 merge 调试目录固定为：

```text
data/debug/conversations/<target_date>/_merge_day_candidates/
```

该目录当前会包含：

- `input.json`
- `prompt.txt`
- `output.json`

当前正式主流程在显式启用 `--debug-output` 时，以及锚点实验路径在显式启用 `--dump-dir` 时，都会把调试产物写到本地目录中，用于排查和比对模型行为。当前可能落盘的内容包括：

- `input.json`：单个锚点窗口输入消息与元数据
- `prompt.txt`：送给 analyzer 的 prompt
- `output.json`：模型返回结果
- `attachment_texts.json`：按需补充的附件正文
- 扩窗相关的请求与补充消息调试文件

这些文件只用于本地调试，不属于正式主流程产物，且可能包含裁剪后的聊天上下文、prompt 与模型输出，不建议长期保留。

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

## 运行前检查

在首次运行或环境变更后，建议先确认以下项目：

- `python3 --version` 可正常返回版本信息
- `lark-cli` 可正常执行
- 当前 `lark-cli` 登录身份为目标飞书 user，而不是 bot 或未登录状态
- 本地 `.env` 或环境变量中已配置在线模型参数
- `WORKTRACE_LLM_REASONING_EFFORT=none`
- 仓库内 `data/` 目录可创建或可写

设计上，WorkTrace CLI 在正式处理前也应执行一次 preflight 检查；若关键依赖缺失、版本不满足、未登录、`no_think` 配置不满足或目录不可写，应直接失败并返回明确原因，而不是在处理中途报错。

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
