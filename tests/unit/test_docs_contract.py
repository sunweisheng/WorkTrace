from __future__ import annotations

from pathlib import Path


def test_env_example_contains_required_online_llm_keys() -> None:
    content = Path(".env.example").read_text(encoding="utf-8")

    assert "WORKTRACE_LLM_BASE_URL=" in content
    assert "WORKTRACE_LLM_MODEL=" in content
    assert "WORKTRACE_LLM_API_KEY=" in content


def test_readme_mentions_local_online_llm_configuration() -> None:
    content = Path("README.md").read_text(encoding="utf-8")

    assert "本地私有模型配置" in content
    assert "WORKTRACE_LLM_BASE_URL" in content
    assert "不能和代码一起提交到 git" in content


def test_skill_mentions_first_run_configuration_requirement() -> None:
    content = Path("SKILL.md").read_text(encoding="utf-8")

    assert "首次使用前" in content
    assert "WORKTRACE_LLM_API_KEY" in content
    assert "不能提交到 git 仓库" in content
