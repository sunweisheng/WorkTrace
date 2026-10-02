from __future__ import annotations

def test_wait_progress_has_stage_count_and_elapsed_without_payload(capsys, monkeypatch):
    import io
    import sys
    from threading import Event
    from src.worktrace.progress import ProgressReporter, report_stage, report_completed

    observed = Event()

    class CaptureStream(io.StringIO):
        def write(self, text):
            result = super().write(text)
            if "1/4" in text:
                observed.set()
            return result

    stream = CaptureStream()
    monkeypatch.setattr(sys, "stderr", stream)
    with ProgressReporter("personal", interval_seconds=0.01):
        report_stage("candidate_generation", total=4)
        report_completed()
        assert observed.wait(1)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "提炼事件" in stream.getvalue()
    assert "1/4" in stream.getvalue()
    assert "已耗时" in stream.getvalue()
    assert "ETA" not in stream.getvalue()


def test_reporter_stops_after_exception():
    from src.worktrace.progress import ProgressReporter

    progress = ProgressReporter("collected", interval_seconds=0.01)
    try:
        with progress:
            raise ValueError("synthetic")
    except ValueError:
        pass
    assert not progress.thread.is_alive()


def test_personal_result_exports_safe_warnings_and_stage_timings():
    from dataclasses import replace
    from src.worktrace.cli import build_failed_result
    from src.worktrace.models import DailyRunResult

    result = replace(build_failed_result("2026-10-01", "legacy warning"),
        status="success_with_warnings", warning_count=1, skipped_slice_count=1,
        warning_messages=["Segment expansion produced no new context synthetic-id"],
        stage_timing_summary={"total": {"wall_clock_ms": 100.0,
                                        "request_accumulated_ms": 90.0}})
    payload = result.to_dict()
    assert payload["error_summary"] == "legacy warning"
    assert payload["warnings"][0]["code"] == "segment_context_missing"
    assert "跳过" in payload["warnings"][0]["summary"]
    assert "synthetic-id" not in repr(payload["warnings"])
    assert payload["stage_timing_summary"]["total"]["wall_clock_ms"] == 100.0
    assert DailyRunResult.from_dict(payload).to_dict() == payload


def test_collected_result_keeps_legacy_messages_and_adds_safe_warnings():
    from src.worktrace.models import CollectedMergeRunResult

    result = CollectedMergeRunResult("success_with_warnings", "2026-10-01",
        "synthetic", "synthetic.md", 1, 1, 1, 0,
        warning_messages=["delivery synthetic-id"])
    payload = result.to_dict()
    assert payload["warning_messages"] == ["delivery synthetic-id"]
    assert payload["warnings"][0]["stage"] == "self_delivery"


def test_segmentation_fallback_is_not_reported_as_lost_context():
    from src.worktrace.runtime_diagnostics import structured_warnings

    warnings = structured_warnings([
        "Skipped anchor after invalid segmentation retries.",
        "Skipped anchor fallback after repeated batch failures.",
    ])
    assert warnings[0]["code"] == "segmentation_fallback"
    assert "回退" in warnings[0]["summary"]
    assert warnings[1]["code"] == "segment_skipped"


def test_known_delivery_error_has_correct_stage_without_matching_keywords():
    from dataclasses import replace
    from src.worktrace.cli import build_failed_result

    result = replace(build_failed_result("2026-10-01", "403 Forbidden"),
        status="success_with_warnings", warning_count=1,
        warning_messages=["403 Forbidden"], self_delivery_status="failed",
        self_delivery_error="403 Forbidden")
    assert result.to_dict()["warnings"][0]["stage"] == "self_delivery"
