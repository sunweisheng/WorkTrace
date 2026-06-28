param(
    [string]$SkillDir = "",
    [switch]$SkipSkillInstall
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $SkillDir) {
    $SkillDir = Join-Path $HOME ".codex\skills"
}

function Write-NextSteps {
    param([string]$EnvFilePath)

    Write-Host ""
    Write-Host "下一步："
    Write-Host "1. 打开 $EnvFilePath，填写 WORKTRACE_LLM_BASE_URL / MODEL / API_KEY"
    Write-Host "2. 确认 WORKTRACE_LLM_REASONING_EFFORT=none"
    Write-Host "3. 执行自检命令：python -m src.worktrace.cli --preflight"
}

function Install-SkillLink {
    param(
        [string]$TargetPath,
        [string]$SourcePath
    )

    if (Test-Path $TargetPath) {
        Write-Host "Skill 目录已存在：$TargetPath"
        return
    }

    try {
        New-Item -ItemType SymbolicLink -Path $TargetPath -Target $SourcePath | Out-Null
        Write-Host "已创建 Skill 符号链接：$TargetPath"
        return
    } catch {
        Write-Host "创建符号链接失败，尝试使用目录联接继续安装..."
    }

    try {
        New-Item -ItemType Junction -Path $TargetPath -Target $SourcePath | Out-Null
        Write-Host "已创建 Skill 目录联接：$TargetPath"
        return
    } catch {
        Write-Host "自动安装 Skill 失败。"
        Write-Host "请检查是否开启了 Windows Developer Mode，或手动把仓库目录放到：$SkillDir"
        Write-Host "建议手动目标名：$TargetPath"
    }
}

Write-Host "[1/5] 检查 Python..."
if (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonCmd = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonCmd = "py"
} else {
    throw "未找到 Python，请先安装 Python 3.11 或更高版本。"
}

& $PythonCmd --version

Write-Host "[2/5] 安装 Python 依赖..."
& $PythonCmd -m pip install -r (Join-Path $RepoRoot "requirements.txt")

Write-Host "[3/5] 初始化 .env..."
$EnvExample = Join-Path $RepoRoot ".env.example"
$EnvFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item $EnvExample $EnvFile
    Write-Host "已创建 .env，请补充你的模型配置。"
} else {
    Write-Host ".env 已存在，跳过初始化。"
}

Write-Host "[4/5] 检查 lark-cli..."
if (Get-Command lark-cli -ErrorAction SilentlyContinue) {
    Write-Host "已找到 lark-cli。"
} else {
    Write-Host "未找到 lark-cli。请先按组织要求安装并登录飞书 CLI。"
}

Write-Host "[5/5] 安装 Skill..."
if ($SkipSkillInstall) {
    Write-Host "已跳过 Skill 安装。"
} else {
    New-Item -ItemType Directory -Force -Path $SkillDir | Out-Null
    $Target = Join-Path $SkillDir "worktrace"
    Install-SkillLink -TargetPath $Target -SourcePath $RepoRoot
}

Write-NextSteps -EnvFilePath $EnvFile
