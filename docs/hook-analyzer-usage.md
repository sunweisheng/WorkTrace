# WorkTrace Hook Analyzer 使用说明

## 1. 目的

`HookAnalyzer` 是 WorkTrace 当前默认的分析后端。

它负责：

- 把 WorkTrace 生成的 prompt 写到子进程 `stdin`
- 调一个外部 hook 命令
- 从该命令的 `stdout` 读取 JSON

这样可以把 WorkTrace 主流程和具体 LLM provider 解耦。

当前默认链路为：

```text
HookAnalyzer -> hook_runner.py --mode chat-completions-http -> 在线 Chat Completions API provider
```

## 2. 当前默认模式

当前仓库默认使用：

```bash
python3 -m src.worktrace.hook_runner --mode chat-completions-http
```

这个模式会：

- 从 `stdin` 读取 prompt
- 从本地 `.env` 或环境变量读取在线模型配置
- 直接调用兼容 OpenAI `Chat Completions API` 的在线服务
- HTTP 请求通过 `curl -w` 输出网络阶段耗时
- 继续消费 `WORKTRACE_HOOK_SCHEMA_PATH`，尽量带上 WorkTrace 当前任务的 JSON schema 约束
- 把模型输出归一化为标准 JSON 后写回 `stdout`

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
```

建议把它们放在仓库根目录 `.env`，不要提交真实值到 git。

优先级如下：

1. 进程环境变量
2. 仓库根目录 `.env`

如果缺少任一必填项，preflight 和 `chat-completions-http` 模式都会直接失败，并要求使用者先单独在本地配置这些值，不能随仓库提交。

## 4. 如何启用 HookAnalyzer

在 Python 中可这样创建配置：

```python
from pathlib import Path
from src.worktrace.config import RuntimeConfig

config = RuntimeConfig(
    data_root=Path("data"),
    analyzer_backend="hook",
    hook_command="python3 -m src.worktrace.hook_runner --mode chat-completions-http",
)
```

## 5. 兼容回退模式

如果需要排障或和旧路径对照，仍可显式切回：

```bash
python3 -m src.worktrace.hook_runner --mode codex-stdin
```

这个模式会继续通过 `codex exec -` 调用 Codex，但它不再是默认模式，且主流程仍要求先通过本地 `WORKTRACE_LLM_*` 配置检查。

## 6. 输出与错误行为

- `hook_runner.py` 会尽量把模型响应包裹层、JSON 代码块或文本片段归一化为当前 analyzer 可消费的 JSON。
- 在线 provider 常见错误会被归类：
  - `401` / `403`：密钥或权限问题
  - `429`：限流
  - `5xx` 或连接失败：服务不可用
- 主流程最终只消费结构化事件结果，不再依赖单独的“管理者总结”输出。
