from src.worktrace.analyzers.function_calls import function_call_spec
from src.worktrace.utils.token_estimation import (
    estimate_codex_schema_input_tokens,
    estimate_function_input_tokens,
    estimate_model_input_tokens,
    estimate_structured_input_tokens,
    estimate_text_tokens,
    prepare_model_prompt,
)


def test_estimate_text_tokens_uses_shared_worktrace_formula() -> None:
    assert estimate_text_tokens("") == 200
    assert estimate_text_tokens("a" * 300) == 325
    assert estimate_text_tokens("中" * 300) == 500


def test_estimate_structured_input_tokens_includes_function_and_codex_schema() -> None:
    prompt = "处理输入"
    schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
        "additionalProperties": False,
    }

    prompt_only = estimate_model_input_tokens(prompt, append_no_think=True)
    function_spec = function_call_spec(
        "preflight",
        schema,
        typical_arguments={"result": "ok"},
    )
    online_input = estimate_function_input_tokens(
        prompt,
        function_spec=function_spec,
        append_no_think=True,
    )
    codex_input = estimate_codex_schema_input_tokens(
        prompt,
        function_spec=function_spec,
        append_no_think=True,
    )
    estimates = estimate_structured_input_tokens(
        prompt,
        function_spec=function_spec,
        append_no_think=True,
    )

    assert prepare_model_prompt(prompt, append_no_think=True).endswith("/no_think")
    assert online_input > prompt_only
    assert codex_input > prompt_only
    assert estimates["online_input_estimated_tokens"] == online_input
    assert estimates["codex_input_estimated_tokens"] == codex_input
    assert estimates["input_estimated_tokens"] == max(online_input, codex_input)
