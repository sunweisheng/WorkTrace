from src.worktrace.utils.token_estimation import (
    build_structured_output_text_config,
    estimate_model_input_tokens,
    estimate_text_tokens,
    prepare_model_prompt,
)


def test_estimate_text_tokens_uses_shared_worktrace_formula() -> None:
    assert estimate_text_tokens("") == 50
    assert estimate_text_tokens("a" * 300) == 150


def test_estimate_model_input_tokens_includes_prompt_marker_and_schema() -> None:
    prompt = "处理输入"
    schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
        "additionalProperties": False,
    }

    prompt_only = estimate_model_input_tokens(prompt, append_no_think=True)
    full_input = estimate_model_input_tokens(
        prompt,
        output_schema=schema,
        append_no_think=True,
    )

    assert prepare_model_prompt(prompt, append_no_think=True).endswith("/no_think")
    assert build_structured_output_text_config(schema)["format"]["schema"] == schema
    assert full_input > prompt_only
