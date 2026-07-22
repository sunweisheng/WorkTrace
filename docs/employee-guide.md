# WorkTrace 员工使用说明

这份说明是写给第一次使用 WorkTrace 的员工看的，默认你不是开发者，也不需要先懂代码。

本文只介绍个人日报流程：从你自己的飞书聊天中生成当天工作事件 Markdown。管理人员合并多人已生成 Markdown 的团队汇总流程，见 [collected-people-merge-plan.md](collected-people-merge-plan.md)。

如果你只关心三件事，可以先看这三句：

1. WorkTrace 的目标是帮你整理“和你自己直接相关的工作事件”
2. 默认结果先发给你自己，不会直接发给领导
3. 系统会用到你本地配置的在线模型服务，所以请先看完下面的隐私和配置说明

## 1. 这个工具是做什么的

WorkTrace 会读取你在指定日期里发过消息或做过 reaction 的飞书工作会话，提取和你自己直接相关的工作事件，整理成一份 Markdown 文件。

这份文件里当前会保留：

- 日期
- 事件标题（显示在每条事件的三级标题中）
- 工作流（仅在能确认项目、产品或政策名称时显示）
- 主要动作
- 事件内容
- 具体对象
- 本人参与方式
- 保留理由（中文说明）
- 保留依据（来源证据）
- 涉及文件

事件标题会优先写明具体对象及关键动作、进展、结果或风险，使你不看正文也能识别具体事项。

如果事件关联了飞书文档或普通附件，标题、内容和“涉及文件”会尽量显示 `《文件名》`；可点击链接会保留链接，普通附件没有链接时会显示纯文件名。

“工作流”表示事件所属的明确项目、产品或政策，只有消息中有足够证据时才显示；系统不会猜测。“主要动作”表示方案确认、配置修改、执行验证等实际动作；“本人参与方式”表示你在事件中是发起、主责执行、协作参与、确认决策、反馈验收、被指派或参与回应。主要动作或本人参与方式没有足够证据时显示“未明确”。

系统还会在 Markdown 的隐藏注释中保存参与方式英文键，以及消息证据、同日会话证据和稳定文件标识的 SHA-256 结果，供后续多人汇总发现可能属于同一事项的候选。团队汇总还会在隐藏注释中保留来源事件 ID，不在正文重复显示。隐藏信息不保存原始消息 ID、会话 ID 或用户 ID；只有文件名而没有稳定链接或附件 ID 时不会生成文件标识。

系统只保留同时具备具体对象、保留理由和保留依据的工作事件：比如形成了结论或决策、更新了文档/数据/配置、发现或处理了问题、明确了后续待办，或推进了客户/合同/付款/交付等事项。保留依据会尽量写清来源证据，而不是只写泛泛价值判断。只写“完成审核”“完成审核工作”，或只是约下午开会、互通一下信息的普通安排，会被过滤掉。

本人确实参加了聊天，不代表每段协作都值得形成日报事件。对于没有明确工作流、没有文件、又被初步判断为后续安排的边界内容，系统会让模型再看一次对应原聊天：模型只标记“临时协作”或“实质工作”信号，并给出真实消息证据；在这项复核里，Python 不判断临时协作语义，只核对这些信号和证据，再按固定规则处理。有任何实质工作信号就保留，只有临时协作信号或仍没有有效信号时删除。复核条件和信号说明统一来自 `config/retention_policy.json`，系统不会因为增加“到工位”“帮我看一眼”等全局排除词而误删其他真实任务。

每条个人事件还会为标题、正文、主要动作、具体对象、保留依据和非空工作流保存内部事实证据。消息较多、参与人较多、事实证据不完整，或模型识别到多个对象、对比案例、多个地点、责任归属等风险时，系统会让模型重新对照原聊天确认当前事件。它可以删除或改写没有依据的地点、角色、结论和建议，但不会因为事情复杂、参与人多或步骤多就删除真实工作。Python不理解这些文字的业务含义，只核对模型是否完整返回、每项事实是否引用当前聊天的真实消息，以及修订后的正文是否被事实项完整覆盖。

复核技术失败和“没有实质工作信号”是两种情况。技术失败会停止本次生成，不会写出不完整日报；正常删除不会显示 warning。旧个人 Markdown 和已经生成的部门汇总不会被追溯修改，需要重新生成个人日报后才会使用这套规则。

你可以先自己审阅、修改，再决定是否转发给领导。

## 2. 这个工具不会默认做什么

为了减少顾虑，这些边界请你先确认：

- 不会默认直接把结果发给领导
- 不会默认自动上传到公司统一数据库
- 不会长期保存完整原始聊天记录
- 不会把所有聊天都抓进来，只处理你在当天发过消息或做过 reaction 的会话
- 不会在最终 Markdown 里显示群名、open_id、消息 ID、会话 ID 或参与人名单
- 事件正文只在责任分工、任务指派、确认沟通对象等确有必要时保留姓名

## 3. 你需要提前准备什么

首次使用前，你需要准备下面几样东西：

- 一台 Windows 或 macOS 电脑
- 已安装 Python 3.11 或更高版本
- 已安装 `lark-cli`
- 你的 `lark-cli` 已登录为飞书 `user` 身份
- 飞书 CLI 配置的机器人可向你发送文件消息
- 你自己可用的在线模型配置

当前模型连接配置必须提供下面 3 项：

```dotenv
WORKTRACE_LLM_BASE_URL=
WORKTRACE_LLM_MODEL=
WORKTRACE_LLM_API_KEY=
```

其中：

- `WORKTRACE_LLM_BASE_URL` 是模型服务地址
- `WORKTRACE_LLM_MODEL` 是模型名
- `WORKTRACE_LLM_API_KEY` 是你的密钥

`WORKTRACE_LLM_REASONING_EFFORT` 不属于缺一不可的连接配置；不填写时，代码默认使用 `none`。模板显式保留 `WORKTRACE_LLM_REASONING_EFFORT=none`，表示当前主流程关闭推理过程。如果把它改成其他值，首次自检会失败。

## 4. 隐私说明

如果你只想先看一份更短的版本，可以先看：

- [privacy-note.md](privacy-note.md)

请先明确知道当前系统会发生什么：

- WorkTrace 会通过你本机上的 `lark-cli` 读取飞书聊天
- WorkTrace 会把经过裁剪和压缩后的必要文本、会话名、发送者信息、消息和会话标识、链接 URL/标题、附件文件名发送到你配置的在线模型服务
- 为补齐 reply/quote 直接关系或模型请求的相邻上下文，系统可能临时读取并发送目标日期之外的直接关联消息，但生成的事件日期仍是目标日期
- 如果图片摘要已启用，本人发送或本人 reply/quote 直接关联的图片会按大小限制处理；其他图片受数量和大小限制，并只在模型明确请求时处理
- 模型明确请求时，指定文本附件或飞书文档正文也会进入在线模型输入
- WorkTrace 会在你本地生成 Markdown 文件
- WorkTrace 默认通过飞书机器人把生成的 Markdown 文件发给你自己

这意味着：

- 这个工具不是“完全本地、绝不外发任何内容”的方案
- 你应当确认自己配置的模型服务是否是你认可的服务
- 如果你对某个模型服务不放心，不应直接把它填进 `.env`

当前系统已经尽量减少暴露范围：

- 只处理与你直接相关的工作事项
- 默认过滤缺少具体对象、保留理由和保留依据的低价值事件
- 默认过滤部分敏感内容
- 默认强制 `/no_think`
- 消息正文中的裸链接会压缩成占位文本，但可引用链接的 URL、标题和临时引用 ID 仍会作为结构化元数据进入 prompt
- 正式主流程默认不长期保存原始聊天

## 5. Windows 安装步骤

Windows 用户请优先看这一节。

### 5.1 安装 Python

1. 打开 PowerShell
2. 输入：

```powershell
python --version
```

如果提示找不到命令，先安装 Python 3.11 或更高版本，再继续。

### 5.2 安装 lark-cli

请先确认你已经按组织要求安装了 `lark-cli`。

安装后在 PowerShell 输入：

```powershell
lark-cli --help
```

如果能正常显示帮助信息，说明这一步完成。

### 5.3 登录飞书 CLI

安装好 `lark-cli` 之后，确认当前登录的是你自己的飞书 `user` 身份，而不是 bot。

检查命令：

```powershell
lark-cli auth status
```

### 5.4 安装 WorkTrace 依赖

最简单的方式是直接运行安装脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_worktrace.ps1
```

这个脚本会尽量帮你完成下面几件事：

- 安装 Python 依赖
- 初始化 `.env`
- 检查 `lark-cli`
- 安装 WorkTrace skill

如果你的 Windows 机器不允许创建符号链接，脚本会自动尝试改用目录联接继续安装。

如果你需要手动执行，也可以进入 WorkTrace 仓库目录后，执行：

```powershell
python -m pip install -r requirements.txt
```

### 5.5 初始化本地模型配置

如果仓库里还没有 `.env`，先复制模板：

```powershell
Copy-Item .env.example .env
```

然后用文本编辑器打开 `.env`，填入你自己的模型配置。

### 5.6 安装 Skill

当前 WorkTrace 仓库根目录就是 skill 根目录。  
如果你的 Agent 使用的是 Codex 兼容 skill 目录，请把整个仓库放到对应 skill 目录，或按后续自动安装脚本完成。

## 6. macOS 安装步骤

macOS 用户流程和 Windows 基本一致，只是命令稍有不同。

### 6.1 检查 Python

```bash
python3 --version
```

### 6.2 检查 lark-cli

```bash
lark-cli --help
```

### 6.3 检查飞书登录态

```bash
lark-cli auth status
```

### 6.4 安装 Python 依赖

最简单的方式是直接运行安装脚本：

```bash
bash ./scripts/install_worktrace.sh
```

如果你需要手动执行，也可以执行：

```bash
python3 -m pip install -r requirements.txt
```

### 6.5 初始化 `.env`

```bash
cp .env.example .env
```

然后填写你自己的模型配置。

## 7. 首次使用前自检

建议首次运行前先做自检。自检应重点确认：

- Python 版本是否满足要求
- Python 依赖是否已安装
- `lark-cli` 是否已安装
- `lark-cli` 是否登录为 `user`
- 飞书 CLI 机器人是否具备向你发送文件消息的权限和可见范围
- `.env` 是否存在
- 模型配置是否完整
- `WORKTRACE_LLM_REASONING_EFFORT` 显式配置时是否为 `none`；未配置时使用代码默认值
- 在线模型是否可连通
- `data/` 目录是否可写

macOS/Linux：

```bash
python3 -m src.worktrace.cli --preflight
```

Windows：

```powershell
python -m src.worktrace.cli --preflight
```

如果自检失败，不要直接跑正式流程，先按照提示处理。

## 8. 正式运行

当前命令行运行方式：

```bash
python3 -m src.worktrace.cli --date 2026-06-23
```

Windows 如果 `python3` 不可用，可以改成：

```powershell
python -m src.worktrace.cli --date 2026-06-23
```

运行成功后：

- 本地会生成当天 Markdown 文件
- 然后系统会尝试通过飞书机器人把该文件发给你自己

如果你只是正常使用，到这里就够了。

如果上一次运行在模型调用阶段中断，且聊天输入和配置没有变化，可以续跑：

```bash
python3 -m src.worktrace.cli --date 2026-06-23 --resume
```

未完成任务的分段和提炼结果临时保存在 `data/cache/llm/YYYY/MM/YYYY-MM-DD/`。普通重跑会先删除旧日报、当天中间结果和当天个人调试目录，从头生成；`--resume` 保留这些内容，并只复用输入完全一致的中间结果。Markdown 成功写入后，中间结果自动清理。

如果你需要让技术同事帮你排查“为什么提炼成了这个事件”或“为什么几个事件被合并到一起”，可以在命令后面加上调试开关：

```bash
python3 -m src.worktrace.cli --date 2026-06-23 --debug-output
```

Windows 也可以这样执行：

```powershell
python -m src.worktrace.cli --date 2026-06-23 --debug-output
```

开启后，系统会把调试文件写到本地：

```text
data/debug/conversations/2026-06-23/
```

普通个人重跑会先删除这个日期的旧调试目录；使用 `--resume` 时保留。

其中跨会话 merge 的调试文件会放在：

```text
data/debug/conversations/2026-06-23/_merge_day_candidates/
```

这个调试根目录可能包含：

- 锚点分段输入、prompt、输出和校验结果；失败轮次保存 `failure.json`
- 分段批次输入、prompt 和候选结果；失败轮次和单片段回退分别保存在 `analysis-XX/`、`fallback-01/`
- 上下文扩展前后的片段
- 分段失败后的直接提炼结果保存在 `_anchor_fallback/`
- 跨会话 merge 和工作流归属校正结果
- `retention_review.json` 中临时协作复核每次尝试的候选摘要、模型信号、证据校验结果和 Python 统计
- `personal_fact_review.json` 中事实复核的触发原因、修订前后字段、事实证据覆盖、Python 统计和失败重试结果
- `final_events.json` 中完成文件聚合和排序后的最终事件、证据指纹、文件标识和过滤 warning
- `llm_usage.json` 中按调用类型记录的成功响应耗时、输入字符数和 provider 返回的 token

使用 `scripts/replay_day_with_trace.py` 回放时，`summary.json` 的 `review_artifact_summary` 会汇总两类复核文件是否存在、尝试次数和失败次数，`llm_usage_summary` 会按调用类型汇总次数、token 和耗时；调用输入报告也会逐次列出复核及其失败重试，并把图片摘要与文字调用分开统计。事实复核并发阶段分析实际运行耗时时看 `personal_fact_review_all` 墙钟值，各候选耗时之和只代表模型调用总负载。复核文件不额外复制整段聊天，完整上下文仍从已有分段调试输入查看。

管理人员开启多人汇总 trace 后，`source-audit.json` 会记录新旧来源文件、部分读取和过滤数量；每个 step JSON 与 prompt 在候选、复核和正文请求前保存，失败时也会生成 summary。`summary.json` 和 `summary.md` 还会记录 Python 计算的输入/输出数量、来源覆盖和高风险复核统计，便于定位失败批次、重试过程以及“哪些共同证据支持合并”或“为什么被拆开”。

请注意：这些调试文件可能包含裁剪后的聊天上下文、附件正文、图片摘要和模型输出，只建议在排障时临时开启。

## 9. 你会看到什么结果

成功后，你会得到：

- 一份本地 Markdown 文件
- 一条由飞书机器人发到你自己的文件消息

Markdown 默认只保留结构化工作事件，不会默认附带整段原始聊天。

每条事件先以三级标题显示事件标题，下面依次显示日期、主要动作、内容、具体对象、本人参与方式、保留理由、保留依据和涉及文件；有明确工作流时显示在日期后。标题不会在字段列表中重复。重新生成的新日报会自然带上增强字段，历史文件不会被批量改写。

## 10. 常见问题

### 10.1 提示缺少模型配置

说明 `.env` 里没有填完整。  
请补齐：

- `WORKTRACE_LLM_BASE_URL`
- `WORKTRACE_LLM_MODEL`
- `WORKTRACE_LLM_API_KEY`

`WORKTRACE_LLM_REASONING_EFFORT` 未配置时默认就是 `none`；如果自检单独提示 reasoning effort 不符合要求，请将它改回 `none`。

### 10.2 提示 `lark-cli` 未登录或不是 user

说明当前飞书 CLI 没有准备好。  
先完成登录，再重新运行自检。

### 10.3 成功生成了本地文件，但没发到自己

这通常说明“生成成功，发送失败”。  
这时本地文件应当还在，你可以先打开本地 Markdown 检查内容，再排查飞书机器人发消息权限、应用可见范围或 CLI 配置。

### 10.4 我担心会不会把私人聊天都读走

当前默认只处理你在目标日期里发过消息或做过 reaction 的会话，并且目标是提取与你直接相关的工作事项，不是抓取全部聊天内容。若某个会话明确不应读取，可以把会话 ID 加入 `config/conversation_blacklist.json`。

如果你想快速向同事解释当前边界，也可以直接转这份短说明：

- [privacy-note.md](privacy-note.md)

### 10.5 我担心会不会直接把结果发给领导

当前默认不会。  
当前阶段默认只会先发给你自己，由你自己决定后续是否修改或转发。
