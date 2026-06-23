# WorkTrace

WorkTrace 是一个面向个人工作回顾的自动化记录项目，目标是从飞书聊天中提取与工作相关的沟通内容，通过 Codex 做语义分析，整理成结构化工作事件和可直接向上级汇报的当日总结，并写入本地 Markdown 文件。首版聚焦“手动指定日期执行”的日处理链路，不承诺真正的定时调度。

仓库根目录同时作为 Codex skill 根目录使用，安装时应链接整个仓库目录，而不是单独链接某个 `skill/` 子目录。

## 当前范围

首版当前确认的能力包括：

- 飞书聊天源，基于 `lark-cli`
- Codex 作为分析 Agent
- Markdown 本地存储
- 事项级事件抽取
- 生成给上级汇报的当日总结
- 同日重跑覆盖
- 只保留工作相关结果，零风险无效消息由 Python 粗过滤，其余是否属于工作事项由 Codex 判断
- 支持跨会话合并事项

## 仓库结构

计划中的仓库结构如下：

```text
.
├── SKILL.md
├── README.md
├── docs/
│   └── detailed-design.md
├── data/
├── src/
└── tests/
```

说明：

- 根目录既是通用脚本项目根目录，也是 Codex skill 根目录。
- `SKILL.md` 放在仓库根目录，作为 skill 入口说明。
- `src/` 承载可复用的 Python 逻辑。

## 核心设计摘要

- Python 负责确定性流程，包括抓取、裁剪、分批、校验和存储。
- Codex 负责批量语义分析，不负责确定性流程控制。
- LLM 不参与数据计算，计算必须由 Python 完成。
- Python CLI 固定为 `python -m src.worktrace.cli --date YYYY-MM-DD`，`stdout` 返回 machine-readable JSON 执行摘要。
- `event_id` 由 Python 基于目标日期内归一化后的来源消息集合稳定生成。
- 原始聊天内容不长期落盘，只保留结构化事件和每日总结。
- 存储目标为按年月目录组织的每日 Markdown 文件。

## 依赖说明

首版依赖如下：

- `python3`
- `lark-cli`
- Codex
- 已登录且具备消息读取权限的飞书 user 身份

当前环境默认的 Codex skill 安装目录为 `~/.codex/skills`。

## 环境要求

建议至少满足以下环境要求：

- `python3` 已安装，且版本满足项目要求
- `lark-cli` 已安装，并可在 `PATH` 中直接调用
- `lark-cli` 已登录为具备消息读取权限的飞书 user 身份
- Codex 调用能力可用
- 当前用户对仓库目录及 `data/` 结果目录具备可创建、可写权限
- 运行环境可使用 `Asia/Shanghai` 作为业务时区

## Codex Skill 安装说明

首版推荐使用本地软链接方式安装 skill，便于一边开发一边调试。安装时链接整个仓库目录。

1. 确保仓库根目录存在 `SKILL.md`
2. 创建软链接到 Codex skill 目录
3. 重启 Codex 以加载新 skill

示例命令：

```bash
mkdir -p ~/.codex/skills
ln -s /path/to/WorkTrace ~/.codex/skills/worktrace
```

说明：

- `~/.codex/skills` 是默认本地 skill 目录。
- `worktrace` 是建议的安装名。
- 如果同名目录已经存在，请先手动清理或改用其他名字。
- 安装完成后需要重启 Codex，Codex 才会重新扫描并识别新 skill。

远程 GitHub 安装方式不是当前主流程，后续如需公开分享，可再补充相应安装说明。

## 使用方式

首版预期使用形态如下：

- 在 Codex 对话中触发 WorkTrace skill
- 指定一个目标日期执行
- 读取目标日期内本人至少发过 1 条消息的飞书会话
- 输出结构化工作事件
- 将结果写入本地 Markdown 文件

具体触发提示词与参数约定由根目录 `SKILL.md` 定义，当前 README 不固定写死最终触发语句。

## 运行前检查

在首次运行或环境变更后，建议先确认以下项目：

- `python3 --version` 可正常返回版本信息
- `lark-cli` 可正常执行
- 当前 `lark-cli` 登录身份为目标飞书 user，而不是 bot 或未登录状态
- Codex 当前会话可正常调用
- 仓库内 `data/` 目录可创建或可写

设计上，WorkTrace CLI 在正式处理前也应执行一次 preflight 检查；若关键依赖缺失、版本不满足、未登录或目录不可写，应直接失败并返回明确原因，而不是在处理中途报错。

## 设计文档

详细设计见 [docs/detailed-design.md](/Users/sunweisheng/Documents/GitHub/WorkTrace/docs/detailed-design.md)。

该文档包含：

- 抽象工厂设计
- 批量分析策略
- 脚本与 LLM 职责边界
- 存储与覆盖策略

## 开发说明

- 优先修改 `src/` 中的通用逻辑。
- 仓库根目录就是 skill 根目录，不再单独维护 `skill/` 子目录。
- 任何统计或计算必须由 Python 执行，不由 LLM 执行。
