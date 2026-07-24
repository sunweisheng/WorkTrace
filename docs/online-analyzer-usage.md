# WorkTrace Online Analyzer 使用说明

> 状态：正式默认 analyzer。

## 1. 调用链

```text
runner / collected_merge
  -> OnlineLLMAnalyzer
  -> openai Python SDK
  -> OpenAI-compatible Responses API provider
```

`OnlineLLMAnalyzer` 当前负责：

- 会话锚点窗口分段
- 会话内片段批量提炼
- 临时协作局部复核
- 个人事实局部复核
- 旧批处理兼容
- 锚点批量回退
- 全日跨会话分组
- 多人 Markdown 合并
- 辅助的结构化工作流归属请求

图片摘要复用同一套模型地址、模型名、API Key、timeout、stream 和 reasoning effort，但由 `OnlineImageSummarizer` 单独发起普通文字/图片理解请求，不强制 Function Calling。`WORKTRACE_LLM_TLS_VERIFY` 只控制 preflight 和文本 analyzer；图片请求保持 OpenAI SDK 自身的证书校验默认行为。

## 2. 本地私有模型配置

```bash
cp .env.example .env
```

必填的连接配置只有三项：

```dotenv
WORKTRACE_LLM_BASE_URL=https://your-openai-compatible-endpoint.example/v1
WORKTRACE_LLM_MODEL=your-model-name
WORKTRACE_LLM_API_KEY=your-api-key
```

主流程要求最终生效的 reasoning effort 为 `none`。该环境变量未配置时，`RuntimeConfig` 默认使用 `none`；模板显式保留这一行：

```dotenv
WORKTRACE_LLM_REASONING_EFFORT=none
```

其他可选项：

```dotenv
WORKTRACE_LLM_TIMEOUT_SECONDS=1200
WORKTRACE_LLM_STREAM=false
WORKTRACE_LLM_TLS_VERIFY=false
```

环境变量优先于仓库根目录 `.env`。真实值不能提交到 git。`WORKTRACE_LLM_STREAM` 是所有 Online 请求的唯一流式开关，代码和模板默认均为 `false`；只有显式设置为 `true` 才启用流式接收。

在线文字请求不等待。可切换的在线失败按 `online_request_retry_limit=1` 只对当前请求再试 Online 1 次，仍失败才切到 Codex；下一请求仍先使用在线线路。Codex 的 `0-1` 秒调用间隔在 `config/llm_retry.json` 配置。图片摘要不使用 Codex 备用线路。

模型调用的请求级重试、结果质量重试、流式首次返回超时、Codex 间隔和并发数不在 `.env` 中配置，而由 `config/llm_retry.json` 管理。当前 Online 请求级配置值 `1` 表示首次失败后最多再试 1 次；话题切分允许 3 个并发请求，事件提炼允许 5 个，个人事实复核和多人高风险复核各允许 3 个；同一会话的话题切分和同一事实复核候选的重试仍保持顺序。分段和提炼的配置值 `3` 均表示首次调用之外最多再重试 3 次。

## 3. 请求规则

固定结构的正式文本请求统一使用任务专用 `FunctionCallSpec`。会话分段、事件提炼、保留复核、事实复核、跨会话分组、工作流归属、多人候选分组、高风险复核、正式内容生成、表情元数据补全和 preflight 分别使用自己的函数名与参数结构。每个参数结构都会：

- prompt 追加 `/no_think`
- 当 reasoning 配置为 `none` 时发送 `reasoning={"effort":"none"}`
- 使用 `strict:true`，通过 `tool_choice` 强制调用当前预期 Function，并关闭并行 Function 调用
- 按当前请求动态枚举 `draft_id`、`segment_id`、消息 ID、附件 ID、链接 ID、工作流 ID 和证据编号
- 完整声明 `required`、`additionalProperties:false`、数组最小/最大数量和去重约束
- 在 prompt 中提供基于当前合法 ID 的典型 Function 参数示例，不再重复嵌入输出结构说明
- 个人事实复核每次只发送一个候选；参数结构固定唯一 `draft_id`，并枚举当前候选允许引用的证据消息 ID
- 个人事实复核的六个文字字段只在 `fact_items` 返回一次，不在外层重复，由 Python 派生最终字段
- `supported=true` 要求标题、正文、具体对象和保留依据都有合法证据；缺少任一必填字段时返回 `supported=false`
- `stream=false` 时响应必须且只能包含一次预期 Function 调用，Python 解析其 `arguments`
- `stream=true` 时按调用 ID 拼接 `response.function_call_arguments.delta`，结束后同样要求且只允许一次预期 Function 调用
- 显式开启流式时，从请求开始到首个流事件的上限为 60 秒；首个流事件返回后，后续读取改用 `WORKTRACE_LLM_TIMEOUT_SECONDS`
- 在线文字请求之间不增加随机等待；`config/llm_retry.json` 的调用间隔只作用于 Codex 请求

Function 参数 Schema 同时传给 Codex `--output-schema`，Codex 备用线路不改为 Function Calling。普通文字总结和图片理解不要求固定结构时不强制 Function Calling；图片摘要使用 `input_image`、base64 data URL 和 `detail=low`。

统一输入估算使用现有确定性字符估算函数：

```text
prepared_prompt = 最终提示词 + 当前合法参数示例 + 当前证据编号清单 + 当前重试错误反馈 + /no_think
online_estimate = estimate(prepared_prompt + tools 完整 Function 定义 + tool_choice)
codex_estimate = estimate(prepared_prompt + 完整 output-schema)
input_estimated_tokens = max(online_estimate, codex_estimate)
```

`model_input_batch_target_tokens=5200` 表示模型输入估算目标，不是 HTTP 字节数或服务端上下文上限。动态枚举、Function 说明、`strict`、`tool_choice` 和关系编号计入；模型名、URL、API Key、timeout 和 stream 不计入。组批和调用前检查使用同一个生产估算函数，响应中的 `usage.input_tokens` 只用于事后核对。

## 4. 返回解析

Online 固定结构解析器只接受预期 Function 的 `arguments`。非流式响应缺少 Function、调用错误 Function 或包含多个 Function 调用都会作为协议错误；流式响应按调用 ID 聚合参数后执行相同检查。参数 JSON 解析完成后，Python 继续校验遗漏、重复、枚举、跨字段关系、证据合法性和来源覆盖。

结果质量校验失败时，只重试当前批次或候选，并在当前 prompt 中加入具体错误码、字段位置、组 ID 和相关编号。加入错误反馈后重新估算；因此超出 5200 时标记为 `oversized_retry` 后仍发送当前请求。达到重试上限后才执行该阶段既有的失败或安全保留策略，不能先静默修补非法证据或来源遗漏。

## 5. Preflight

```bash
python3 -m src.worktrace.cli --preflight
```

在线模型探针发送一个最小 Function Calling 请求，通过 `tool_choice` 强制调用 `submit_worktrace_probe` 并要求参数为 `{"probe":"ok"}`。服务不支持该调用方式时直接报错，不回退旧结构化输出方式。探针单次超时上限为 45 秒，即使正式请求 timeout 更长也不会让自检长期挂起。在线模式还会只检查本机存在 `codex` 命令，为当前请求备用线路做准备，不额外执行 Codex 探针。

个人日报执行前自动调用该检查；`merge-collected` 和 `sync-reaction-catalog` 是独立子命令，不自动运行整套个人日报 preflight。

## 6. 错误映射

| provider/SDK 错误 | WorkTrace 行为 |
| --- | --- |
| `401` | API Key 或认证失败 |
| `403` | 权限不足 |
| `429` | 限流 |
| 其他 HTTP 状态 | 返回状态码和 provider 消息 |
| timeout | 请求超时 |
| TLS/证书失败 | 明确标记 TLS 问题 |
| 网络连接失败 | 服务不可达/网络错误 |
| 缺少/重复/错误 Function 或参数不是合法 JSON | analyzer 协议失败 |

图片摘要失败在个人日报中会降级为 warning 并跳过图片；主分段/提炼/合并请求失败则按 runner 的重试、回退或失败边界处理。

## 7. 客户端生命周期

Online 文本、图片和 preflight 每次请求都重新读取当前配置，创建独立的 OpenAI 与 HTTP 客户端，并在本次请求结束后关闭。实现中不保留全局单例、配置指纹或客户端锁，因此 `.env` 或环境变量的后续变更会从下一次请求开始生效。

## 8. 非默认 Codex analyzer

`RuntimeConfig(analyzer_backend="codex")` 会装配 `CodexAnalyzer`。它是开发/备选路径，不是 `.env` 中可直接切换的普通用户选项。

runner 通过 analyzer 是否具备分段接口选择执行路径：当前 Online/Codex 实现都应满足 `Analyzer` 抽象契约；自定义旧 analyzer 不支持分段时可走会话级 `ConversationSlice` 兼容路径。

## 9. 调试耗时口径

`--debug-output` 生成的 `llm_usage.json` 按 `request_kind` 保存每次成功响应的耗时、输入字符数、输入估算、分批目标、超限标记和 provider 返回的 token。多人 trace 进一步保存 Online/Codex/最终估算、Function 名、去敏定义、证据编号、Python 校验结果、实际输入 token 和估算差值。`scripts/replay_day_with_trace.py` 将调用数据汇总到 `summary.json` 的 `llm_usage_summary`，`scripts/report_replay_timings.py` 优先读取该结构，不再从日志顺序猜测调用类型。

个人事实复核并发时，`personal_fact_review` 表示各候选从首次调用到内部重试结束的耗时，求和后会大于实际运行时间；`personal_fact_review_all` 表示整个并发阶段的墙钟耗时。分析主要耗时时应使用 `personal_fact_review_all`，各候选累计值只用于判断模型调用总负载。
