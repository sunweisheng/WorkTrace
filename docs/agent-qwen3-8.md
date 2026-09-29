# WorkTrace Qwen3.8 Agent 分支

本说明适用于 `codex/qwen3-8-agent`。分支默认通过公司网关调用 `qwen3.8-max`；正文、图片摘要、多人汇总和调试报告共用这一条模型线路，不依赖 Codex CLI。正式版本号仍为 `4.0.2`，用分支名区分部署。

## 部署准备

1. 在 Agent 平台检出本分支，安装 Python 3.11+、`lark-cli` 和 `requirements.txt` 中的依赖。运行目录设为仓库根目录，并提供可持久化的 `data/` 与 `merge_inbox/`。
2. 配置飞书 CLI 的 user 登录身份；`lark-cli auth status` 必须显示可用的 user 身份。个人日报仍从本人可见的聊天读取数据，生成后按 `config/self_delivery.json` 自动把 Markdown 发给本人。
3. 在平台密钥配置中提供 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`。也可在被 Git 忽略的仓库本地 `.env` 中填写这三项；模板见 `.env.example`。`WORKTRACE_LLM_BASE_URL`、`WORKTRACE_LLM_MODEL`、`WORKTRACE_LLM_API_KEY` 仍可用，同一项同时提供时 `LLM_*` 优先。不要提交真实 Key。

已用该网关验证 `chat_completions` 的严格 Function Calling 和图片识别，因此本分支固定使用 `WORKTRACE_LLM_WIRE_API=chat_completions`。测试时开启 `WORKTRACE_LLM_TLS_VERIFY=true` 也已通过。服务若以后改变接口，可显式改为 `responses`，但必须重新运行下面的预检，不能跳过结构化输出与图片验证。

## 运行

```bash
python3 -m pip install -r requirements.txt
python3 -m src.worktrace.cli --preflight
python3 -m src.worktrace.cli --date YYYY-MM-DD
python3 -m src.worktrace.cli merge-collected --date YYYY-MM-DD
```

`--preflight` 会检查 Python、飞书登录、数据目录、模型的单次严格 Function 调用和一张纯色测试图片，不读取真实聊天。个人日报启动前也会运行同样的检查。模型无权限、接口不兼容或图片识别失败时会停止；不会改用 Codex。多人汇总沿用已有的输入目录与本人送达规则。

`qwen3.8-max` 尚无通过评测的更大输入预算，本分支使用 7000 token 的保守分批目标。统计和校验仍由 Python 完成，模型不负责计算。

## 凭据与调试

真实 Key 只放在平台密钥配置或本地 `.env`；该文件已被 Git 忽略。已在对话中公开过的测试 Key 应先轮换。调试模式会产生可能包含聊天内容的 trace，只有通过隐私检查的单份 `support_report` Markdown 可以交给维护人员。
