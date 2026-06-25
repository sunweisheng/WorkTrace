from __future__ import annotations

from pathlib import Path

from src.worktrace.benchmark_hook_hello import (
    _build_prompt,
    _build_stats,
    _run_once,
)


def test_build_prompt_mentions_user_text() -> None:
    prompt = _build_prompt("你好")

    assert "用户对你说：你好" in prompt
    assert "reply" in prompt


def test_build_stats_summarizes_runs() -> None:
    stats = _build_stats(
        [
            {"elapsed_seconds": 1.2, "returncode": 0},
            {"elapsed_seconds": 2.4, "returncode": 1},
            {"elapsed_seconds": 1.8, "returncode": 0},
        ]
    )

    assert stats == {
        "success_count": 2,
        "failure_count": 1,
        "avg_elapsed_seconds": 1.8,
        "min_elapsed_seconds": 1.2,
        "max_elapsed_seconds": 2.4,
    }


def test_run_once_passes_schema_path_and_parses_stdout(tmp_path: Path) -> None:
    captured_env: dict[str, str] = {}

    def fake_runner(args, *, cwd, timeout, input_text, env):
        nonlocal captured_env
        captured_env = dict(env)

        class Result:
            returncode = 0
            stdout = '{"reply":"你好。"}'
            stderr = ""

        return Result()

    result = _run_once(
        run_index=1,
        hook_command="mock-hook --flag",
        prompt="test prompt",
        output_schema={"type": "object"},
        cwd=tmp_path,
        timeout_seconds=30,
        command_runner=fake_runner,
    )

    assert result["run"] == 1
    assert result["returncode"] == 0
    assert result["parsed"] == {"reply": "你好。"}
    assert captured_env["WORKTRACE_HOOK_SCHEMA_PATH"].endswith(".json")
    assert not Path(captured_env["WORKTRACE_HOOK_SCHEMA_PATH"]).exists()
