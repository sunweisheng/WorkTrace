# WorkTrace Online Analyzer 使用说明

## 1. 目的

`OnlineLLMAnalyzer` 是 WorkTrace 当前默认的分析后端。

它负责：

- 在 Python 进程内直接构造 prompt
- 通过 OpenAI Python SDK 调用兼容 OpenAI `Responses API` 的在线服务
- 把模型输出归一化为结构化 JSON 并返回给主流程

## 2. 当前默认模式

当前仓库默认使用内置在线 analyzer：

```python
RuntimeConfig(analyzer_backend="online")
```

这个模式会：

- 从本地 `.env` 或环境变量读取在线模型配置
- 使用 OpenAI Python SDK 发起 HTTPS 请求
- 支持内部 `stream=true` 流式接收，但最终仍只返回完整 JSON
- 支持关闭 HTTPS 证书校验，兼容客户私有中转站
- 在第二次正式在线请求起，对相邻请求随机睡眠 `1-2` 秒

## 3. 本地配置

默认模式要求用户在本地单独配置以下参数：

```dotenv
WORKTRACE_LLM_BASE_URL=https://your-openai-compatible-endpoint.example/v1
WORKTRACE_LLM_MODEL=your-model-name
WORKTRACE_LLM_API_KEY=your-api-key
```

可选：

```dotenv
WORKTRACE_LLM_TIMEOUT_SECONDS=180
WORKTRACE_LLM_STREAM=true
WORKTRACE_LLM_TLS_VERIFY=false
WORKTRACE_LLM_REASONING_EFFORT=none
WORKTRACE_LLM_SLEEP_MIN_SECONDS=1
WORKTRACE_LLM_SLEEP_MAX_SECONDS=2
```

建议把它们放在仓库根目录 `.env`，不要提交真实值到 git。

优先级如下：

1. 进程环境变量
2. 仓库根目录 `.env`

如果缺少任一必填项，preflight 会直接失败，并要求使用者先单独在本地配置这些值，不能随仓库提交。

## 4. 如何启用 Online Analyzer

在 Python 中可这样创建配置：

```python
from pathlib import Path
from src.worktrace.config import RuntimeConfig

config = RuntimeConfig(
    data_root=Path("data"),
    analyzer_backend="online",
)
```

## 5. 输出与错误行为

- 主流程最终只消费结构化事件结果，不依赖流式增量文本。
- 在线 provider 常见错误会被归类：
  - `401` / `403`：密钥或权限问题
  - `429`：限流
  - `5xx`：服务不可用
  - TLS 校验失败：证书不被信任
  - 网络错误：服务不可达
