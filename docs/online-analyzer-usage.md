# WorkTrace Codex 与 Online 调用说明

> 状态：当前正式流程支持 `codex_with_fallback` 和 `online_only`，默认使用前者。

## 1. 路由规则

`WORKTRACE_LLM_MODE` 从进程环境和仓库本地 `.env` 读取，进程环境优先。未配置时为 `codex_with_fallback`；空值或其他值会明确报错，不会静默切换。

| 模式 | 首选线路 | 备用线路 | Codex 依赖 |
| --- | --- | --- | --- |
| `codex_with_fallback` | Codex | 配置有效时仅当前请求使用 Online | 要求命令、认证和本地七项配置 |
| `online_only` | Online | 无 | 正式流程不检查或调用 Codex |

默认双线路模式下，每个新请求都从 Codex 开始，不会因为上一个请求切到 Online 而改变下一请求的起点：

```text
Codex 首次调用
  -> 可重试技术失败时再试 Codex 一次
  -> 仍失败时仅把当前请求交给 Online 一次
  -> 下一个新请求重新优先 Codex
```

网络、超时、限流、服务端异常、空结果和不可解析 JSON 可以进入请求级重试或切换。登录、权限、模型不存在、推理强度不支持、TLS 与请求配置错误不会进入请求级切换。

`config/llm_retry.json` 的 `primary_request_retry_limit` 是首选线路技术失败后的额外重试次数，当前为 `1`。旧键 `online_request_retry_limit` 仍能读取，但仅为兼容旧配置。在默认双线路模式中，Online 没有独立的请求级重试；它是该次底层请求的最后一次备用调用。仅 Online 模式由同一配置控制 Online 的请求级重试，最后不追加备用请求，调试记录 `backend=online`、备用次数为 `0`。

各任务还有独立的外层重试和失败处理，不能把底层线路图理解为所有阶段的总调用次数：

- 会话切分、片段提炼、锚点回退、临时协作复核和个人事实复核在默认模式下仍可能捕获请求异常并进入各自外层重试，下一轮再次优先 Codex；仅 Online 的终止请求错误会直接传播。
- 临时协作与个人事实复核会把具体验证错误放入局部重试提示词；切分、片段提炼和锚点批量重试沿用原输入，部分非法来源或缺失成员也会被过滤、跳过并记 warning，不能承诺每次校验失败都会反馈或重试。
- 全日分组、标题发现、完整内容复核和最终正文重写各自携带质量反馈，质量重试用尽后，在默认模式且尚未用过备用时可将当前请求交给 Online 一次。全日分组技术调用失败会终止；返回结果持续非法时保留合法组并把其余候选拆单。标题发现失败按没有标题候选继续，局部复核失败保留原分组，正文失败使用当前组的确定性结果并写 warning。

两条线路都读取 `WORKTRACE_LLM_TIMEOUT_SECONDS`，未配置时为 `180` 秒；该值约束每次 Online 请求或 Codex 子进程执行，不是包括外层重试和 Codex 排队等待的整天总时限。Codex 的全局同时调用上限固定为 `3`，文字、图片、表情补全和诊断报告共用；启动间隔由 `codex_request_interval_min_seconds` / `codex_request_interval_max_seconds` 控制，当前为 `0` 到 `1` 秒。

## 2. 本地配置

先在仓库根目录复制模板：

```bash
cp .env.example .env
```

先选择模式：

```dotenv
WORKTRACE_LLM_MODE=codex_with_fallback
```

仅使用 Online 时把该值改为 `online_only`，只需配置下方三项 Online 连接值，不要求安装、登录或配置 Codex。

默认模式的 Codex 主线路必须在这个仓库本地 `.env` 显式写入模型、推理强度和中转提供方设置；这七项不读取进程环境变量，也不继承个人 Codex 的模型或提供方配置：

```dotenv
WORKTRACE_CODEX_MODEL=your-codex-model-name
WORKTRACE_CODEX_REASONING_EFFORT=your-model-supported-effort
WORKTRACE_CODEX_PROVIDER_ID=your-relay-id
WORKTRACE_CODEX_PROVIDER_NAME=your-relay-name
WORKTRACE_CODEX_PROVIDER_BASE_URL=https://your-relay.example/v1
WORKTRACE_CODEX_PROVIDER_WIRE_API=responses
WORKTRACE_CODEX_PROVIDER_REQUIRES_OPENAI_AUTH=true
```

中转站 Key 不写入 WorkTrace `.env`，继续由 Codex CLI 自己的认证存储提供。没有内置模型名或推理强度列表。默认模式自检用正式严格 Schema 发起 Codex 小探针，验证这个实际组合；仅 Online 自检改用 Online Function Calling 探针。正式个人日报启动和独立锚点实验启动也会执行自检，不能把它们描述为只检查配置。

Online 使用以下三项连接配置：默认模式中可作为备用，缺少或不合法时自检详情记录警告并禁用备用，不阻止 Codex 主线路；仅 Online 模式中三项必填，缺失会停止生成：

```dotenv
WORKTRACE_LLM_BASE_URL=https://your-openai-compatible-endpoint.example/v1
WORKTRACE_LLM_MODEL=your-model-name
WORKTRACE_LLM_API_KEY=your-api-key
WORKTRACE_LLM_WIRE_API=responses
```

`WORKTRACE_LLM_WIRE_API` 只允许 `responses` 或 `chat_completions`，不配置时默认 Responses，也不会根据模型名或服务地址自动切换。Online 默认使用 `WORKTRACE_LLM_REASONING_EFFORT=high`，只输出最终结果。`WORKTRACE_LLM_STREAM=false` 是默认值，可按服务能力显式开启流式；文字和图片共用该开关，Online 自检探针固定非流式。`WORKTRACE_LLM_TLS_VERIFY` 可控制证书验证。进程环境可以覆盖 Online、模式及共享超时配置；真实密钥不能提交到 git。模板的 `WORKTRACE_LLM_TIMEOUT_SECONDS=1200` 为示例设置，未配置时仍使用代码默认 `180` 秒。

升级保留已有 `.env` 和平台注入值，旧的 `none` 不会自动变成 `high`。使用高推理时须同步修改 `WORKTRACE_LLM_REASONING_EFFORT`；进程环境优先于 `.env`。预检不再限制只能使用 `none`，也不会因设置 `high` 禁用 Online 备用。

## 3. 同一任务契约

固定结构任务都由 `FunctionCallSpec` 提供同一份 `name`、`description`、`strict`、动态 `parameters`、典型参数、结构示例和最终自检。函数名称、描述、`strict:true` 与 Codex 提交规则来自 `config/llm_function_contracts.json`，不在 Python 中重复硬编码。

部分 Online 服务不接受 `uniqueItems`，也会拒绝“数组最小数量大于当前动态枚举数量”的不可满足约束。程序只在 Online 发送前生成兼容副本并移除这两类限制；原始 Function schema、Codex `--output-schema` 和 Python 的重复、数量、证据及来源覆盖校验保持不变。

| 线路 | 传输方式 | 严格保证 |
| --- | --- | --- |
| Online（仅 Online 或默认模式备用） | Responses 或 Chat Completions 原生 Function Calling：`tools`、`tool_choice`（高推理时 `auto`，`none` 时指定函数）、`parallel_tool_calls=false` | `strict:true`；非流式和流式都只接受一次预期 Function 调用 |
| Codex（默认模式主线路） | 完整契约提示词加 `--output-schema` | 同一 `parameters`；提示词要求只提交一次参数 JSON 对象，Python 继续按任务校验 |

Codex CLI 没有 `--strict=true` 参数，项目不会伪造该参数。Codex 提示词会明确展示 Function 名称、描述和 `strict=true`，要求不输出 Function 外壳、Markdown、解释或额外字段；同一份动态 `parameters` 写入 `--output-schema`。对象 Schema 使用 `additionalProperties:false` 并列出必填字段。Schema 是请求契约，Python 的实际字段、证据和覆盖校验由各任务执行；旧领域解析器和部分候选过滤仍有兼容处理，不能把服务端严格 Schema 当作本地每种字段错误都必然拒绝的保证。

Online 默认使用 `WORKTRACE_LLM_REASONING_EFFORT=high`。Responses 发送 `reasoning.effort=high`，Chat Completions 发送 `reasoning_effort=high`，文字、图片和在线探针使用同一设置。高推理设置不追加 `/no_think`，输出仍要求仅提交最终结果，不展示思考过程。显式配置 `none` 时保留旧行为：追加 `/no_think`，Responses 发送 `reasoning.effort=none`，Chat Completions 发送 `thinking.type=disabled`。实际服务是否支持所选强度，以完整自检和真实业务复测为准。

个人事实复核每次只处理一个候选；其 `draft_id` 与允许引用的证据消息 ID 都由同一份动态参数 Schema 限制。事实字段不在外层重复，统一由 `fact_items` 返回；不同候选最多 3 路并发处理。证据不足以支持必填事实时必须返回 `supported=false`，不返回半完整事件。Python 在两条线路返回后都执行字段完整、枚举、证据归属和覆盖率校验。

统一分批估算仍取两条线路真实请求体中的较大值：

```text
online_prepared_prompt = Function 示例和自检 + /no_think（仅 none 时发送）
responses_estimate = estimate(Responses prompt + tools + tool_choice)
chat_estimate = estimate(Chat messages + tools + tool_choice)
codex_prepared_prompt = 完整契约提示词 + Codex 提交规则
codex_estimate = estimate(codex_prepared_prompt + 同一 parameters output-schema)
online_estimate = max(responses_estimate, chat_estimate)
input_estimated_tokens = max(online_estimate, codex_estimate)
```

默认模式的 `model_input_batch_target_tokens` 从 `config/model_input_budget.json` 中与主模型、备用模型名称精确匹配的预算 profile 读取；没有匹配项时使用配置默认预算（当前为 `7000`），配置文件不存在时回退 `7000`。仅 Online 直接使用配置默认预算，不套用 Codex 模型组合 profile；仍用同一个三种请求结构的最大值估算函数，但不会因此读取 Codex 配置或调用 Codex。它是输入估算目标，不是 HTTP 字节数或服务端上下文上限。最小必要输入仍超限时按任务标记 `oversized_singleton`，局部重试反馈造成超限时标记 `oversized_retry` 后发送。

## 4. Codex 应用级隔离

每次 Codex 调用都在独立的空临时目录执行：提示词通过 stdin 输入，Schema、结果和图片副本只写入该目录，结束即删除。图片摘要仍是普通文本任务，使用 `--image` 传入临时图片副本，不强制改为 Function Calling。

固定结构任务的命令结构如下，尖括号表示 Python 生成的占位值，并非可直接执行的 Shell 命令；普通图片摘要省略 `--output-schema` 并增加 `--image`：

```bash
codex exec --skip-git-repo-check --ephemeral --ignore-user-config --ignore-rules \
  --strict-config --sandbox read-only --json --model "$WORKTRACE_CODEX_MODEL" \
  -c 'model_provider="<WORKTRACE_CODEX_PROVIDER_ID>"' \
  -c 'model_providers.<id>.name="<WORKTRACE_CODEX_PROVIDER_NAME>"' \
  -c 'model_providers.<id>.base_url="<WORKTRACE_CODEX_PROVIDER_BASE_URL>"' \
  -c 'model_providers.<id>.wire_api="<WORKTRACE_CODEX_PROVIDER_WIRE_API>"' \
  -c 'model_providers.<id>.requires_openai_auth=<WORKTRACE_CODEX_PROVIDER_REQUIRES_OPENAI_AUTH>' \
  -c 'model_reasoning_effort="<WORKTRACE_CODEX_REASONING_EFFORT>"' \
  -c 'shell_environment_policy.inherit="none"' -c 'web_search="disabled"' \
  --disable multi_agent --disable shell_tool \
  --output-schema <parameters.schema.json> \
  --output-last-message <result.json> -
```

`--disable multi_agent` 是当前 Codex CLI 支持的关闭多智能体能力的方式。不要使用 `-c 'agents.enabled=false'`：当前 CLI 将 `agents` 解释为角色配置对象，布尔值会让命令在发送请求前失败。

子进程只接收允许名单中的运行环境变量，不传递 `WORKTRACE_*`、仓库 `.env`、历史调试目录或其他无关凭据。Windows 额外保留系统目录、用户目录和临时目录所需变量，变量名按大小写不敏感处理。Python 把本地 `.env` 中的非密钥中转提供方设置转换为安全 TOML 字面量；Codex 认证仍使用它已有的中转站 Key。程序解析 `--json` 事件，允许不执行任何操作的 `error` 提示项，发现 Shell、MCP、Web、其他工具调用或未知项仍判为协议违规，非零退出码仍为失败。

这是应用级隔离：`read-only` 保护写入，不等同于操作系统级读取隔离。

## 5. Preflight 与客户端生命周期

```bash
python3 -m src.worktrace.cli --preflight
```

默认双线路模式的 preflight 检查 Python、`lark-cli` 用户身份、Codex 命令和仓库本地 Codex 配置，并使用正式 `FunctionCallSpec`、正式 Schema 和正式隔离参数执行 Codex 小探针。该模式的 Online 备用仅校验配置，不发送请求。

仅 Online 时检查 Python、`lark-cli` 用户身份和 Online 配置，以当前 `responses` / `chat_completions` 执行一个非流式 Function Calling 探针；不检查 Codex 命令、配置、认证或版本。Online 探针超时取共享配置与 `45` 秒中的较小值，Codex 探针使用正式子进程请求时限。两种模式还检查数据目录可写和时区可用，自检不发送测试文件。

Online 每次请求重新读取配置，创建独立的 OpenAI 与 HTTP 客户端，并在本次请求结束后关闭。图片 Online 备用和独立 Online 探针都使用相同的接口开关；图片仍是普通文字/图片理解请求，不强制 Function Calling。

默认模式图片摘要先走 Codex 普通文本任务和 `--image`；可重试技术错误按配置重试，工具调用等协议违规或配置定义的无法识别回复可直接触发当前图片 Online 备用一次。仅 Online 的图片摘要由 Online 执行并读取相同请求级重试配置，不追加备用。图片处理异常由调用位置处理并写 warning，不应推断为所有结构化任务都会继续。

正式的仅 Online 模式不依赖 Codex，限于遵循运行时工厂的入口。`scripts/benchmark_model_input_budget.py` 是显式双线路评测工具，直接构造 Codex 与 Online analyzer，不随 `online_only` 自动改成单线路评测。`scripts/replay_day_with_trace.py` 默认遵循配置，保留的显式 `--analyzer-backend online/codex` 分别映射为 `online_only` / `codex_with_fallback`；诊断时不应擅自传入覆盖值。

## 6. 调试账本

`--debug-output` 会写入统一的 `llm_calls.json`：个人 trace 位于日期目录，多人 trace 位于对应 scope 目录。每条记录包含调用编号、任务类型、后端、模型、推理强度、重试与切换原因、完整 Function 契约、最终提示词、原始结果和 Python 校验状态；`summary.json` 仍提供 `llm_usage_summary` 的聚合耗时和 token 数据。

账本不保存密钥、认证文件、个人 Codex 配置、完整环境变量、图片内容或原始 Codex JSONL。个人和多人 trace 以调用编号关联账本；安全诊断报告只能读取 Python 已计算并允许的事实，不能读取账本中的 prompt 或原始结果。

全日分组和多人汇总的阶段耗时看各自的 `*_all` 墙钟字段；各请求耗时之和只表示模型调用总负载。

输入预算估算保守地预留 `/no_think` 的长度，实际高推理请求不发送该指令；重试中的上一份个人最终返回也计入完整输入。
