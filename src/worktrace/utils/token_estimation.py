from __future__ import annotations

import json

from ..analyzers.function_calls import FunctionCallSpec


def estimate_text_tokens(value: str) -> int:
    return max(1, len(value) // 3 + 50)


def prepare_model_prompt(prompt: str, *, append_no_think: bool) -> str:
    prepared = prompt.rstrip()
    if append_no_think and not prepared.endswith("/no_think"):
        prepared = f"{prepared}\n/no_think"
    return prepared


def estimate_model_input_tokens(
    prompt: str,
    *,
    output_schema: dict[str, object] | None = None,
    append_no_think: bool = False,
) -> int:
    prepared_prompt = prepare_model_prompt(
        prompt,
        append_no_think=append_no_think,
    )
    input_parts = [prepared_prompt]
    if output_schema is not None:
        input_parts.append(
            json.dumps(
                {"output_schema": output_schema},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    return estimate_text_tokens("\n".join(input_parts))


def estimate_function_input_tokens(
    prompt: str,
    *,
    function_spec: FunctionCallSpec,
    append_no_think: bool = False,
) -> int:
    prepared_prompt = prepare_model_prompt(
        function_spec.prompt_with_example(prompt),
        append_no_think=append_no_think,
    )
    function_input = json.dumps(
        {
            "tools": [function_spec.tool()],
            "tool_choice": function_spec.tool_choice(),
            "parallel_tool_calls": False,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return estimate_text_tokens(f"{prepared_prompt}\n{function_input}")


def estimate_codex_schema_input_tokens(
    prompt: str,
    *,
    function_spec: FunctionCallSpec,
    append_no_think: bool = False,
) -> int:
    prepared_prompt = prepare_model_prompt(
        function_spec.prompt_with_example(prompt),
        append_no_think=append_no_think,
    )
    schema_input = json.dumps(
        {"output_schema": function_spec.parameters},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return estimate_text_tokens(f"{prepared_prompt}\n{schema_input}")


def estimate_structured_input_tokens(
    prompt: str,
    *,
    function_spec: FunctionCallSpec,
    append_no_think: bool = False,
) -> dict[str, int]:
    prepared_prompt = function_spec.prompt_with_example(prompt)
    prompt_estimate = estimate_text_tokens(
        prepare_model_prompt(prepared_prompt, append_no_think=append_no_think)
    )
    online_estimate = estimate_function_input_tokens(
        prompt,
        function_spec=function_spec,
        append_no_think=append_no_think,
    )
    codex_estimate = estimate_codex_schema_input_tokens(
        prompt,
        function_spec=function_spec,
        append_no_think=append_no_think,
    )
    return {
        "prompt_estimated_tokens": prompt_estimate,
        "online_input_estimated_tokens": online_estimate,
        "codex_input_estimated_tokens": codex_estimate,
        "input_estimated_tokens": max(online_estimate, codex_estimate),
    }
