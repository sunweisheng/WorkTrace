from src.worktrace.utils.token_estimation import estimate_text_tokens


def test_estimate_text_tokens_uses_shared_worktrace_formula() -> None:
    assert estimate_text_tokens("") == 50
    assert estimate_text_tokens("a" * 300) == 150
