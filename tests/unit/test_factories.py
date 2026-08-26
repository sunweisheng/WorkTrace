from __future__ import annotations

from pathlib import Path

import pytest

from src.worktrace.analyzers.base import Analyzer
from src.worktrace.analyzers.failover import FailoverAnalyzer
from src.worktrace.config import RuntimeConfig
from src.worktrace.delivery.base import DeliveryChannel
from src.worktrace.factories import build_runtime_dependencies
from src.worktrace.resolvers.base import ContentResolver
from src.worktrace.sources.base import ChatSource
from src.worktrace.stores.base import EventStore


def test_build_runtime_dependencies_returns_interface_instances(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        primary_request_retry_limit=2,
    )
    runtime = build_runtime_dependencies(config)

    assert isinstance(runtime.chat_source, ChatSource)
    assert isinstance(runtime.content_resolver, ContentResolver)
    assert isinstance(runtime.analyzer, Analyzer)
    assert isinstance(runtime.analyzer, FailoverAnalyzer)
    assert runtime.analyzer.primary_request_retry_limit == 2
    assert isinstance(runtime.delivery_channel, DeliveryChannel)
    assert isinstance(runtime.event_store, EventStore)


def test_failover_retries_codex_before_using_online() -> None:
    from src.worktrace.errors import RetryableAnalyzerProtocolError
    from src.worktrace.llm_usage import LLMUsageRecorder

    class Codex:
        def __init__(self) -> None:
            self.calls = 0

        def analyze_batch(self, target_date, batch_input):
            self.calls += 1
            if self.calls <= 2:
                raise RetryableAnalyzerProtocolError("Request timed out.")
            return "codex-result"

    class Online:
        def __init__(self) -> None:
            self.calls = 0

        def analyze_batch(self, target_date, batch_input):
            self.calls += 1
            return "online-result"

    recorder = LLMUsageRecorder()
    codex = Codex()
    online = Online()
    analyzer = FailoverAnalyzer(
        primary=codex,
        fallback=online,
        usage_recorder=recorder,
    )

    assert analyzer.analyze_batch("2026-07-17", object()) == "online-result"
    assert analyzer.last_request_used_fallback() is True
    assert analyzer.last_request_backend() == "online"
    assert analyzer.analyze_batch("2026-07-17", object()) == "codex-result"
    assert analyzer.last_request_used_fallback() is False
    assert analyzer.last_request_backend() == "codex"
    assert codex.calls == 3
    assert online.calls == 1
    first_failure, second_failure = recorder.records()
    assert first_failure["backend"] == "codex"
    assert first_failure["status"] == "failed"
    assert first_failure["fallback_from"] is None
    assert first_failure["fallback_to"] is None
    assert second_failure["fallback_from"] == "codex"
    assert second_failure["fallback_to"] == "online"
    assert recorder.summary()["fallback_count"] == 1


def test_failover_does_not_use_online_when_codex_retry_succeeds() -> None:
    from src.worktrace.errors import RetryableAnalyzerProtocolError
    from src.worktrace.llm_usage import LLMUsageRecorder

    class Codex:
        def __init__(self) -> None:
            self.calls = 0

        def analyze_batch(self, target_date, batch_input):
            self.calls += 1
            if self.calls == 1:
                raise RetryableAnalyzerProtocolError("Request timed out.")
            return "codex-result"

    class Online:
        def analyze_batch(self, target_date, batch_input):
            raise AssertionError("Online must not run")

    recorder = LLMUsageRecorder()
    codex = Codex()
    analyzer = FailoverAnalyzer(
        primary=codex,
        fallback=Online(),
        usage_recorder=recorder,
    )

    assert analyzer.analyze_batch("2026-07-17", object()) == "codex-result"
    assert analyzer.last_request_used_fallback() is False
    assert codex.calls == 2
    assert recorder.records()[0]["fallback_to"] is None


def test_explicit_current_request_fallback_does_not_change_the_next_route() -> None:
    from src.worktrace.llm_usage import LLMUsageRecorder

    class Codex:
        def __init__(self, recorder: LLMUsageRecorder) -> None:
            self.calls = 0
            self.recorder = recorder

        def analyze_batch(self, target_date, batch_input):
            self.calls += 1
            self.recorder.record("batch_analysis", {}, backend="codex")
            return f"codex-result-{self.calls}"

    class Online:
        def __init__(self) -> None:
            self.calls = 0

        def analyze_batch(self, target_date, batch_input):
            self.calls += 1
            return "online-result"

    recorder = LLMUsageRecorder()
    codex = Codex(recorder)
    online = Online()
    analyzer = FailoverAnalyzer(
        primary=codex,
        fallback=online,
        usage_recorder=recorder,
    )

    with recorder.request_context("codex-retry"):
        assert analyzer.analyze_batch("2026-07-21", object()) == "codex-result-1"
    with recorder.request_context("online-fallback"):
        assert (
            analyzer.fallback_current_request(
                "analyze_batch",
                "2026-07-21",
                object(),
                failed_request_context_id="codex-retry",
                error_category="python_validation_failed",
            )
            == "online-result"
        )

    failed_codex_record = recorder.records()[0]
    assert failed_codex_record["status"] == "failed"
    assert failed_codex_record["fallback_from"] == "codex"
    assert failed_codex_record["fallback_to"] == "online"
    assert failed_codex_record["error_category"] == "python_validation_failed"
    assert analyzer.last_request_used_fallback() is True

    assert analyzer.analyze_batch("2026-07-21", object()) == "codex-result-2"
    assert analyzer.last_request_used_fallback() is False
    assert codex.calls == 2
    assert online.calls == 1


def test_failover_does_not_switch_permanent_online_errors() -> None:
    from src.worktrace.errors import AnalyzerProtocolError
    from src.worktrace.llm_usage import LLMUsageRecorder

    class Online:
        def __init__(self) -> None:
            self.calls = 0

        def analyze_batch(self, target_date, batch_input):
            self.calls += 1
            raise AnalyzerProtocolError("HTTP 401: invalid API key")

    class Codex:
        def analyze_batch(self, target_date, batch_input):
            raise AssertionError("Codex must not run")

    recorder = LLMUsageRecorder()
    online = Online()
    analyzer = FailoverAnalyzer(
        primary=online,
        fallback=Codex(),
        usage_recorder=recorder,
    )

    with pytest.raises(AnalyzerProtocolError, match="HTTP 401"):
        analyzer.analyze_batch("2026-07-17", object())

    assert recorder.records()[0]["error_category"] == "authentication"
    assert online.calls == 1


def test_failover_does_not_classify_grouping_connection_evidence_as_network() -> None:
    from src.worktrace.errors import PersonalGroupingValidationError
    from src.worktrace.llm_usage import LLMUsageRecorder

    class Online:
        def merge_day_candidates(
            self,
            target_date,
            candidates,
            *,
            validation_feedback="",
        ):
            raise PersonalGroupingValidationError(
                "member_connection_evidence_invalid field=merged_groups[0]"
            )

    class Codex:
        def merge_day_candidates(self, *args, **kwargs):
            raise AssertionError("Codex must not run")

    recorder = LLMUsageRecorder()
    analyzer = FailoverAnalyzer(
        primary=Online(),
        fallback=Codex(),
        usage_recorder=recorder,
    )

    with pytest.raises(
        PersonalGroupingValidationError,
        match="member_connection_evidence_invalid",
    ):
        analyzer.merge_day_candidates("2026-07-23", [object()])

    assert recorder.records()[0]["error_category"] == "invalid_protocol"


def test_failover_does_not_switch_model_input_limit_errors() -> None:
    from src.worktrace.errors import ModelInputLimitError
    from src.worktrace.llm_usage import LLMUsageRecorder

    class Online:
        def analyze_batch(self, target_date, batch_input):
            raise ModelInputLimitError(
                "Model input exceeds model_input_batch_target_tokens"
            )

    class Codex:
        def __init__(self) -> None:
            self.calls = 0

        def analyze_batch(self, target_date, batch_input):
            self.calls += 1
            return "codex-result"

    codex = Codex()
    analyzer = FailoverAnalyzer(
        primary=Online(),
        fallback=codex,
        usage_recorder=LLMUsageRecorder(),
    )

    with pytest.raises(ModelInputLimitError, match="model_input_batch_target_tokens"):
        analyzer.analyze_batch("2026-07-17", object())

    assert codex.calls == 0


def test_failover_records_provider_input_rejection_without_switching() -> None:
    from src.worktrace.errors import ModelInputRejectedError
    from src.worktrace.llm_usage import LLMUsageRecorder

    class Online:
        def analyze_batch(self, target_date, batch_input):
            error = ModelInputRejectedError("HTTP 400: model input rejected")
            error.estimated_input_tokens = 7_596
            error.input_target_tokens = 5_200
            error.oversized_singleton = True
            raise error

    class Codex:
        def analyze_batch(self, target_date, batch_input):
            raise AssertionError("Codex must not run")

    recorder = LLMUsageRecorder()
    analyzer = FailoverAnalyzer(
        primary=Online(),
        fallback=Codex(),
        usage_recorder=recorder,
    )

    with pytest.raises(ModelInputRejectedError, match="HTTP 400"):
        analyzer.analyze_batch("2026-07-20", object())

    record = recorder.records()[0]
    assert record["error_category"] == "request_rejected"
    assert record["estimated_input_tokens"] == 7_596
    assert record["input_target_tokens"] == 5_200
    assert record["input_target_overage_tokens"] == 2_396
    assert record["oversized_singleton"] is True


def test_runtime_config_defaults_to_codex_backend() -> None:
    config = RuntimeConfig()

    assert config.analyzer_backend == "codex"
    assert config.primary_request_retry_limit == 1
    assert config.online_request_retry_limit == 1
    assert config.llm_tls_verify is False
    assert config.codex_request_interval_min_seconds == 0.0
    assert config.codex_request_interval_max_seconds == 1.0


def test_build_runtime_dependencies_supports_online_analyzer(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_root=tmp_path / "data",
        analyzer_backend="online",
    )
    runtime = build_runtime_dependencies(config)

    assert isinstance(runtime.analyzer, Analyzer)
