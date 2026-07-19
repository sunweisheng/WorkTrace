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
    record = recorder.records()[0]
    assert record["request_kind"] == "segment_batch_analysis"
    assert record["backend"] == "online"
    assert record["status"] == "success"
    assert record["duration_ms"] == 1234.5
    assert record["prompt_chars"] == 456
    assert record["input_tokens"] == 100
    assert record["output_tokens"] == 20
    assert record["total_tokens"] == 120


def test_usage_recorder_marks_codex_tokens_unavailable() -> None:
    recorder = LLMUsageRecorder()
    recorder.record(
        "collected_event_merge",
        {},
        backend="codex",
        duration_ms=1200,
        codex_wait_ms=100,
    )

    record = recorder.records()[0]

    assert record["token_usage_status"] == "unavailable"
    assert recorder.summary()["by_backend"]["codex"]["success_count"] == 1
    assert recorder.summary()["codex_wait_ms"]["total"] == 100
