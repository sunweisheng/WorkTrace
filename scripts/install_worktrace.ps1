param(
    [string]$SkillDir = "",
    [switch]$SkipSkillInstall
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $SkillDir) {
    $CodexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
    $SkillDir = Join-Path $CodexRoot "skills"
}

function Assert-NativeSuccess {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step 失败（退出码 $LASTEXITCODE），安装已停止。"
    }
}

function Install-SkillLink {
    param([string]$TargetPath, [string]$SourcePath)
    $Existing = Get-Item -LiteralPath $TargetPath -Force -ErrorAction SilentlyContinue
    if ($Existing) {
        $LinkTarget = @($Existing.Target) | Select-Object -First 1
        if ($LinkTarget) {
            if (-not [System.IO.Path]::IsPathRooted($LinkTarget)) {
                $LinkTarget = Join-Path (Split-Path -Parent $TargetPath) $LinkTarget
            }
            $Resolved = [System.IO.Path]::GetFullPath($LinkTarget).TrimEnd('\', '/')
            $Expected = [System.IO.Path]::GetFullPath($SourcePath).TrimEnd('\', '/')
            if ($Resolved -eq $Expected -and (Test-Path -LiteralPath $TargetPath)) {
                Write-Host "Skill 已正确链接：$TargetPath"
                return
            }
        }
        throw "Skill 目标已存在，但不是当前仓库的有效链接：$TargetPath。请检查或另选 -SkillDir；原目录已保留。"
    }
    try {
        New-Item -ItemType SymbolicLink -Path $TargetPath -Target $SourcePath | Out-Null
        Write-Host "已创建 Skill 符号链接：$TargetPath"
        return
    } catch {
        Write-Host "符号链接不可用，尝试普通用户可用的目录联接..."
    }
    try {
        New-Item -ItemType Junction -Path $TargetPath -Target $SourcePath | Out-Null
        Write-Host "已创建 Skill 目录联接：$TargetPath"
    } catch {
        throw "Skill 链接创建失败。请检查目录权限或使用 -SkipSkillInstall 后手动安装；安装尚未完成。"
    }
}

Write-Host "[1/5] 检查 Python..."
if (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonCmd = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonCmd = "py"
} else {
    throw "未找到 Python，请先安装 Python 3.11 或更高版本并加入 PATH。"
}
& $PythonCmd --version
Assert-NativeSuccess "Python 启动"
& $PythonCmd -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 2)'
Assert-NativeSuccess "Python 版本检查（要求 3.11+）"

Write-Host "[2/5] 安装 Python 依赖..."
& $PythonCmd -m pip install -r (Join-Path $RepoRoot "requirements.txt")
Assert-NativeSuccess "pip 依赖安装"
& $PythonCmd -m pip check
Assert-NativeSuccess "pip 依赖校验"

Write-Host "[3/5] 初始化 .env..."
$EnvFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path -LiteralPath $EnvFile)) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot ".env.example") -Destination $EnvFile
    Write-Host "已创建 .env，请补充模型配置；复制 Skill 本身不会安装依赖和外部 CLI。"
} else {
    Write-Host ".env 已存在，保留现有配置。"
}

Write-Host "[4/5] 检查外部命令..."
if (Get-Command lark-cli -ErrorAction SilentlyContinue) {
    Write-Host "已找到 lark-cli；请使用 lark-cli auth status 检查用户身份。"
} else {
    Write-Host "未找到 lark-cli。官方安装说明：https://github.com/larksuite/cli"
    Write-Host "选择与 Windows AMD64/ARM64 匹配的官方发行包，按官方校验说明安装。"
    Write-Host "之后执行 lark-cli config init 和 lark-cli auth login，由本人选择并确认授权。"
}
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Host "未找到 codex。默认模式需要 Codex CLI；online_only 无需 Codex。"
    Write-Host "官方安装说明：https://developers.openai.com/codex/cli"
}
Write-Host "修改用户 PATH 后请重启终端和 Codex；已有进程不会自动读取新 PATH。"

Write-Host "[5/5] 安装 Skill..."
if ($SkipSkillInstall) {
    Write-Host "已按参数跳过 Skill 安装。"
} else {
    New-Item -ItemType Directory -Force -Path $SkillDir | Out-Null
    Install-SkillLink -TargetPath (Join-Path $SkillDir "worktrace") -SourcePath $RepoRoot
}
Write-Host ""
Write-Host "依赖准备完成；外部 CLI、模型配置和用户授权仍需通过自检。"
Write-Host "在仓库目录执行："
Write-Host "1. python -m src.worktrace.cli import-codex-config（预览；--apply 只填写空缺）"
Write-Host "2. 打开 $EnvFile，选择模式并填写 Online 配置，推理保持 none"
Write-Host "3. python -m src.worktrace.cli --preflight-full（实际探测两条配置线路）"
Write-Host "中文管道读取和可选 hosts 说明见 docs/windows-guide.md；安装不修改网络设置。"
