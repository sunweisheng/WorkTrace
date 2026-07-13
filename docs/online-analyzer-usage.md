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
- 旧批处理兼容
- 锚点批量回退
- 全日跨会话分组
- 多人 Markdown 合并
- 辅助的结构化工作流归属请求

图片摘要复用同一套模型地址、模型名和 API Key，但由 `OnlineImageSummarizer` 单独发起非流式图片请求。

## 2. 本地私有模型配置

```bash
cp .env.example .env
```

必填：

```dotenv
WORKTRACE_LLM_BASE_URL=https://your-openai-compatible-endpoint.example/v1
WORKTRACE_LLM_MODEL=your-model-name
WORKTRACE_LLM_API_KEY=your-api-key
WORKTRACE_LLM_REASONING_EFFORT=none
```

可选：

```dotenv
WORKTRACE_LLM_TIMEOUT_SECONDS=600
WORKTRACE_LLM_STREAM=false
WORKTRACE_LLM_TLS_VERIFY=false
WORKTRACE_LLM_SLEEP_MIN_SECONDS=1
WORKTRACE_LLM_SLEEP_MAX_SECONDS=2
```

环境变量优先于仓库根目录 `.env`。真实值不能提交到 git。

## 3. 请求规则

正式文本请求统一：

- prompt 追加 `/no_think`
- 当 reasoning 配置为 `none` 时发送 `reasoning={"effort":"none"}`
- 按任务传入严格 JSON schema
- `stream=true` 时收集 `response.output_text.delta`
- `stream=false` 时读取完整 Responses payload
- 第二次正式在线请求起，在配置区间内随机等待

图片摘要使用 `input_image`、base64 data URL 和 `detail=low`，不走结构化 JSON schema。

## 4. 返回解析

provider 的结构化输出兼容性并不完全一致。当前解析器接受：

- 纯 JSON
- Markdown 代码块包裹的 JSON
- 文本中可提取的 JSON object/array
- 常见 `structured_output`、`result`、`content`、`message`、`data` envelope

preflight 和正式 analyzer 复用同一 JSON 提取规则，避免模型返回正确代码块 JSON 时自检失败、正式链路却可用的差异。

## 5. Preflight

```bash
python3 -m src.worktrace.cli --preflight
```

在线模型探针发送一个最小 JSON schema 请求并要求返回 `{"probe":"ok"}`。探针单次超时上限为 45 秒，即使正式请求 timeout 更长也不会让自检长期挂起。

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
| 空文本或不可解析 JSON | analyzer 协议失败 |

图片摘要失败在个人日报中会降级为 warning 并跳过图片；主分段/提炼/合并请求失败则按 runner 的重试、回退或失败边界处理。

## 7. 客户端生命周期

文本 analyzer 使用进程级 OpenAI client 缓存。以下设置变化会重建 client：

- base URL
- API Key
- model
- timeout
- TLS verify
- stream
- reasoning effort

图片 summarizer 当前维护自己的 client，不与文本 analyzer 共享全局 client。

## 8. 非默认 Codex analyzer

`RuntimeConfig(analyzer_backend="codex")` 会装配 `CodexAnalyzer`。它是开发/备选路径，不是 `.env` 中可直接切换的普通用户选项。

runner 通过 analyzer 是否具备分段接口选择执行路径：当前 Online/Codex 实现都应满足 `Analyzer` 抽象契约；自定义旧 analyzer 不支持分段时可走会话级 `ConversationSlice` 兼容路径。
