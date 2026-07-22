from __future__ import annotations

import json


JSON_SCHEMA_OUTPUT_NAME = "worktrace_output"


def estimate_text_tokens(value: str) -> int:
    return max(1, len(value) // 3 + 50)


def prepare_model_prompt(prompt: str, *, append_no_think: bool) -> str:
    prepared = prompt.rstrip()
    if append_no_think and not prepared.endswith("/no_think"):
        prepared = f"{prepared}\n/no_think"
    return prepared


def build_structured_output_text_config(
    output_schema: dict[str, object],
) -> dict[str, object]:
    return {
        "format": {
            "type": "json_schema",
            "name": JSON_SCHEMA_OUTPUT_NAME,
            "schema": output_schema,
            "strict": True,
        }
    }


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
                {"text": build_structured_output_text_config(output_schema)},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    return estimate_text_tokens("\n".join(input_parts))
