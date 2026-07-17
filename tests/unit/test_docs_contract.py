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


def test_docs_define_retention_review_model_and_python_boundaries() -> None:
    documents = [
        Path("README.md").read_text(encoding="utf-8"),
        Path("docs/detailed-design.md").read_text(encoding="utf-8"),
        Path("docs/employee-guide.md").read_text(encoding="utf-8"),
        Path("SKILL.md").read_text(encoding="utf-8"),
    ]

    for content in documents:
        assert "config/retention_policy.json" in content
        assert "临时协作" in content
        assert "实质工作" in content
        assert "Python" in content
        assert "不" in content and "语义" in content
        assert "旧" in content and "追溯" in content
    for content in (documents[0], documents[1], documents[3]):
        assert "retention_review_summary" in content
        assert "6200" in content


def test_docs_define_personal_fact_review_evidence_boundary() -> None:
    documents = [
        Path("README.md").read_text(encoding="utf-8"),
        Path("docs/detailed-design.md").read_text(encoding="utf-8"),
        Path("docs/employee-guide.md").read_text(encoding="utf-8"),
        Path("SKILL.md").read_text(encoding="utf-8"),
    ]

    for content in documents:
        assert "fact_items" in content or "事实证据" in content
        assert "原聊天" in content
        assert "Python" in content
        assert "不" in content and (
            "事实含义" in content or "业务含义" in content
        )
        assert ("复杂" in content or "多步骤" in content) and "删除" in content
        assert "config/retention_policy.json" in content
    for content in (documents[0], documents[1], documents[3]):
        assert "fact_risk_flags" in content
        assert "personal_fact_review_summary" in content
        assert "6200" in content


def test_docs_describe_single_source_fact_review_protocol_and_concurrency() -> None:
    documents = [
        Path("README.md").read_text(encoding="utf-8"),
        Path("docs/detailed-design.md").read_text(encoding="utf-8"),
        Path("docs/online-analyzer-usage.md").read_text(encoding="utf-8"),
        Path("SKILL.md").read_text(encoding="utf-8"),
    ]

    for content in documents:
        assert "一个候选" in content or "单候选" in content
        assert "draft_id" in content
        assert "枚举" in content
        assert "外层重复" in content
        assert "3" in content and "并发" in content
        assert "supported=false" in content


def test_privacy_docs_describe_current_discovery_and_hidden_metadata() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    skill = Path("SKILL.md").read_text(encoding="utf-8")
    employee = Path("docs/employee-guide.md").read_text(encoding="utf-8")
    privacy = Path("docs/privacy-note.md").read_text(encoding="utf-8")

    for content in (skill, employee, privacy):
        assert "发过消息或做过 reaction" in content
    for content in (readme, employee, privacy):
        assert "消息证据" in content
        assert "同日会话" in content
        assert "文件标识" in content
        assert "不保存原始消息 ID" in content or "不保存原始 `om_`" in content
    assert "参与方式英文键" in readme
    assert "参与方式英文键" in employee
    assert "参与方式英文键" in privacy


def test_implementation_index_covers_personal_review_modules() -> None:
    content = Path("docs/implementation-breakdown.md").read_text(encoding="utf-8")
    detailed = Path("docs/detailed-design.md").read_text(encoding="utf-8")

    assert "_review_retention_candidates" in content
    assert "_review_personal_event_facts" in content
    assert "pipeline/retention_review.py" in content
    assert "pipeline/personal_fact_review.py" in content
    assert "config/retention_policy.json" in content
    assert "retention_review.json" in content
    assert "personal_fact_review.json" in content
    assert "个人事实复核条件" in detailed


def test_debug_docs_cover_both_review_artifacts_and_replay_summary() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    detailed = Path("docs/detailed-design.md").read_text(encoding="utf-8")
    employee = Path("docs/employee-guide.md").read_text(encoding="utf-8")
    skill = Path("SKILL.md").read_text(encoding="utf-8")

    for content in (readme, detailed, employee, skill):
        assert "retention_review.json" in content
        assert "personal_fact_review.json" in content
        assert "失败" in content
    for content in (readme, detailed, employee):
        assert "review_artifact_summary" in content
        assert "llm_usage_summary" in content
        assert "personal_fact_review_all" in content


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
    assert "来源负责人" in markdown_contract
    assert "source_report_owners" in markdown_contract


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
        assert "同日会话" in content
        assert "明确冲突" in content
        assert "合并人" in content
        assert "关系优先" in content
        assert "来源事件 ID" in content
        assert "max_model_input_tokens" in content
        assert "collected_merge_prompt_char_threshold" not in content


def test_docs_describe_two_level_collected_merge_and_full_content_grouping() -> None:
    documents = [
        Path("README.md").read_text(encoding="utf-8"),
        Path("docs/detailed-design.md").read_text(encoding="utf-8"),
        Path("docs/collected-people-merge-plan.md").read_text(encoding="utf-8"),
    ]
    improvement_plan = Path(
        "docs/two-level-collected-merge-improvement-plan.md"
    ).read_text(encoding="utf-8")

    for content in documents:
        assert "LLM 使用事件正文发现候选组" in content
        assert "轻量 LLM 发现候选组" not in content
        assert "部门负责人" in content
        assert "中心负责人" in content
        assert "来源负责人" in content
        assert "quality_summary" in content
        assert "config/collected_merge.json" in content
    assert "生产代码和文档已按本方案实现" in improvement_plan
    assert "真实多人 V2 语义效果仍需后续运行验收" in improvement_plan


def test_docs_allow_manual_personal_and_upstream_input_combination() -> None:
    documents = [
        Path("README.md").read_text(encoding="utf-8"),
        Path("docs/detailed-design.md").read_text(encoding="utf-8"),
        Path("docs/collected-people-merge-plan.md").read_text(encoding="utf-8"),
        Path("docs/two-level-collected-merge-improvement-plan.md").read_text(
            encoding="utf-8"
        ),
        Path("merge_inbox/README.md").read_text(encoding="utf-8"),
        Path("SKILL.md").read_text(encoding="utf-8"),
    ]

    for content in documents:
        assert "不拦截" in content
        assert "不提示重复来源" in content or "不写重复来源 warning" in content
    combined = "\n".join(documents)
    assert "中心目录不得同时放" not in combined
    assert "中心目录不要保留" not in combined
    assert "重复输入在模型调用前被拦截" not in combined


def test_docs_describe_coverage_review_and_python_quality_calculation() -> None:
    documents = [
        Path("README.md").read_text(encoding="utf-8"),
        Path("docs/detailed-design.md").read_text(encoding="utf-8"),
        Path("docs/collected-people-merge-plan.md").read_text(encoding="utf-8"),
        Path("docs/two-level-collected-merge-improvement-plan.md").read_text(
            encoding="utf-8"
        ),
    ]

    for content in documents:
        assert "covered_draft_ids" in content
        assert "fact_items" in content
        assert "高风险" in content
        assert "6200" in content
        assert "Python" in content and "计算" in content


def test_docs_describe_python_collected_evidence_relations() -> None:
    documents = [
        Path("README.md").read_text(encoding="utf-8"),
        Path("docs/detailed-design.md").read_text(encoding="utf-8"),
        Path("docs/collected-people-merge-plan.md").read_text(encoding="utf-8"),
    ]

    for content in documents:
        assert "evidence_relations" in content
        assert "conversation_groups" in content
        assert "Python" in content
        assert "原始" in content and "指纹" in content
        assert "完全相同" in content and "不能自动合并" in content
        assert "input_events" in content


def test_docs_describe_enhanced_debug_artifacts() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    detailed = Path("docs/detailed-design.md").read_text(encoding="utf-8")
    merge_design = Path("docs/collected-people-merge-plan.md").read_text(
        encoding="utf-8"
    )

    assert "final_events.json" in readme
    assert "final_events.json" in detailed
    assert "failure.json" in readme
    assert "failure.json" in detailed
    assert "_anchor_fallback" in readme
    assert "_anchor_fallback" in detailed
    for content in (readme, detailed, merge_design):
        assert "input_events" in content
        assert "deterministic_groups" in content
        assert "boundary_warnings" in content
        assert "source-audit.json" in content
        assert "partial_file_count" in content
        assert "WORKTRACE_COLLECTED_MERGE_RETRYABLE_ERROR_LIMIT" in content
        assert "WORKTRACE_COLLECTED_MERGE_RETRY_DELAY_SECONDS" in content


def test_docs_describe_online_template_defaults_and_necessary_names() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    online_usage = Path("docs/online-analyzer-usage.md").read_text(encoding="utf-8")
    env_example = Path(".env.example").read_text(encoding="utf-8")
    privacy = Path("docs/privacy-note.md").read_text(encoding="utf-8")
    employee_guide = Path("docs/employee-guide.md").read_text(encoding="utf-8")

    for content in (readme, online_usage, env_example):
        assert "WORKTRACE_LLM_TIMEOUT_SECONDS=1200" in content
        assert "WORKTRACE_LLM_STREAM=true" in content
        assert "WORKTRACE_LLM_REASONING_EFFORT=none" in content
    for content in (privacy, employee_guide):
        assert "参与人名单" in content
        assert "确有必要时保留姓名" in content


def test_detailed_design_is_the_current_code_source_of_truth() -> None:
    content = Path("docs/detailed-design.md").read_text(encoding="utf-8")

    assert "本文档以当前代码为准" in content
    assert "reaction" in content
    assert "ConversationSegmentUnit" in content
    assert "_analyze_anchor_fallback" in content
    assert "工作流 assignment" in content
    assert "关系优先分批" in content


def test_anchor_docs_separate_main_flow_from_experiment() -> None:
    status = Path("docs/anchor-first-implementation-breakdown.md").read_text(
        encoding="utf-8"
    )
    experiment = Path("docs/anchor-experiment-usage.md").read_text(encoding="utf-8")

    assert "已进入正式个人日报" in status
    assert "仍只属于独立实验" in status
    assert "持久化锚点级缓存" in status
    assert "独立实验入口" in experiment


def test_docs_describe_current_initial_windows_and_resume_checkpoints() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    detailed = Path("docs/detailed-design.md").read_text(encoding="utf-8")
    segmentation = Path("docs/conversation-slice-retry-design.md").read_text(
        encoding="utf-8"
    )
    implementation = Path("docs/implementation-breakdown.md").read_text(
        encoding="utf-8"
    )

    for content in (readme, detailed, segmentation, implementation):
        assert "config/conversation_window.json" in content
        assert "config/llm_retry.json" in content
        assert "pipeline/initial_windows.py" in content or "初始窗口" in content
    for content in (readme, detailed, segmentation):
        assert "私聊" in content
        assert "--resume" in content
        assert "data/cache/llm" in content
    assert "当前主链窗口为前后各 30 条消息" not in readme
    assert "当前正式 runner 使用 `before_limit=30`" not in segmentation


def test_docs_describe_attachment_file_name_and_image_privacy_boundaries() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    detailed = Path("docs/detailed-design.md").read_text(encoding="utf-8")
    employee = Path("docs/employee-guide.md").read_text(encoding="utf-8")
    privacy = Path("docs/privacy-note.md").read_text(encoding="utf-8")

    for content in (readme, detailed):
        assert "附件文件名" in content
        assert "不能" in content and "推断" in content and "正文" in content
        assert "无效附件引用" in content
    for content in (employee, privacy):
        assert "附件文件名" in content
        assert "本人发送" in content
        assert "reply/quote" in content


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
