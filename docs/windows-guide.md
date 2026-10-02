# Windows 安装、配置与验收

本次改进依据 Windows 11 AMD64、PowerShell 7.6.5、Python 3.12.10
的真实反馈。新增代码在 macOS 上完成离线验证，仓库配置 Windows CI；
Windows Server 2025 AMD64 的 Python 3.11/3.12 两组离线检查已通过。
这不代表所有 Windows 版本、ARM64 和真实在线业务已经验收。

## 安装

直接依赖 `mslex` 用于保护 CMD 参数中的特殊字符（如文件名里的 `&`）。
需要 Python 3.11+。推荐 PowerShell 7，从仓库目录运行：

```powershell
pwsh -NoProfile -File .\scripts\install_worktrace.ps1
```

脚本安装 `requirements.txt` 并执行 `pip check`，保留已有 `.env`。
Python 版本不足、pip 失败或 Skill 链接失败会停止。默认链接位置是
`$env:CODEX_HOME\skills\worktrace`，未设置时使用当前用户 `.codex`。
含中文、空格目录受支持；符号链接不可用时尝试目录联接。已有目标只有
确实链接到当前仓库才复用，普通目录、其他安装和损坏链接均保留并提示。
可以用 `-SkillDir <目录>` 另选位置，或明确用 `-SkipSkillInstall` 跳过链接。

复制 Skill 文件只提供代码与操作说明，不会安装 Python 依赖、外部 CLI
或私有模型配置。虚拟环境安装示例：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m src.worktrace.cli --help
```

飞书 CLI 的[官方安装说明与发行包](https://github.com/larksuite/cli)。
选择匹配 AMD64/ARM64 的包，并按官方校验说明核验。安装后先阅读：

```powershell
lark-cli config init --help
lark-cli auth login --help
lark-cli auth status
```

按组织应用信息执行初始化，由本人选择并确认用户授权。安装脚本不授予
飞书业务权限。默认双线路还需要 [Codex CLI](https://developers.openai.com/codex/cli)；
`online_only` 无需 Codex。修改用户 PATH 后，重启终端和 Codex，已有进程
不会自动获得新 PATH。临时测试也可以仅向当前 PowerShell 的 `$env:PATH`
追加自己安装的目录，无需修改系统设置。

## 显式导入 Codex 配置

```powershell
python -m src.worktrace.cli import-codex-config
python -m src.worktrace.cli import-codex-config --apply
```

默认仅预览，读取实际 `CODEX_HOME/config.toml` 或用户默认目录。
有默认 profile 时采用该 profile，也可用 `--profile <名称>` 选择。
只读取个人顶层和 profile 的模型、推理强度与 provider；项目配置及会话
命令行覆盖不在导入范围内。缺少必填字段时列出 `missing_keys`，不猜模型。

`values` 是导入后的非敏感设置，`preserved_keys` 是保留的已有键。
`--apply` 只填写 `.env` 空缺项，已有 `high`、模型、provider 均不覆盖。
运行期仍只读取 WorkTrace 本地七项 Codex 设置，继续隔离个人配置和凭据环境。

`auth_source` 显示 provider bearer token、provider 环境变量、Codex 存储或
无需认证；它只说明来源，不表示凭据已经有效。个人 provider 的
`experimental_bearer_token` 不会自动进入隔离运行，`env_key` 也不继承。
需要转换 bearer token 时明确执行：

```powershell
python -m src.worktrace.cli import-codex-config --apply --import-auth
```

此命令调用官方 `codex login --with-api-key`，密钥只经 stdin 传入 Codex
文件认证存储，不写入 WorkTrace `.env`、日志、命令参数或结果。现有认证
文件或已登录/无法确认的认证状态会阻止转换，不会覆盖已有凭据。转换后
`requires_openai_auth=true` 表示采用 Codex 认证存储；不需要认证的 provider
仍可保持 false。若 `.env` 已有 false 或不同 provider，命令先阻止转换，
列出保留值；需本人确认字段含义并手动调整，再重试。已有 CLI 认证无需
再次导入。provider 环境变量认证目前只报告来源，不自动复制环境密钥。
认证操作以 [OpenAI 官方认证文档](https://developers.openai.com/codex/auth)
为依据；认证是否被目标 provider 接受，仍以实际探针为准。

## 自检与错误提示

```powershell
python -m src.worktrace.cli --preflight
python -m src.worktrace.cli --preflight-full
```

默认双线路自检实际探测 Codex，`online_fallback=available` 仅表示备用配置
存在，`online_probe_status=not_run` 表示未探测。完整自检分别探测主线路和
备用 Function Calling：一条失败仍检查另一条，任何必需检查失败则退出 1。
`online_only` 只探测 Online。探针仅提交固定测试内容，不读取私人聊天、
生成日报或发消息，也不改变正式业务的认证错误处理和主备顺序。

完整自检提供各项状态和脱敏错误类别，区分 CLI 缺失、飞书未配置、未登录、
认证被拒绝、权限、网络、TLS 和不支持的模型或推理参数。服务返回 401
不能单凭状态码证明具体是哪份凭据不匹配，会提示检查 provider 与认证来源。
匹配规则和提示维护在 `config/runtime_diagnostics.json`。

Online 的 `tls_verify` / `certificate_verification` 展示实际校验开关。
默认仍兼容原内网配置（关闭校验），不能据此宣称证书可信性已验收；
按自己服务的证书链配置 `WORKTRACE_LLM_TLS_VERIFY=true` 后再检查。
Codex TLS 行为由其 CLI/provider 决定，Online 的字段不代表 Codex 的校验状态。
安装及自检不会修改 hosts、证书或系统网络。确有网络前提时，可由本人在
Windows hosts 文件中添加仅针对指定域名的 `服务IP 自己指定的域名`，
不要照抄其他机器的地址，也不要改变 HTTPS 原域名来绕过证书检查。

## 中文输出、进度与告警

CLI 主进程和外部命令输出按 UTF-8 处理，stdout 只有最终 JSON，日志和
进度写 stderr。等待超过默认 30 秒会显示中文阶段与已耗时，有明确工作量
时显示完成/总量；不显示聊天、prompt、凭据、内部 ID 或预计完成时间。
无需打开原始 debug，即可从个人和多人 JSON 的 `stage_timing_summary`
读取墙钟耗时及请求累计耗时。阶段可能包含其他阶段，不能直接相加；
请求累计耗时表示并发负载，不是实际等待时间。

PowerShell 7.4+ 原生命令直接重定向通常保留字节；管道经 PowerShell
读取时仍受宿主解码设置影响。PowerShell 5.1 可能按旧代码页解码原生管道，
`Out-File` 默认还会写 UTF-16。WorkTrace 不修改系统代码页；推荐升级到
PowerShell 7，或在捕获端明确读取 UTF-8。下面方式由 Python 直接保存
stdout 字节，避免 PowerShell 重新编码（它会执行所给命令，本例只自检）：

```powershell
python -c "import pathlib,subprocess,sys; p=subprocess.run([sys.executable,'-m','src.worktrace.cli','--preflight'],stdout=subprocess.PIPE); pathlib.Path('preflight.json').write_bytes(p.stdout); sys.exit(p.returncode)"
Get-Content -Encoding UTF8 .\preflight.json
```

PowerShell 5.1 的实际管道显示与 ARM64 尚需各自实机复验。

JSON 新增 `warnings` 数组（code、stage、summary），旧个人 `error_summary`
和多人 `warning_messages` 保留以兼容已有消费者。`success_with_warnings`
表示产物成功，但仍需检查送达及跳过数量；`segment_context_missing` 会明确
说明片段已经跳过；若新增消息被输入预算挡住，另有
`segment_context_budget_exceeded` 告警。不能把这份报告认定为数据完整。历史 JSON 缺少新字段
仍可读取。结构化提示不复制原始错误或 ID，旧字段保持原有详细说明。

4.1.4 起，真实日报在最终事件核对失败时，CLI 仍为 `failed`，不会写入或发送 Markdown；已完成阶段的会话、消息、候选和待核对组数会保留在结果中。`event_count=0` 仍只表示没有正式产物。4.1.5 起，`day_grouping_summary.content_render_error_counts` 和安全诊断按固定类别统计最终核对的失败尝试；同次尝试出现多个类别时分别计数，同类问题只计一次。CLI 失败摘要不显示未通过组的 ID，安全诊断不包含原消息或模型返回；业务校验重试与请求失败后的重试仍分别统计。证据校验失败应先核对最终组的动作、事实和角色，不应按网络故障处理。新代码仍需 Windows 实机复测。

## 自动检查范围

`.github/workflows/windows.yml` 在干净 Windows AMD64 环境安装依赖，运行
CLI 帮助、模块导入、`pip check` 和完整离线测试套件。PowerShell 测试覆盖
旧 Python、pip 失败、已存在目标、保留 `.env`、CODEX_HOME 与中文空格路径；
真实 `.cmd/.bat` 进程还验证带 `&`、`%`、`!`、引号和 `^` 的参数，
以及安装目录本身带 `&`、`%`、`!`、`^` 的情况。
认证、飞书和模型使用合成数据，不调用真实业务服务。

Windows CI 显式设置 `PYTHONUTF8=0`，检验测试不依赖全局 UTF-8 模式。涉及报告和模型模式的测试读写文件显式指定 UTF-8，stdin 探针和比较脚本使用 `sys.executable`，避免命中商店的 `python3` 别名。完整套件覆盖锚点实验缓存和多人汇总。

4.1.5 发布前的[完整 Windows CI](https://github.com/sunweisheng/WorkTrace/actions/runs/37038975768) 在提交 `edbe19435169bcea27d2be066475205622800c83` 上通过：Python 3.11 和 3.12 各 1041 项通过、1 项跳过、0 失败，两组均设置 `PYTHONUTF8=0`。这不替代 Windows 11 上的真实日报和跳过数量复测。


2026-10-02 的[Windows CI 复测](https://github.com/sunweisheng/WorkTrace/actions/runs/36974505094)
已通过，代码提交为 `4510b15e0f18878e4bcdf87a2508e44d6325f885`。
Windows Server 2025 AMD64、PowerShell 7、Python 3.11/3.12 两组各
168 项通过、1 项 POSIX 安装检查跳过；macOS 全量测试为 954 项通过、
24 项 Windows/PowerShell 检查跳过。更新代码后仍应查看对应提交的
Actions 结果，不能把这次结果当作后续版本的验证。

## 高优先级修复的新增 CI 验证

[本轮 Windows CI](https://github.com/sunweisheng/WorkTrace/actions/runs/36992852328) 在源码提交 `97a5fe0b39e54fd77686c451c58459182e313e7e` 上通过。Python 3.11 和 3.12 各 358 项通过、1 项跳过；两组均设置 `PYTHONUTF8=0`。此前 Windows 11 完整日报结果仍对应已发布的 4.1.2，本轮没有重跑真实日报或飞书送达。
