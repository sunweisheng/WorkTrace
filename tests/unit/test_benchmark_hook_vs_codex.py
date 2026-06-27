from __future__ import annotations

from pathlib import Path

from src.worktrace.benchmark_hook_vs_codex import _run_command


def test_run_command_captures_duration_and_stdout(tmp_path: Path) -> None:
    payload = _run_command(
        ["python3", "-c", "print('ok')"],
        cwd=tmp_path,
    )

    assert payload["returncode"] == 0
    assert payload["duration_ms"] >= 0
    assert payload["stdout"].strip() == "ok"


def test_benchmark_script_pins_both_analyzer_backends() -> None:
    content = Path("src/worktrace/benchmark_hook_vs_codex.py").read_text(encoding="utf-8")

    assert "RuntimeConfig(analyzer_backend='codex')" in content
    assert "RuntimeConfig(analyzer_backend='online')" in content
