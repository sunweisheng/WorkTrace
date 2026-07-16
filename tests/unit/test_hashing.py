from src.worktrace.utils.hashing import conversation_fingerprint


def test_conversation_fingerprint_is_stable_within_day_and_changes_across_days() -> None:
    first = conversation_fingerprint("2026-07-14", "oc_shared")

    assert first == conversation_fingerprint("2026-07-14", "oc_shared")
    assert first != conversation_fingerprint("2026-07-15", "oc_shared")
    assert first != conversation_fingerprint("2026-07-14", "oc_other")
    assert first.startswith("sha256:")
    assert "oc_shared" not in first


def test_conversation_fingerprint_rejects_empty_inputs() -> None:
    assert conversation_fingerprint("", "oc_shared") == ""
    assert conversation_fingerprint("2026-07-14", "") == ""
