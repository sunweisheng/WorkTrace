from __future__ import annotations

from pathlib import Path


def test_env_example_contains_required_online_llm_keys() -> None:
    content = Path(".env.example").read_text(encoding="utf-8")

    assert "WORKTRACE_LLM_BASE_URL=" in content
    assert "WORKTRACE_LLM_MODEL=" in content
    assert "WORKTRACE_LLM_API_KEY=" in content
    assert "WORKTRACE_LLM_REASONING_EFFORT=none" in content


def test_readme_mentions_local_online_llm_configuration() -> None:
    content = Path("README.md").read_text(encoding="utf-8")

    assert "本地私有模型配置" in content
    assert "WORKTRACE_LLM_BASE_URL" in content
    assert "不能和代码一起提交到 git" in content
    assert "/no_think" in content


def test_readme_mentions_event_rules_file() -> None:
    content = Path("README.md").read_text(encoding="utf-8")

    assert "config/event_rules.json" in content
    assert "精确排除" in content


def test_readme_mentions_quick_usage_examples() -> None:
    content = Path("README.md").read_text(encoding="utf-8")

    assert "快速使用说明" in content
    assert "帮我生成 2026-07-06 的个人事件MD" in content
    assert "帮我合并 2026-07-06 的部门事件MD" in content
    assert "YYYY-MM-DD-登录人姓名-merged.md" in content


def test_readme_and_skill_no_longer_mention_merge_drive_upload() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    skill = Path("SKILL.md").read_text(encoding="utf-8")

    assert "飞书 Drive" not in readme
    assert "merge_delivery.local.json" not in readme
    assert "上传 Drive" not in skill
    assert "管理汇总模式不自送达给当前用户" not in skill


def test_skill_mentions_first_run_configuration_requirement() -> None:
    content = Path("SKILL.md").read_text(encoding="utf-8")

    assert "每次使用前" in content
    assert "必须先检查用户是否已经提供本地在线模型配置" in content
    assert "WORKTRACE_LLM_API_KEY" in content
    assert "不能提交到 git 仓库" in content
    assert "/no_think" in content
    assert "管理人员得到规范化的 `YYYY-MM-DD-登录人姓名-merged.md`" in content
