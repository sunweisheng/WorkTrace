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
- 主要动作
- 事件内容
- 具体对象
- 本人参与方式
- 保留理由（中文说明）
- 保留依据（来源证据）
- 涉及文件

事件标题会优先写明具体对象及关键动作、进展、结果或风险，使你不看正文也能识别具体事项。

如果事件关联了飞书文档或普通附件，标题、内容和“涉及文件”会尽量显示 `《文件名》`；可点击链接会保留链接，普通附件没有链接时会显示纯文件名。

“主要动作”表示方案确认、配置修改、执行验证等实际动作，系统会把已知内部英文键转换成配置中的中文显示名；“本人参与方式”表示你在事件中是发起、主责执行、协作参与、确认决策、反馈验收、被指派或参与回应。主要动作或本人参与方式没有足够证据时显示“未明确”。

收到同事的成果、简单确认或原样再次发送，只能说明接收或转发，不能算成本人完成汇总和交付。“主要动作”和“本人参与方式”会一起核对本人实际做了什么；明确承担任务与已经完成工作分别表达。

系统还会在 Markdown 的隐藏注释中保存参与方式英文键，以及消息证据、同日会话证据和稳定文件标识的 SHA-256 结果，供后续多人汇总发现可能属于同一事项的候选。团队汇总还会在隐藏注释中保留来源事件 ID，不在正文重复显示。隐藏信息不保存原始消息 ID、会话 ID 或用户 ID；只有文件名而没有稳定链接或附件 ID 时不会生成文件标识。

系统只保留同时具备具体对象、保留理由和保留依据的工作事件：比如形成了结论或决策、更新了文档/数据/配置、发现或处理了问题、明确了后续待办，或推进了客户/合同/付款/交付等事项。保留依据会尽量写清来源证据，而不是只写泛泛价值判断。只写“完成审核”“完成审核工作”，或只是约下午开会、互通一下信息的普通安排，会被过滤掉。

会议时间、参会人和沟通渠道的确定不属于业务决策，单纯参会、调整日程、听取或查看附件、简单确认都不会单独形成事件；同一段聊天里已经形成的业务结论、具体任务、问题风险或交付变化仍会单独记录。附件存在、文件名明确或确认已查看都不能单独作为保留理由；已经明确要求提交总结、审核结论或修改结果等具体产出时，即使当天还没有反馈结果，仍可记录为后续业务动作。聊天已经说明数据核查或问题排查已经完成，并给出排查对象和结论时，即使结论正常且你随后只回复收到，也会记录这项排查事实。

本人确实参加了聊天，不代表每段协作都值得形成日报事件。对于没有文件、又被初步判断为后续安排的边界内容，系统会让模型再看一次对应原聊天：模型只标记“临时协作”或“实质工作”信号，并给出真实消息证据；在这项复核里，Python 不判断临时协作语义，只核对这些信号和证据，再按固定规则处理。有任何实质工作信号就保留，只有临时协作信号或仍没有有效信号时删除。复核条件和信号说明统一来自 `config/retention_policy.json`，系统不会因为增加“到工位”“帮我看一眼”等全局排除词而误删其他真实任务。

每条个人事件还会为标题、正文、主要动作、具体对象和保留依据保存内部事实证据。消息较多、参与人较多、事实证据不完整，或模型识别到多个对象、对比案例、多个地点、责任归属等风险时，系统会让模型重新对照原聊天确认当前事件。它可以删除或改写没有依据的地点、角色、结论和建议；只有一句话不准确时会先修订这句话，不能因此删除整条仍有证据的事件，也不会因为事情复杂、参与人多或步骤多就删除真实工作。Python 不理解这些文字的业务含义，只核对模型是否完整返回、每项事实是否引用当前聊天的真实消息，以及修订后的正文是否被事实项完整覆盖。

候选事件形成后，模型按同一实际事项做全日分组。分组提示词不发送会话 ID 和片段 ID；同一会话不同事项、同类工作和宽泛目标都不能单独支持合并。每个多事件组必须写明具体共同对象，并逐条说明每个候选与该对象的直接关系，引用该候选自己的来源消息。Python 检查候选完整、成员说明一一对应和证据归属。

初步分组后，系统会把当天全部组的编号和标题统一检查一次，寻找可能漏合并的组；一个初步组有多个成员时，检查标题会包含这些成员各自的标题，避免只看主标题漏掉关键事项。这一步不发送日期、正文、聊天消息、附件、人员信息或其他分组阶段的案例，也不会直接合并事件。模型必须把每个标题与其余标题比较，并逐个交代是否可能有关，不能只返回少数候选而跳过其余组；系统会检查有没有遗漏、重复或引用不存在的编号，再把互相交叉的关系组成检查范围。这套检查不针对某天、某人或某类业务设置关键词，也不依赖固定长度的相同文字。共享来源片段、直接 reply/quote、共同消息、共同稳定文件，以及名称相同但版本号不同的非图片附件，也会形成复核线索。文件名比较会保留扩展名、月份、年份和业务编号，只忽略配置中的版本后缀。标题相似或文件名称相同都不代表一定合并，系统还要结合完整内容重新安排检查范围内的候选；初步组可以拆开，也可以与其他候选重新组合。系统还会检查“关系已合并”的成员是否真的出现在同一个最终事件里。最终分组确定后，系统会逐项重新生成标题、正文和具体对象；只有一个候选或某个最终组只有一项时也会执行。某项生成失败只回退该项原内容并显示 warning，不影响其他事件继续生成。

复核技术失败和“没有实质工作信号”是两种情况。临时协作和事实复核技术失败会停止本次生成，不会写出不完整日报；标题发现经过重试仍失败时会放弃这项辅助检查并显示 warning，但仍继续生成和发送个人日报。正常删除不会显示 warning。旧个人 Markdown 和已经生成的部门汇总不会被追溯修改，需要重新生成个人日报后才会使用这套规则。

你可以先自己审阅、修改，再决定是否转发给领导。

## 2. 这个工具不会默认做什么

为了减少顾虑，这些边界请你先确认：

- 不会默认直接把结果发给领导
- 不会默认自动上传到公司统一数据库
- 正式任务成功写入后清理模型临时缓存；失败或中断时会保留聊天上下文供续跑，原始调试目录也需在不再需要时清理
- 不会把所有聊天都抓进来，只处理你在当天发过消息或做过 reaction 的会话
- 不会在最终 Markdown 里显示群名、open_id、消息 ID、会话 ID 或参与人名单
- 事件正文只在责任分工、任务指派、确认沟通对象等确有必要时保留姓名

## 模型调用模式

在 `.env` 设置 `WORKTRACE_LLM_MODE`，进程环境变量优先于 `.env`。
未配置时使用 `codex_with_fallback`；空值或其他值会明确报错。

| 配置值 | 首次调用和重试 | 重试耗尽后 | 必需配置 |
| --- | --- | --- | --- |
| `codex_with_fallback`（默认） | Codex | 按现有规则调用 Online 一次 | Codex 命令、登录与七项 `WORKTRACE_CODEX_*`；Online 可选 |
| `online_only` | Online | 不追加备用请求 | `WORKTRACE_LLM_BASE_URL`、`WORKTRACE_LLM_MODEL`、`WORKTRACE_LLM_API_KEY` |

仅 Online 模式无需安装、登录或配置 Codex；已有 Codex 配置也不会参与检查。
个人日报、多人汇总、事实复核、图片摘要、诊断报告和回放使用同一模式。
Online 继续读取现有协议、流式、TLS 和超时配置，推理设置须为 `none`。
启动检查会验证 Online 的 Function Calling 格式，跳过全部 Codex 检查。

重试次数继续读取 `config/llm_retry.json`：技术请求默认重试 1 次，话题切分和
事件提炼的阶段重试各 3 次，全日分组校验重试 1 次。仅 Online 模式全部
请求和重试使用同一 Online 线路，备用切换次数为 0；不增加 SDK 隐藏重试。
仅 Online 的鉴权、权限、TLS 和配置错误不重试。默认模式请求层也不重试
这些错误，但部分个人阶段仍可能按原有规则重试；具体说明见 README。
图片失败仍只跳过该图并告警；诊断模型
失败时仍生成通过隐私检查的基础报告，报告显示 Codex“未启用”。

仅 Online 使用 `config/model_input_budget.json` 的 `default_target_tokens`
（当前 7000），不匹配 Codex 与 Online 组合的预算。临时模型结果按模式区分，
切换模式或读取旧缓存时重新请求，`--resume` 不会跨模式复用模型结果。

## 3. 你需要提前准备什么

首次使用前，你需要准备下面几样东西：

- 一台 Windows 或 macOS 电脑
- 已安装 Python 3.11 或更高版本
- 已安装 `lark-cli`
- 你的 `lark-cli` 已登录为飞书 `user` 身份
- 默认双线路模式：已安装并登录 `codex` 命令；仅 Online 模式无需 Codex
- 飞书 CLI 配置的机器人可向你发送文件消息
- 你自己可用的在线模型配置

默认双线路模式必须在仓库本地 `.env` 填写下面七项，认证由已登录的
Codex CLI 保存；这些值不从进程环境或个人 Codex 模型配置继承：

```dotenv
WORKTRACE_CODEX_MODEL=your-codex-model-name
WORKTRACE_CODEX_REASONING_EFFORT=your-model-supported-effort
WORKTRACE_CODEX_PROVIDER_ID=your-relay-id
WORKTRACE_CODEX_PROVIDER_NAME=your-relay-name
WORKTRACE_CODEX_PROVIDER_BASE_URL=https://your-relay.example/v1
WORKTRACE_CODEX_PROVIDER_WIRE_API=responses
WORKTRACE_CODEX_PROVIDER_REQUIRES_OPENAI_AUTH=true
```

仅 Online 模式跳过上述配置，必须提供下面 3 项；默认双线路模式使用它们配置可选备用：

```dotenv
WORKTRACE_LLM_BASE_URL=
WORKTRACE_LLM_MODEL=
WORKTRACE_LLM_API_KEY=
WORKTRACE_LLM_WIRE_API=responses
```

其中：

- `WORKTRACE_LLM_BASE_URL` 是模型服务地址
- `WORKTRACE_LLM_MODEL` 是模型名
- `WORKTRACE_LLM_API_KEY` 是你的密钥
- `WORKTRACE_LLM_WIRE_API` 是 Online 接口，默认 `responses`；服务明确要求 Chat Completions 时才改为 `chat_completions`

`WORKTRACE_LLM_REASONING_EFFORT` 不属于缺一不可的连接配置；不填写时，代码默认使用 `none`。模板显式保留 `WORKTRACE_LLM_REASONING_EFFORT=none`，此时请求会发送关闭思考的字段。改成其他值时，仅 Online 自检失败；默认双线路自检显示 Online 备用已禁用并提示原因，但当前后续代码仍可能创建该备用，也不会发送关闭思考的字段。请保持 `none`，不能把这项自检状态理解为运行中已经禁用备用。

如需只生成本地文件、不发送给自己，把 `config/self_delivery.json` 改为：

```json
{"enabled": false}
```

默认值是 `{"enabled": true}`。该开关同时适用于个人日报和多人汇总；关闭后 CLI JSON 的 `self_delivery_status` 会显示为 `disabled`，不代表生成失败。

## 4. 隐私说明

如果你只想先看一份更短的版本，可以先看：

- [privacy-note.md](privacy-note.md)

请先明确知道当前系统会发生什么：

- WorkTrace 会通过你本机上的 `lark-cli` 读取飞书聊天
- WorkTrace 会把经过裁剪和压缩后的必要文本、会话名、发送者信息、消息和会话标识、链接 URL/标题、附件文件名发送到配置选择的模型服务；默认先调用 Codex，失败后按规则调用 Online 备用，仅 Online 模式直接发送到 Online 服务
- 为补齐 reply/quote 直接关系或模型请求的相邻上下文，系统可能临时读取并发送目标日期之外的直接关联消息，但生成的事件日期仍是目标日期
- 如果图片摘要已启用，本人发送或本人 reply/quote 直接关联的图片会按大小限制处理；其他图片受数量和大小限制，并只在模型明确请求时处理
- 模型明确请求时，指定文本附件或飞书文档正文也会进入上述模型线路输入
- 自动补全文档标题也可能在本机读取完整文档，正文只有在补读请求中才提供给提炼模型
- 会话黑名单会过滤搜索命中，阻止后续拉取和模型处理；飞书搜索服务仍可能先返回该会话的命中
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
- Online 强制 `/no_think`；Codex 主线路不追加该文本，而是使用完整严格契约
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

Windows 会自动从 `PATH` 定位 `lark-cli` 和启用时的 `codex`。`lark-cli.cmd`、`codex.cmd` 等命令脚本通过系统命令解释器启动，`codex.exe` 等原生程序直接启动；包含空格或中文的文件路径仍按原参数传递，输出统一按 UTF-8 读取。

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

建议首次运行前先做自检。命令会自动检查：

- Python 版本是否满足要求
- `lark-cli` 是否已安装
- `lark-cli` 是否登录为 `user`
- 默认双线路模式：`codex` 命令是否可用
- 默认双线路模式：仓库本地 `.env` 是否显式配置 `WORKTRACE_CODEX_MODEL`、`WORKTRACE_CODEX_REASONING_EFFORT` 和全部 `WORKTRACE_CODEX_PROVIDER_*` 项
- 默认双线路模式：使用正式 Schema 的 Codex 小探针是否成功
- 默认模式：Online 备用连接配置是否合法，缺少时只禁用备用
- 仅 Online 模式：必需连接配置和 Function Calling 探针是否通过，不检查 Codex
- `data/` 目录是否可写
- `Asia/Shanghai` 时区是否可用

Python 依赖中的 `tzdata` 会在 Windows 没有系统时区数据库时提供后备，不改变日报日期判断。

自检不会发送测试文件，因此飞书机器人的发文件权限和应用可见范围仍需在首次正式运行时验证。投递失败不会删除已经生成的本地 Markdown。

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
- 自送达开启时，系统会通过飞书机器人把该文件发给你自己

如果你只是正常使用，到这里就够了。

如果上一次运行在模型调用阶段中断，且聊天输入、模式、模型和规则没有变化，可以续跑：

```bash
python3 -m src.worktrace.cli --date 2026-06-23 --resume
```

未完成任务的分段和提炼结果临时保存在 `data/cache/llm/YYYY/MM/YYYY-MM-DD/`。普通重跑在自检通过后先删除旧日报、当天中间结果和当天个人调试目录，从头生成；`--resume` 保留这些内容，只核对序列化窗口/批次和模式是否一致。它不会自动识别模型名、完整提示词或全部规则的变化，改过这些设置后应普通重跑。Markdown 成功写入后中间结果自动清理；失败或中断留下的缓存可能含裁剪后的聊天，不再续跑时应清理。

如果遇到无法启动、运行失败、速度太慢、模型反复重试、事件遗漏、错误合并、文件未生成或结果未送达，直接在 Codex 中说：

- `用调试模式重新跑 2026-06-23 的个人日报，并生成可以发给维护人员的诊断报告`
- `2026-06-23 的多人汇总结果不对，帮我用调试模式排查并生成诊断报告`

Codex 会自动执行正确命令并等待结束。自送达开启时，调试可能重新生成日报并再次发送给你自己，结束后还会增加一次独立的大模型调用，用来整理已经由 Python 脱敏和计算好的诊断事实。

开发者需要直接执行时，个人日报命令是：

```bash
python3 -m src.worktrace.cli --date 2026-06-23 --debug-output
```

Windows 也可以这样执行：

```powershell
python -m src.worktrace.cli --date 2026-06-23 --debug-output
```

多人汇总命令是：

```bash
python3 -m src.worktrace.cli --debug-output merge-collected --date YYYY-MM-DD
```

运行结束后，查看 CLI JSON 中的 `support_report`：

- `generated_with_llm`：可以只发送 `support_report.path` 指向的 Markdown
- `generated_after_llm_failure`：基础报告可以发送，但大模型分析没有完成
- `blocked`：隐私检查没有通过，不要发送任何原始调试文件
- `failed`：报告没有生成成功，也不要把原始 trace 当作诊断报告发送

基础报告中的“诊断整理执行情况”会说明分析是调用失败、返回格式无效，还是结论与已统计的事实冲突。诊断整理的调用数量和耗时单独记录，日报业务统计不包含这些后续调用；没有采集到请求记录时显示“未采集”。

安全报告是单个 `data/debug/support_reports/worktrace-support-<随机编号>.md`。WorkTrace 不会自动上传或发送它，也不会生成 ZIP。只把这一份 Markdown 发给维护人员，绝不能发送完整 `data/debug` 目录。

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
- `_merge_day_candidates/input.json`、`prompt.txt` 和 `grouping_attempts.json` 中的全日分组输入、尝试和 Python 校验结果
- `day_group_discovery.json` 中协议版本、全部标题输入、逐组检查、Python 形成的候选以及重试或放弃原因；旧记录没有逐组检查时显示不可用
- `day_group_review.json` 中结构关系、同一基础文件名和标题发现形成的复核范围、必须覆盖的候选编号、解析前模型返回、局部复核尝试和保留决定
- `personal_group_render.json` 中全部最终个人事件组的成员、内容重写尝试、失败回退和最终文字
- `day_group_review_replay.json` 中失败范围单独重放时的线路、耗时、上次校验错误、返回结果或放弃原因
- `resolved_groups.json` 中的最终稳定分组、warning 和 `day_grouping_summary`
- `retention_review.json` 中临时协作复核每次尝试的候选摘要、模型信号、证据校验结果和 Python 统计
- `personal_fact_review.json` 中事实复核的触发原因、修订前后字段、事实证据覆盖、Python 统计和失败重试结果
- `final_events.json` 中完成文件聚合和排序后的最终事件、证据指纹、文件标识和过滤 warning
- `llm_calls.json` 中按调用编号记录的严格契约、最终提示词、线路、模型、推理强度、成功或失败、切换原因、原始结果和 Python 校验；不保存密钥、认证文件、个人 Codex 配置、完整环境变量、图片内容或 Codex JSONL
- `llm_usage.json` 中按调用类型汇总耗时、输入字符数和 provider 返回的 token；确定性修复单个 Function 参数逗号时还会保存修复诊断

从仓库根目录执行 `python3 -m scripts.replay_day_with_trace --date YYYY-MM-DD` 回放时，`summary.json` 的 `review_artifact_summary` 会汇总两类事实复核文件，`day_grouping_artifact_summary` 和 `day_grouping_summary` 会汇总全日初始分组、标题发现、完整内容复核、最终个人事件统一写作和失败范围重放，`llm_usage_summary` 会按调用类型汇总次数、token 和耗时。完整内容复核会分别显示 Python 处理过的结果尝试数和实际模型请求数，后者包含所选主线路的技术重试及可用的备用请求。调用输入报告会逐次列出标题发现、完整内容复核、最终个人事件统一写作、失败范围重放及其重试，并显示标题发现的输入字符、Online/Codex 估算、实际 token、超限状态和逐组检查覆盖。分析实际运行耗时时，标题发现看 `day_group_discovery_all`，事实复核看 `personal_fact_review_all`，完整内容复核看 `day_group_review_all`，最终个人事件统一写作看 `personal_group_render_all`，整个分组阶段看 `merge_day_candidates`；各候选或请求耗时之和只代表模型调用总负载。旧 trace 缺少标题发现文件时明确显示节点不可用；有旧标题发现文件但没有 `group_checks` 时显示逐组检查不可用，不补造数据。

如果整日回放已经完成，但 `day_group_review.json` 显示某个完整内容复核范围最终失败，可运行 `python3 -m scripts.replay_failed_day_group_reviews --date YYYY-MM-DD` 只重新请求失败范围。该脚本使用已有调试输入和最后一次具体错误，不重新拉取聊天，也不直接修改个人 MD；全部线路仍失败时写明放弃原因并结束，不阻碍已经完成的日报。随后重新运行调用输入报告时，失败范围重放会单独列出。

需要比较新旧分组时，先保存旧 trace，再运行 `scripts/report_event_grouping_comparison.py`。脚本统计候选覆盖、标题发现候选及其完整复核结果、单例/多事件组、发生合并或拆分的候选、理由和证据，不代替人工判断事件是否真的应当合并。

管理人员汇总时，最终单来源组和多来源组都会逐事件重新生成标题、正文和具体对象，最多同时处理 3 项。某项正文生成失败时只回退该项已经脱敏的来源内容并显示 warning，不影响其他团队事件和汇总文件写入。开启多人汇总 trace 后，`source-audit.json` 会记录新旧来源文件、部分读取和过滤数量；`collected_group_discovery.json` 记录全部初步组标题和标题候选，`collected_group_review.json` 记录不可拆重复来源块、检查范围、关系处理、跨组合并和初步组拆分。每个 step JSON 与 prompt 在候选、复核和正文请求前保存，失败时也会生成 summary。`summary.json` 和 `summary.md` 还会记录 Python 计算的输入/输出数量、来源覆盖、标题发现、完整复核和内容重写统计，便于定位失败批次、重试过程以及“哪些共同证据支持合并”或“为什么被拆开”。

请注意：这些原始调试文件可能包含裁剪后的聊天上下文、附件正文、图片摘要、prompt 和模型输出，只建议在排障时临时开启，不能外发。报告模型不会读取这些原始内容；它只读取 Python 生成的编号状态、数量、耗时、token、重试、备用线路和送达结果。最终报告通过配置正则扫描路径、内部 ID、网址、联系方式、日期、文件名和密钥样式，未通过时不会保留文件。扫描器不识别人名语义；隐私保护还依赖不把原始业务信息交给报告模型，并将模型返回限制为编号事实和允许值。

## 9. 你会看到什么结果

成功后，你会得到：

- 一份本地 Markdown 文件
- 自送达开启时，一条由飞书机器人发到你自己的文件消息

Markdown 默认只保留结构化工作事件，不会默认附带整段原始聊天。

每条事件先以三级标题显示事件标题，下面依次显示日期、主要动作、内容、具体对象、本人参与方式、保留理由、保留依据和涉及文件。标题不会在字段列表中重复。重新生成的新日报会自然带上增强字段，历史文件不会被批量改写。

## 10. 常见问题

### 10.1 提示缺少模型配置

先看当前使用的模式和错误提示中的配置名：

- 默认 `codex_with_fallback`：补齐第 3 节的七项 `WORKTRACE_CODEX_*`，并确认 Codex 已登录。缺少 Online 配置只会禁用备用。
- `online_only`：补齐下面三项，可以用进程环境覆盖 `.env`：

- `WORKTRACE_LLM_BASE_URL`
- `WORKTRACE_LLM_MODEL`
- `WORKTRACE_LLM_API_KEY`

`WORKTRACE_LLM_REASONING_EFFORT` 未配置时默认就是 `none`；如果自检单独提示 reasoning effort 不符合要求，请将它改回 `none`。

### 10.2 提示 `lark-cli` 未登录或不是 user

说明当前飞书 CLI 没有准备好。  
先完成登录，再重新运行自检。

### 10.3 成功生成了本地文件，但没发到自己

先查看 CLI JSON 的 `self_delivery_status`。`disabled` 表示自送达已关闭；
`failed` 才表示发送失败。这两种情况都会保留本地 Markdown，可以先打开
检查内容。发送失败时再排查飞书机器人权限、应用可见范围或 CLI 配置。

### 10.4 我担心会不会把私人聊天都读走

当前默认只处理你在目标日期里发过消息或做过 reaction 的会话，并且目标是提取与你直接相关的工作事项，不是抓取全部聊天内容。若某个会话明确不应读取，先把 `config/conversation_blacklist.example.json` 复制为本地 `config/conversation_blacklist.json`，再加入会话 ID。实际黑名单只保存在本机，不纳入 Git 管理。

如果你想快速向同事解释当前边界，也可以直接转这份短说明：

- [privacy-note.md](privacy-note.md)

### 10.5 我担心会不会直接把结果发给领导

当前默认不会。  
当前阶段默认只会先发给你自己，由你自己决定后续是否修改或转发。

## Windows 新安装与自检

复制 Skill 不等于安装运行依赖。安装脚本检查 Python 3.11+ 和 pip
退出码，直接依赖包含 httpx；使用 CODEX_HOME，保留已有 `.env`。
详细步骤见 [Windows 使用说明](windows-guide.md)，包含中文空格路径、
官方飞书 CLI 初始化与授权、PATH 更新和 PowerShell 5.1/7 编码说明。

`python -m src.worktrace.cli import-codex-config` 默认只预览，
`--apply` 仅填空缺，不覆盖已有 high；认证转换需要显式
`--apply --import-auth`，通过 stdin 导入且保留已有凭据。
`--preflight-full` 实际验证各配置后端的 Function Calling，普通
自检的 online_fallback=available 仅表示配置存在。tls_verify 说明
Online 实际证书校验开关，关闭状态不能认定为证书可信性已验收。

正常运行的中文进度在 UTF-8 stderr，最终 JSON 在 stdout；新 warnings
说明告警代码、阶段和影响，stage_timing_summary 提供普通耗时摘要。
旧 error_summary 与 warning_messages 保留。success_with_warnings
不表示整个任务失败；片段被跳过时报告可能遗漏事项，不能宣称完整。
