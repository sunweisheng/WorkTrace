#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_DIR="${1:-$HOME/.codex/skills}"

echo "[1/5] 检查 Python..."
if command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD="python"
else
  echo "未找到 Python，请先安装 Python 3.11 或更高版本。"
  exit 1
fi

"$PYTHON_CMD" --version

echo "[2/5] 安装 Python 依赖..."
"$PYTHON_CMD" -m pip install -r "$REPO_ROOT/requirements.txt"

echo "[3/5] 初始化 .env..."
if [ ! -f "$REPO_ROOT/.env" ]; then
  cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
  echo "已创建 .env，请补充你的模型配置。"
else
  echo ".env 已存在，跳过初始化。"
fi

echo "[4/5] 检查 lark-cli..."
if command -v lark-cli >/dev/null 2>&1; then
  echo "已找到 lark-cli。"
else
  echo "未找到 lark-cli。请先按组织要求安装并登录飞书 CLI。"
fi

echo "[5/5] 安装 Skill..."
mkdir -p "$SKILL_DIR"
TARGET="$SKILL_DIR/worktrace"
if [ -e "$TARGET" ]; then
  echo "Skill 目录已存在：$TARGET"
else
  ln -s "$REPO_ROOT" "$TARGET"
  echo "已创建 Skill 链接：$TARGET"
fi

echo
echo "下一步："
echo "1. 打开 $REPO_ROOT/.env，填写 WORKTRACE_LLM_BASE_URL / MODEL / API_KEY"
echo "2. 确认 WORKTRACE_LLM_REASONING_EFFORT=none"
echo "3. 执行自检命令：python3 -m src.worktrace.cli --preflight"
