from __future__ import annotations

import re
from pathlib import Path


LOCAL_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\((?!https?://|mailto:)([^)#]+)(?:#[^)]+)?\)")


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
    assert "普通事件排除" in content


def test_readme_describes_current_segmented_personal_flow() -> None:
    content = Path("README.md").read_text(encoding="utf-8")

    assert "本人发言和本人 reaction" in content
    assert "segment_start_message_ids" in content
    assert "SegmentAnalysisBatch" in content
    assert "workstream_key" in content
    assert "config/image_summary.json" in content
    assert "WORKTRACE_COLLECTED_MERGE_TRACE" in content
    assert "一个会话 = 一个 slice = 一次首轮 LLM" not in content


def test_docs_describe_event_metadata_and_markdown_compatibility() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    detailed = Path("docs/detailed-design.md").read_text(encoding="utf-8")
    markdown_contract = Path(
        "docs/markdown-output-simplification-design.md"
    ).read_text(encoding="utf-8")

    for content in (readme, detailed, markdown_contract):
        assert "工作流" in content
        assert "主要动作" in content
        assert "本人参与方式" in content
        assert "merge_meta" in content
        assert "SHA-256" in content
        assert "未明确" in content
    assert "config/event_metadata.json" in readme
    assert "旧 Markdown" in detailed
    assert "不批量改写历史文件" in markdown_contract


def test_docs_define_collected_merge_boundaries_and_conflict_priority() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    merge_design = Path("docs/collected-people-merge-plan.md").read_text(
        encoding="utf-8"
    )

    for content in (readme, merge_design):
        assert "工作流相同" in content
        assert "不能单独" in content or "不能直接" in content
        assert "共同消息" in content
        assert "共同文件" in content
        assert "明确冲突" in content
        assert "合并人" in content
        assert "滚动合并" in content
        assert "来源事件 ID" in content


def test_docs_describe_enhanced_debug_artifacts() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    detailed = Path("docs/detailed-design.md").read_text(encoding="utf-8")
    merge_design = Path("docs/collected-people-merge-plan.md").read_text(
        encoding="utf-8"
    )

    assert "final_events.json" in readme
    assert "final_events.json" in detailed
    for content in (readme, detailed, merge_design):
        assert "input_events" in content
        assert "deterministic_groups" in content
        assert "boundary_warnings" in content


def test_detailed_design_is_the_current_code_source_of_truth() -> None:
    content = Path("docs/detailed-design.md").read_text(encoding="utf-8")

    assert "本文档以当前代码为准" in content
    assert "reaction" in content
    assert "ConversationSegmentUnit" in content
    assert "_analyze_anchor_fallback" in content
    assert "工作流 assignment" in content
    assert "滚动合并" in content


def test_anchor_docs_separate_main_flow_from_experiment() -> None:
    status = Path("docs/anchor-first-implementation-breakdown.md").read_text(
        encoding="utf-8"
    )
    experiment = Path("docs/anchor-experiment-usage.md").read_text(encoding="utf-8")

    assert "已进入正式个人日报" in status
    assert "仍只属于独立实验" in status
    assert "持久化锚点级缓存" in status
    assert "独立实验入口" in experiment


def test_readme_indexes_every_docs_markdown() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    for path in sorted(Path("docs").glob("*.md")):
        assert path.as_posix() in readme, f"README is missing docs index entry: {path}"


def test_local_markdown_links_exist() -> None:
    markdown_paths = [Path("README.md"), *sorted(Path("docs").glob("*.md"))]

    for source_path in markdown_paths:
        content = source_path.read_text(encoding="utf-8")
        for target in LOCAL_MARKDOWN_LINK_RE.findall(content):
            resolved = (source_path.parent / target).resolve()
            assert resolved.exists(), f"Broken local link in {source_path}: {target}"


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
