from __future__ import annotations

from src.worktrace.config import RuntimeConfig
from src.worktrace.cli import _clear_previous_personal_run
from src.worktrace.models import SegmentAnalysisBatch
from src.worktrace.pipeline.llm_checkpoints import LLMCheckpointStore


def test_analysis_checkpoint_round_trips_and_clears(tmp_path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    store = LLMCheckpointStore(config, "2026-07-13")
    batch = SegmentAnalysisBatch(
        target_date="2026-07-13",
        conversation_id="oc_1",
        conversation_name="",
        self_open_id="ou_self",
        self_display_name="本人",
        segments=[],
    )

    assert store.load_analysis(batch) is None
    store.save_analysis(batch, [], ["已完成"], 2)
    assert store.load_analysis(batch) == ([], ["已完成"], 2)

    store.clear()
    assert store.load_analysis(batch) is None


def test_default_rerun_cleanup_removes_personal_markdown_and_checkpoints(tmp_path) -> None:
    config = RuntimeConfig(data_root=tmp_path / "data")
    store = LLMCheckpointStore(config, "2026-07-13")
    batch = SegmentAnalysisBatch(
        target_date="2026-07-13",
        conversation_id="oc_1",
        conversation_name="",
        self_open_id="ou_self",
        self_display_name="本人",
        segments=[],
    )
    store.save_analysis(batch, [], [], 0)
    markdown_path = config.data_root / "2026" / "07" / "2026-07-13-本人.md"
    markdown_path.parent.mkdir(parents=True)
    markdown_path.write_text("old", encoding="utf-8")

    _clear_previous_personal_run(config, "2026-07-13")

    assert not markdown_path.exists()
    assert store.load_analysis(batch) is None
