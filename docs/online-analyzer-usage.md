# WorkTrace Codex 与 Online 调用说明

> 状态：Codex 是正式主线路；Online 是当前请求的备用线路。

## 1. 路由规则

每个新请求都从 Codex 开始，不会因为上一个请求切到 Online 而改变下一请求的起点：

```text
Codex 首次调用
  -> 可重试技术失败时再试 Codex 一次
  -> 仍失败时仅把当前请求交给 Online 一次
  -> 下一个新请求重新优先 Codex
```

网络、超时、限流、服务端异常、空结果和无效 JSON 可以重试或切换。登录、权限、模型不存在、推理强度不支持、TLS 与请求配置错误不会切换线路。Python 结构、编号、证据或覆盖率校验失败时，沿用各任务既有的质量重试次数，将具体错误反馈给 Codex；用尽后才把当前请求交给 Online 一次。

`config/llm_retry.json` 的 `primary_request_retry_limit` 是 Codex 技术失败后的额外重试次数，当前为 `1`。旧键 `online_request_retry_limit` 仍能读取，但仅为兼容旧配置。Online 没有独立的请求级重试；它是当前请求的最后一次备用调用。两条线路共用 `WORKTRACE_LLM_TIMEOUT_SECONDS`。Codex 的全局同时调用上限固定为 3，文字、图片、表情补全和诊断报告共用这个限制。

## 2. 本地配置

先在仓库根目录复制模板：

```bash
cp .env.example .env
```

Codex 主线路必须在这个仓库本地 `.env` 显式写入模型、推理强度和中转提供方设置；不读取进程环境变量，也不继承个人 Codex 的模型或提供方配置：

```dotenv
WORKTRACE_CODEX_MODEL=your-codex-model-name
WORKTRACE_CODEX_REASONING_EFFORT=your-model-supported-effort
WORKTRACE_CODEX_PROVIDER_ID=your-relay-id
WORKTRACE_CODEX_PROVIDER_NAME=your-relay-name
WORKTRACE_CODEX_PROVIDER_BASE_URL=https://your-relay.example/v1
WORKTRACE_CODEX_PROVIDER_WIRE_API=responses
WORKTRACE_CODEX_PROVIDER_REQUIRES_OPENAI_AUTH=true
```

中转站 Key 不写入 WorkTrace `.env`，继续由 Codex CLI 自己的认证存储提供。没有内置模型名或推理强度列表。`--preflight` 用正式严格 Schema 发起一次小探针，验证这个实际组合。正常启动只检查配置，不发送收费的 Online 探针。

Online 备用在需要时使用以下三项连接配置；缺少或不合法时会给出警告并禁用备用，但不阻止 Codex 主线路运行：

```dotenv
WORKTRACE_LLM_BASE_URL=https://your-openai-compatible-endpoint.example/v1
WORKTRACE_LLM_MODEL=your-model-name
WORKTRACE_LLM_API_KEY=your-api-key
```

Online 保持 `WORKTRACE_LLM_REASONING_EFFORT=none`、`WORKTRACE_LLM_STREAM=false`，并可设置 `WORKTRACE_LLM_TLS_VERIFY`。环境变量可以覆盖 Online 连接配置；真实密钥不能提交到 git。`WORKTRACE_LLM_TIMEOUT_SECONDS=1200` 是两条线路的共同总时限。

## 3. 同一任务契约

固定结构任务都由 `FunctionCallSpec` 提供同一份 `name`、`description`、`strict`、动态 `parameters`、典型参数、结构示例和最终自检。函数名称、描述、`strict:true` 与 Codex 提交规则来自 `config/llm_function_contracts.json`，不在 Python 中重复硬编码。

| 线路 | 传输方式 | 严格保证 |
| --- | --- | --- |
| Online 备用 | 原生 Function Calling：`tools`、强制 `tool_choice`、`parallel_tool_calls=false` | `strict:true`；只接受一次预期 Function 调用 |
| Codex 主线路 | 完整契约提示词加 `--output-schema` | 同一 `parameters`；模型只能提交一次参数 JSON 对象，Python 继续校验 |

Codex CLI 没有 `--strict=true` 参数，项目不会伪造该参数。Codex 提示词会明确展示 Function 名称、描述和 `strict=true`，要求不输出 Function 外壳、Markdown、解释或额外字段；同一份动态 `parameters` 写入 `--output-schema`。所有对象 Schema 继续使用 `additionalProperties:false`，所有字段进入 `required`。Python 仍拒绝缺少字段、额外字段、非法枚举、错误证据编号和不完整覆盖。

Online 提示词才追加 `/no_think`，并在请求体中发送 `reasoning={"effort":"none"}`。Codex 实际提示词不追加 `/no_think`。

个人事实复核每次只处理一个候选；其 `draft_id` 与允许引用的证据消息 ID 都由同一份动态参数 Schema 限制。事实字段不在外层重复，统一由 `fact_items` 返回；不同候选最多 3 路并发处理。证据不足以支持必填事实时必须返回 `supported=false`，不返回半完整事件。Python 在两条线路返回后都执行字段完整、枚举、证据归属和覆盖率校验。

统一分批估算仍取两条线路真实请求体中的较大值：

```text
online_prepared_prompt = Function 示例和自检 + /no_think
online_estimate = estimate(online_prepared_prompt + tools + tool_choice)
codex_prepared_prompt = 完整契约提示词 + Codex 提交规则
codex_estimate = estimate(codex_prepared_prompt + 同一 parameters output-schema)
input_estimated_tokens = max(online_estimate, codex_estimate)
```

`model_input_batch_target_tokens=7000` 是输入估算目标，不是 HTTP 字节数或服务端上下文上限。局部重试加入校验错误后若超限，会标记为 `oversized_retry` 后发送。

## 4. Codex 应用级隔离

每次 Codex 调用都在独立的空临时目录执行：提示词通过 stdin 输入，Schema、结果和图片副本只写入该目录，结束即删除。图片摘要仍是普通文本任务，使用 `--image` 传入临时图片副本，不强制改为 Function Calling。

实际命令固定包含：

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
  --disable multi_agent --output-schema <parameters.schema.json> \
  --output-last-message <result.json> -
```

`--disable multi_agent` 是当前 Codex CLI 支持的关闭多智能体能力的方式。不要使用 `-c 'agents.enabled=false'`：当前 CLI 将 `agents` 解释为角色配置对象，布尔值会让命令在发送请求前失败。

子进程只接收允许名单中的运行环境变量，不传递 `WORKTRACE_*`、仓库 `.env`、历史调试目录或其他无关凭据。Python 只把本地 `.env` 中的非密钥中转提供方设置转换为上述 `-c` 参数；Codex 认证仍使用它已有的中转站 Key。程序解析 `--json` 事件，发现 Shell、MCP、Web 或其他工具调用即判为协议违规。

这是应用级隔离：`read-only` 保护写入，不等同于操作系统级读取隔离。

## 5. Preflight 与客户端生命周期

```bash
python3 -m src.worktrace.cli --preflight
```

preflight 检查 Python、`lark-cli` 用户身份、Codex 命令和仓库本地 Codex 配置，并使用正式 `FunctionCallSpec`、正式 Schema 和正式隔离参数执行 Codex 小探针。Online 仅校验配置，不发送请求。

Online 每次请求重新读取配置，创建独立的 OpenAI 与 HTTP 客户端，并在本次请求结束后关闭。图片 Online 备用同样是普通文字/图片理解请求，不强制 Function Calling。

## 6. 调试账本

`--debug-output` 会写入统一的 `llm_calls.json`：个人 trace 位于日期目录，多人 trace 位于对应 scope 目录。每条记录包含调用编号、任务类型、后端、模型、推理强度、重试与切换原因、完整 Function 契约、最终提示词、原始结果和 Python 校验状态；`summary.json` 仍提供 `llm_usage_summary` 的聚合耗时和 token 数据。

账本不保存密钥、认证文件、个人 Codex 配置、完整环境变量、图片内容或原始 Codex JSONL。个人和多人 trace 以调用编号关联账本；安全诊断报告只能读取 Python 已计算并允许的事实，不能读取账本中的 prompt 或原始结果。

全日分组和多人汇总的阶段耗时看各自的 `*_all` 墙钟字段；各请求耗时之和只表示模型调用总负载。
