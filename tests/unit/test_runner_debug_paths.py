from src.worktrace.runner import _safe_segment_batch_dir_name


def test_safe_segment_batch_dir_name_shortens_long_value_without_collisions() -> None:
    shared_prefix = "anchor-001__turn-002-" * 20

    first = _safe_segment_batch_dir_name(shared_prefix + "first")
    second = _safe_segment_batch_dir_name(shared_prefix + "second")

    assert len(first) <= 96
    assert len(second) <= 96
    assert first != second
