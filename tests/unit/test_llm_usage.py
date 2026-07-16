from src.worktrace.llm_usage import LLMUsageRecorder, extract_usage


def test_extract_usage_reads_responses_and_chat_compatible_fields() -> None:
    assert extract_usage(
        {"usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}}
    ) == {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}
    assert extract_usage(
        {"response": {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}}
    ) == {"input_tokens": 3, "output_tokens": 2, "total_tokens": None}


def test_usage_recorder_reports_output_tokens_and_missing_values() -> None:
    recorder = LLMUsageRecorder()
    recorder.record(
        "segment_batch_analysis",
        {"usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}},
        duration_ms=1234.5,
        prompt_chars=456,
    )
    recorder.record("image_summary", {})

    summary = recorder.summary()

    assert summary["request_count"] == 2
    assert summary["output_tokens"] == 20
    assert summary["reported_output_tokens_request_count"] == 1
    assert summary["missing_output_tokens_request_count"] == 1
    assert summary["by_request_kind"]["segment_batch_analysis"]["output_tokens"] == 20
    assert summary["by_request_kind"]["image_summary"]["missing_output_tokens_request_count"] == 1
    assert recorder.records()[0] == {
        "request_kind": "segment_batch_analysis",
        "duration_ms": 1234.5,
        "prompt_chars": 456,
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
    }
