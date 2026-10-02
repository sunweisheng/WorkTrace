from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]


def prepare_repo(tmp_path):
    repo = tmp_path / "中文 仓库"
    (repo / "scripts").mkdir(parents=True)
    (repo / "requirements.txt").write_text("", encoding="utf-8")
    (repo / ".env.example").write_text("EXAMPLE=1\n", encoding="utf-8")
    return repo


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer")
def test_posix_installer_rejects_old_python_before_pip(tmp_path):
    repo = prepare_repo(tmp_path)
    script = repo / "scripts/install_worktrace.sh"
    shutil.copy(REPO / "scripts/install_worktrace.sh", script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python3"
    python.write_text('#!/bin/sh\n[ "$1" = "--version" ] && exit 0\n'
                      '[ "$1" = "-c" ] && exit 2\nexit 0\n')
    python.chmod(0o755)
    result = subprocess.run(["bash", str(script), str(tmp_path / "skills")],
        env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
        capture_output=True)
    assert result.returncode != 0
    assert not (repo / ".env").exists()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell required")
@pytest.mark.parametrize("failure", ["version", "pip", "link", "none"])
def test_powershell_installer_checks_failures_and_preserves_env(tmp_path, failure):
    repo = prepare_repo(tmp_path)
    script = repo / "scripts/install_worktrace.ps1"
    shutil.copy(REPO / "scripts/install_worktrace.ps1", script)
    env = repo / ".env"
    env.write_text("KEEP=中文\n", encoding="utf-8")
    skill_home = tmp_path / "Codex 中文"
    target = skill_home / "skills/worktrace"
    if failure == "link":
        target.mkdir(parents=True)
    wrapper = tmp_path / "run.ps1"
    wrapper.write_text('''
param($Installer, $HomePath, $FailureMode)
$env:CODEX_HOME = $HomePath
function python {
    if ($args -contains '--version') {
        $global:LASTEXITCODE = 0; return
    }
    if ($args -contains '-c') {
        $global:LASTEXITCODE = $(if ($FailureMode -eq 'version') { 2 } else { 0 })
        return
    }
    $global:LASTEXITCODE = $(if ($FailureMode -eq 'pip') { 1 } else { 0 })
}
try { & $Installer; exit 0 } catch { Write-Error $_; exit 1 }
''', encoding="utf-8-sig")
    result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-File",
        str(wrapper), str(script), str(skill_home), failure], capture_output=True)
    assert (result.returncode == 0) == (failure == "none")
    assert env.read_text(encoding="utf-8") == "KEEP=中文\n"
    if failure == "none":
        assert target.exists()
        assert (target / "requirements.txt").is_file()
        repeated = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-File",
            str(wrapper), str(script), str(skill_home), failure], capture_output=True)
        assert repeated.returncode == 0
        assert env.read_text(encoding="utf-8") == "KEEP=中文\n"
