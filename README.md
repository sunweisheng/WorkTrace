# WorkTrace

WorkTrace 是一个面向普通员工的个人工作事件整理工具。它的目标不是监控个人隐私，而是帮助员工把“今天和自己直接相关的工作沟通”整理成一份可以自己先审阅、再决定是否转发的每日 Markdown 记录，减少过几天或过几周后回忆不清当时发生了什么的问题。

当前阶段，WorkTrace 主要服务使用 `Codex` 或兼容 `Codex Skill` 的 Agent 的员工用户。个人日报模式的输入来源默认是飞书聊天，输出是一份当天的本地 Markdown 文件，并通过飞书 CLI 机器人身份发给员工自己。另有管理人员汇总模式，可把多人已经生成的 WorkTrace Markdown 放入 `merge_inbox/YYYY/MM/DD/` 后生成团队汇总 `YYYY-MM-DD-登录人姓名-merged.md`；日期目录下的一级子目录会作为独立合并范围，各自生成自己的团队汇总文件，并同样通过飞书机器人发给当前登录人自己。若同一真实事项中包含“当前登录用户自己的个人事件 MD”来源，则团队汇总会以该来源为主，其它来源仅作不冲突补充。

## 快速使用说明

员工每天不需要手工整理聊天内容。输入就是“目标日期内你自己发过消息、且和你自己直接相关的飞书工作对话”。

- 你可以直接对 Agent 说：`帮我生成 2026-07-06 的个人事件MD`
- 也可以说：`跑一下 2026-07-06 的 WorkTrace 个人日报`
- 执行后，你会得到本地个人文件 `YYYY-MM-DD-姓名.md`，同时飞书机器人会把这份文件发给你自己

管理人员先收集团队成员当天已经生成的个人 MD，放进 `merge_inbox/YYYY/MM/DD/`；如果要按项目或小组分开汇总，就放在日期目录下的一级子目录里。

- 你可以直接对 Agent 说：`帮我合并 2026-07-06 的部门事件MD`
- 也可以说：`把 merge_inbox 里 2026-07-06 的多人日报合并成部门事件MD`
- 执行后，你会得到规范化的团队汇总文件 `YYYY-MM-DD-登录人姓名-merged.md`，同时飞书机器人会把每个生成的汇总文件发给你自己

仓库根目录同时作为 Codex skill 根目录使用，安装时应链接整个仓库目录，而不是单独链接某个 `skill/` 子目录。

## 项目目标

当前阶段的目标是：

- 帮员工提取目标日期内与自己直接相关的工作事件
- 支持管理人员对已收集的多人 WorkTrace Markdown 做团队事项汇总
- 自动过滤明显无关内容与部分敏感内容
- 输出只适合转发和回顾的结构化 Markdown
- 默认把结果先发给员工自己，而不是直接发给领导
- 从第一天起就让输出格式兼容后续人工逐级汇总

当前阶段不负责：

- 自动从员工原始聊天直接给领导、部门或公司做汇总
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
- 可通过配置让提示词避开薪资、绩效、争吵、辱骂等敏感内容
- 最终输出默认不显示人名、群名、open_id、消息 ID、会话 ID、发送时间点
- 正式主流程默认不长期保存原始聊天内容
- 长期保留的主产物是结构化 Markdown，而不是原始聊天记录
- 生成成功后默认只通过飞书 CLI 机器人身份发给员工自己

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
- 首轮 / 扩窗 prompt 携带 reply / quote 关系摘要
- 会话内扩窗重跑，以及附件正文 / 飞书文档正文按需补充
- 同日全量候选事件的跨会话合并
- 同日重跑覆盖
- 管理人员收集多人 Markdown 后的同日团队汇总合并，支持日期根目录和一级子目录分别汇总
- 默认通过内置 Online analyzer 调用兼容 OpenAI `Responses API` 的在线模型服务
- 本地 `.env` / 环境变量私有配置在线模型参数，不随 git 提交
- 默认只保留与本人直接相关的事项
- 只保留具备“具体对象、保留理由、保留依据”的工作相关结果，零风险无效消息和低沉淀价值事件由 Python 过滤
- 默认不提炼咨询类事件、流程审核类事件、团建活动组织类事件、技能培训类事件，这类事项不作为公司级长期事件沉淀重点
- 最终直接输出事件清单，不再生成管理者总结
- 默认通过飞书 CLI 机器人身份将当天 Markdown 文件发给当前登录用户自己
- 管理汇总模式默认把每个汇总 Markdown 通过飞书 CLI 机器人身份发给当前登录用户自己

## 当前主流程

当前个人日报主流程如下：

1. 拉取目标日期内本人参与且本人当日发过消息的飞书会话
2. Python 过滤明显无效消息
3. 按会话构造 `ConversationSlice`
4. `1 个会话 = 1 次首轮 LLM`，首轮会发送当前 `ConversationSlice` 内全部消息，并携带 reply / quote 关系摘要、附件元信息、链接元信息，只提炼该会话中与本人直接相关的 `candidate_events`
5. 若首轮返回 `context_requests`，则在同一会话内自动补充更早消息、更晚消息、附件正文或飞书文档 / wiki 正文，并重跑直到收敛、无新信息或达到重跑上限
6. 汇总全日所有会话的 `candidate_events`
7. Python 按结构化保留门槛过滤低价值候选事件
8. 一次性调用 `merge_day_candidates(...)`，对全日候选事件做跨会话分组
9. Python 根据分组结果物化 `MergedEventDraft`，并再次校验保留门槛
10. Python 构建最终 `WorkEvent`
11. Python 聚合每个最终事件对应的显式文档链接
12. 将事件列表写入当天 Markdown 文件，文件名为 `YYYY-MM-DD-姓名.md`
13. 通过飞书 CLI 机器人身份将当天 Markdown 文件发送给当前登录用户自己

管理人员汇总流程如下：

1. 管理人员把多人已经生成的 WorkTrace Markdown 放入 `merge_inbox/YYYY/MM/DD/`
2. 来源文件名只要能识别出日期和姓名成分即可，例如 `YYYY-MM-DD-姓名.md`、`姓名-YYYY-MM-DD.md`、`姓名_YYYY-MM-DD.md`
3. 日期根目录始终作为一个合并范围；日期目录下每个一级子目录也作为独立合并范围
4. Python 读取每个合并范围当前层的标准 WorkTrace Markdown 事件块，不递归更深层目录
5. Python 先按同一套结构化保留门槛过滤来源事件
6. 相同 `event_id` 且标题/内容满足确定性规则时先锁定为合并组
7. 若来源文件名中的姓名与当前登录用户名精确匹配，则该来源会被标记为“合并人来源”；若当前目录没有匹配到，则写 warning 并回退为普通多人合并
8. 其它事件交给 LLM 保守判断是否属于同一真实工作事件；若某个合并组包含“合并人来源”，则最终标题、内容、具体对象、保留理由、保留依据都以该来源为主，其它来源只补充不冲突的信息
9. Python 校验和修复 LLM 返回的分组，保留来源人员和来源事件 ID，并在写入前再次校验保留门槛
10. 每个合并范围生成规范化汇总文件 `YYYY-MM-DD-登录人姓名-merged.md`
11. 通过飞书 CLI 机器人身份将每个汇总 Markdown 文件发送给当前登录用户自己

## 输出原则

当前阶段最终面向员工的个人 Markdown，只保留下面这些公共工作信息：

- 日期
- 事件标题
- 事件内容
- 具体对象
- 保留理由（面向阅读的中文说明；内部仍保留 `retention_reason` 枚举）
- 保留依据（来源证据，说明来自哪个会话、谁发起或确认、关键动作或结论）
- 涉及文件

其中“涉及文件”的用途是给员工未来回忆当时事件细节时使用，不是给 LLM 做复杂推理用的。真实链接应该尽量留在最终 Markdown 里给人看，而不是在 prompt 里展开给模型推理。

管理人员汇总产物 `YYYY-MM-DD-登录人姓名-merged.md` 例外：为了团队事项审阅和追溯来源，会额外显示 `来源人员` 和 `来源事件 ID`。

“有效工作事件”必须同时具备具体对象、保留理由和保留依据：例如产出或修改了文档、数据、配置、代码，形成了结论或决策，发现或处理了问题/风险，明确了待办、负责人、期限，推进了客户、合同、付款、交付等外部业务，或审核/审批有明确对象和结论。保留依据应写清来源证据，而不是泛泛价值判断。仅写“完成审核”“完成审核工作”，或只是“下午开会互通一下信息”的普通安排，不应进入个人日报，也不会进入管理人员汇总。

## LLM 调用原则

当前主流程中的 LLM 调用默认且强制使用两层约束：

- prompt 末尾统一追加 `/no_think`
- 请求体里显式要求关闭推理过程，默认配置 `WORKTRACE_LLM_REASONING_EFFORT=none`

这意味着当前链路更偏向“直接抽取、直接归并、直接结构化输出”，而不是依赖模型展示推理过程。

同时，当前 prompt 中对链接的表达也应尽量弱化：

- 可以保留 `[飞书文档]`
- 可以保留 `[飞书文档: 标题]`
- 不应把完整 URL 列表直接展开给模型

当前首轮 / 扩窗 prompt 还有两条额外约束：

- 不再按消息条数把当前 slice / anchor unit 截成前 40 条；凡是已进入当前分析窗口的消息都会发送给 LLM
- 若当前消息是在纠正或替换前文对象，模型必须优先采用当前消息确认后的对象；被 reply / quote 的内容只能作为背景

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
- 个人日报 CLI 固定为 `python -m src.worktrace.cli --date YYYY-MM-DD`，`stdout` 返回 machine-readable JSON 执行摘要。
- 管理汇总 CLI 固定为 `python -m src.worktrace.cli merge-collected --date YYYY-MM-DD`，输入来自 `merge_inbox/YYYY/MM/DD/` 及其一级子目录，每个合并范围生成本目录 `YYYY-MM-DD-登录人姓名-merged.md`。
- 如需排查真实运行中的提炼和 merge 行为，可追加 `--debug-output`，把调试文件写入本地目录。
- `event_id` 由 Python 基于目标日期内归一化后的来源消息集合稳定生成，但不应作为员工最终看到的主要字段。
- 正式执行默认不长期落盘原始聊天内容，最终只保留结构化事件清单。
- 存储目标为按年月目录组织的每日 Markdown 文件，个人日报文件名为 `YYYY-MM-DD-姓名.md`。

## 依赖说明

当前依赖如下：

- `python3`
- `lark-cli`
- `openai` Python SDK
- 一个兼容 OpenAI `Responses API` 的在线模型服务
- 已登录且具备消息读取权限的飞书 user 身份
- 已配置可向当前用户发送消息的飞书 CLI 机器人身份

当前环境默认的 Codex skill 安装目录为 `~/.codex/skills`。

## 环境要求

建议至少满足以下环境要求：

- `python3` 已安装，且版本满足项目要求
- `lark-cli` 已安装，并可在 `PATH` 中直接调用
- `lark-cli` 已登录为具备消息读取权限的飞书 user 身份
- `lark-cli` 机器人身份具备向当前用户发送文件消息的权限和可见范围
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

事件是否值得保留，主要还是靠结构化字段和 Python 门槛判断，不靠维护一大堆关键词。

`event_rules.json` 的用途只有两个：

- 给 LLM 提示词补充“哪些敏感话题不要提炼”
- 用少量精确规则，直接排除你已经确认不该进入结果的事件

如果要教别人用，最简单的理解就是：

- `confidential_event_keywords`：只用于提示词提醒模型回避这类工作敏感话题
- `non_work_sensitive_keywords`：只用于提示词提醒模型回避这类非工作敏感内容
- `excluded_event_topics`：按事件标题精确排除
- `excluded_event_content_signatures`：按事件内容“包含某段特征文本”排除

对应规则文件是 [config/event_rules.json](/Users/sunweisheng/Documents/GitHub/WorkTrace/config/event_rules.json)：

```json
{
  "confidential_event_keywords": ["工资", "薪资", "劳动仲裁", "绩效"],
  "non_work_sensitive_keywords": ["吵架", "辱骂"],
  "excluded_event_topics": ["代码同步"],
  "excluded_event_content_signatures": ["劳动仲裁", "绩效", "git pull"]
}
```

这 4 个字段的实际效果如下：

- `confidential_event_keywords`
  只进提示词，不做 Python 最终关键词过滤。
  适合放：工资、薪资、绩效、劳动仲裁这类你希望模型默认不要提炼的工作敏感信息。
- `non_work_sensitive_keywords`
  只进提示词，不做 Python 最终关键词过滤。
  适合放：吵架、辱骂、调情这类明显不该进入工作事件的非工作敏感内容。
- `excluded_event_topics`
  按标题精确匹配。
  只有事件标题清楚且稳定时才适合放这里，例如固定会被提炼成 `代码同步` 的噪音事件。
- `excluded_event_content_signatures`
  按内容包含匹配。
  适合放标题不稳定、但正文里总会出现固定特征词的事件，例如 `劳动仲裁`、`绩效`、`git pull`。

再直白一点：

- 想“提醒模型少碰某类敏感话题”，放 `confidential_event_keywords` 或 `non_work_sensitive_keywords`
- 想“无论模型怎么写，只要命中就直接排除”，放 `excluded_event_topics` 或 `excluded_event_content_signatures`

几个常见例子：

- 例 1：`劳动仲裁取数跟进`
  标题可能叫“劳动仲裁取数跟进”“仲裁数据整理”“仲裁取数协调”，但正文通常会出现 `劳动仲裁`，更适合放进 `excluded_event_content_signatures`
- 例 2：`绩效版本最终版确认`
  标题写法可能变化，但正文大概率会出现 `绩效`，适合放进 `excluded_event_content_signatures`
- 例 3：`代码同步`
  如果这类噪音标题很固定，直接放进 `excluded_event_topics` 更干脆

使用原则：

- 不要把 `excluded_event_topics` / `excluded_event_content_signatures` 当成“大而全的事件价值判断系统”
- 这两个字段更适合做“少量精确拉黑”
- 如果只是希望模型更保守，不一定要强制排除，优先放 `confidential_event_keywords` 或 `non_work_sensitive_keywords`

当前代码语义是：

- `confidential_event_keywords` 和 `non_work_sensitive_keywords` 只用于生成提示词约束
- `excluded_event_topics` 是标题精确匹配
- `excluded_event_content_signatures` 是内容包含匹配
- `excluded_event_topics` 和 `excluded_event_content_signatures` 都会在候选事件阶段、以及合并后事件阶段各检查一次

如果需要彻底排除某些会话，不让它们进入消息收集链路，请单独维护 [config/conversation_blacklist.json](/Users/sunweisheng/Documents/GitHub/WorkTrace/config/conversation_blacklist.json)。

这份配置和 `event_rules.json` 的区别很简单：

- `event_rules.json` 是“会话已经进来了，再决定哪些事件不要保留”
- `conversation_blacklist.json` 是“这个群或会话一开始就不要进来”

如果你要教别人用，可以直接这么说：

- 某个群长期和自己工作事件无关，直接拉黑这个群 ID
- 某个群虽然偶尔有工作信息，但出于隐私或噪音考虑，明确不希望 WorkTrace 读取，也直接拉黑这个群 ID
- 只要进了这个黑名单，WorkTrace 连这个群当天的消息都不会收

配置格式如下：

```json
{
  "excluded_conversation_ids": [
    "oc_be07388984c344d1b2d68c4a92b74c81",
    "oc_0021bbab18a1c2311f76ea72f23cbe18"
  ]
}
```

字段含义只有一个：

- `excluded_conversation_ids`
  要彻底排除的飞书会话 ID 列表。支持放多个，系统会自动去重。

命中黑名单后的效果也很明确：

- 会在“目标会话发现”阶段直接跳过
- 不会继续进入消息拉取
- 不会进入会话切片
- 不会进入 LLM 提炼
- 不会进入后续跨会话合并

适用场景：

- 明确不想收的群
- 长期噪音群
- 敏感群
- 和个人日报目标无关、但自己又经常发言的群

不适用场景：

- 不是整个群都要排除，只是想排除其中某类事件
  这时应该优先用 `event_rules.json`

一个直观例子：

- 你已经确认 `oc_0021bbab18a1c2311f76ea72f23cbe18` 这个群不该进入 WorkTrace
  就把它加到 `excluded_conversation_ids` 里，后面这个群的消息就不会再被收集

环境变量会覆盖 `.env` 中的同名项，因此也可以在 CI 或个人 shell 中单独注入。

如果缺少 `WORKTRACE_LLM_BASE_URL`、`WORKTRACE_LLM_MODEL` 或 `WORKTRACE_LLM_API_KEY`，当前 preflight 会直接失败，并明确要求用户先在本地补齐配置；未配置时不得继续运行 WorkTrace。

## 管理人员汇总说明

管理人员汇总模式用于合并多人已经生成的 WorkTrace Markdown，不重新读取员工原始聊天。

汇总前会先过滤未通过结构化保留门槛的来源事件；汇总 LLM 输出的每个 group 也必须带 `retention_reason` 和 `retention_detail`。其中 `retention_detail` 作为保留依据/来源证据使用，最终团队汇总 Markdown 写入前会再次校验。

若来源文件名中的姓名与当前 `lark-cli` 登录用户名精确匹配，则该来源会被标记为“合并人来源”。LLM 仍负责判断哪些事件属于同一真实事项，但如果某个合并组包含“合并人来源”，则最终内容会以该来源为主，其它来源只能补充不冲突的信息，不能覆盖其中已明确写出的版本、结论、进展、结果或待办指向。若当前目录没有匹配到“合并人来源”，系统会写 warning，并回退为普通多人合并。

输入目录：

```text
merge_inbox/YYYY/MM/DD/
├── YYYY-MM-DD-张三.md
├── 张三-YYYY-MM-DD.md
└── 项目A/
    └── YYYY-MM-DD-王五.md
```

日期根目录会生成一个 `YYYY-MM-DD-登录人姓名-merged.md`；日期目录下每个一级子目录也会单独生成自己的团队汇总文件。每个合并范围会读取当前目录下的 `.md` 文件，支持把上游 `*-merged.md` 继续作为输入，但仍会跳过旧 `_merged.md`、当前目录本次输出同名 `YYYY-MM-DD-登录人姓名-merged.md`、隐藏文件、非 Markdown 文件和更深层子目录。

执行命令：

```bash
python3 -m src.worktrace.cli merge-collected --date 2026-06-29
```

输出文件：

```text
merge_inbox/2026/06/29/2026-06-29-管理者-merged.md
merge_inbox/2026/06/29/项目A/2026-06-29-管理者-merged.md
```

执行成功后，飞书机器人会把每个生成的汇总 Markdown 文件发给当前 `lark-cli` 登录用户自己。

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
- 再将当天 Markdown 文件通过飞书 CLI 机器人身份发给员工自己

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
- `linked_file_texts.json`：按需补充的飞书文档 / wiki 正文
- 扩窗相关的请求与补充消息调试文件

这些文件只用于本地调试，不属于正式主流程产物，且可能包含裁剪后的聊天上下文、prompt 与模型输出，不建议长期保留。

## 命令行执行

直接执行某一天的个人日报主流程：

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
  "output_path": "/abs/path/to/data/2026/06/2026-06-23-张三.md",
  "error_summary": ""
}
```

合并管理人员已收集的多人 Markdown：

```bash
python3 -m src.worktrace.cli merge-collected --date 2026-06-29
```

成功时，`stdout` 会返回包含 `source_file_count`、`source_event_count`、`merged_event_count`、`warning_messages`、`self_delivery_status` 和 `outputs` 的 JSON 摘要；`outputs` 会列出每个合并范围的输入目录、输出文件和自发送结果。

## 运行前检查

在首次运行或环境变更后，建议先确认以下项目：

- `python3 --version` 可正常返回版本信息
- `lark-cli` 可正常执行
- 当前 `lark-cli` user 身份已登录，可用于读取目标飞书聊天
- `lark-cli` bot 身份可向当前用户发送文件消息
- 本地 `.env` 或环境变量中已配置在线模型参数
- `WORKTRACE_LLM_REASONING_EFFORT=none`
- 仓库内 `data/` 目录可创建或可写

设计上，WorkTrace CLI 在正式处理前也应执行一次 preflight 检查；若关键依赖缺失、版本不满足、未登录、`no_think` 配置不满足或目录不可写，应直接失败并返回明确原因，而不是在处理中途报错。

## 设计文档

当前相关设计说明见：

- [conversation-slice-retry-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/conversation-slice-retry-design.md)
- [cross-conversation-merge-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/cross-conversation-merge-design.md)
- [collected-people-merge-plan.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/collected-people-merge-plan.md)
- [markdown-output-simplification-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/markdown-output-simplification-design.md)
- [detailed-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/detailed-design.md)

## 开发说明

- 优先修改 `src/` 中的通用逻辑。
- 仓库根目录就是 skill 根目录，不再单独维护 `skill/` 子目录。
- 任何统计或计算必须由 Python 执行，不由 LLM 执行。
