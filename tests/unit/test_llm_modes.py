from pathlib import Path

import pytest

from src.worktrace.config import RuntimeConfig, load_runtime_config_overrides
from src.worktrace.factories import AnalyzerFactory
from src.worktrace.llm_usage import LLMUsageRecorder
from src.worktrace.errors import (
    AnalyzerProtocolError,
    RetryableAnalyzerProtocolError,
)
from src.worktrace.analyzers.function_calls import function_call_spec


def online_env(root):
    (root / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://example.invalid/v1\n"
        "WORKTRACE_LLM_MODEL=test-model\n"
        "WORKTRACE_LLM_API_KEY=test-key\n"
    )


@pytest.mark.parametrize("value", ["codex_with_fallback", "online_only"])
def test_mode_from_dotenv(tmp_path, monkeypatch, value):
    monkeypatch.delenv("WORKTRACE_LLM_MODE", raising=False)
    (tmp_path / ".env").write_text(f"WORKTRACE_LLM_MODE={value}\n")
    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)
    assert config.llm_mode == value


def test_mode_defaults_and_environment_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKTRACE_LLM_MODE", raising=False)
    assert (
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path).llm_mode
        == "codex_with_fallback"
    )
    (tmp_path / ".env").write_text("WORKTRACE_LLM_MODE=codex_with_fallback\n")
    monkeypatch.setenv("WORKTRACE_LLM_MODE", "online_only")
    assert (
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path).llm_mode
        == "online_only"
    )


@pytest.mark.parametrize("value", ["unknown", ""])
def test_invalid_mode_is_rejected(tmp_path, monkeypatch, value):
    monkeypatch.setenv("WORKTRACE_LLM_MODE", value)
    with pytest.raises(ValueError, match="WORKTRACE_LLM_MODE"):
        load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)


def forbid_codex(*args, **kwargs):
    raise AssertionError("Codex must not be touched")


@pytest.mark.parametrize("retryable, expected", [(True, 2), (False, 1)])
def test_online_primary_retries_without_fallback(
    tmp_path, monkeypatch, retryable, expected
):
    online_env(tmp_path)
    monkeypatch.setattr(
        "src.worktrace.analyzers.codex.CodexAnalyzer", forbid_codex
    )
    monkeypatch.setattr(
        "src.worktrace.config.load_codex_llm_settings", forbid_codex
    )
    calls = []
    error_type = (
        RetryableAnalyzerProtocolError if retryable else AnalyzerProtocolError
    )

    def request(*args, **kwargs):
        calls.append(1)
        raise error_type("Request timed out." if retryable else "HTTP 401")

    monkeypatch.setattr(
        "src.worktrace.analyzers.online.OnlineLLMAnalyzer.request_function",
        request,
    )
    recorder = LLMUsageRecorder()
    analyzer = AnalyzerFactory.create_default(
        RuntimeConfig(llm_mode="online_only"),
        cwd=tmp_path,
        usage_recorder=recorder,
    )
    spec = function_call_spec(
        "preflight",
        {
            "type": "object",
            "properties": {"probe": {"type": "string"}},
            "required": ["probe"],
            "additionalProperties": False,
        },
    )
    with pytest.raises(error_type):
        analyzer.request_function("test", function_spec=spec)
    assert len(calls) == expected
    assert analyzer.last_request_backend() == "online"
    assert not analyzer.last_request_used_fallback()
    assert not analyzer.supports_current_request_fallback()
    assert recorder.summary()["fallback_count"] == 0


def test_online_preflight_skips_codex(tmp_path, monkeypatch):
    from src.worktrace.preflight import run_preflight_checks, CommandResult

    online_env(tmp_path)
    monkeypatch.setattr(
        "src.worktrace.preflight.shutil.which",
        lambda name: "/bin/lark-cli" if name == "lark-cli" else None,
    )
    monkeypatch.setattr(
        "src.worktrace.preflight.load_codex_llm_settings", forbid_codex
    )
    monkeypatch.setattr("src.worktrace.preflight.probe_codex", forbid_codex)
    monkeypatch.setattr(
        "src.worktrace.preflight.probe_online_llm",
        lambda *a, **k: {"online_probe": "ok"},
    )

    def runner(args):
        assert tuple(args) == ("lark-cli", "auth", "status")
        return CommandResult(
            0,
            '{"identity":"user","identities":{"user":{"available":true,"openId":"ou_test"}}}',
            "",
        )

    report = run_preflight_checks(
        RuntimeConfig(llm_mode="online_only", data_root=tmp_path / "data"),
        cwd=tmp_path,
        command_runner=runner,
    )
    assert report.ok, report.error_summary
    assert report.details["llm_mode"] == "online_only"
    assert report.details["codex_probe"] == "disabled"
    assert report.details["analyzer_backend"] == "online"
    assert report.details["online_probe"] == "ok"


def test_personal_group_validation_has_no_phantom_fallback(tmp_path):
    from tests.integration.test_runner_cross_conversation_merge import (
        _runner,
        _draft,
    )
    from src.worktrace.analyzers.failover import FailoverAnalyzer
    from src.worktrace.models import CrossConversationGroupResult

    class Online:
        calls = 0

        def merge_day_candidates(self, *args, **kwargs):
            self.calls += 1
            return CrossConversationGroupResult(groups=[])

    primary = Online()
    analyzer = FailoverAnalyzer(
        primary, None, LLMUsageRecorder(), primary_backend="online"
    )
    runner = _runner(tmp_path, analyzer, llm_mode="online_only")
    result, warnings, attempts, retries, fallbacks, repairs = (
        runner._request_valid_day_groups(
            "2026-07-22",
            [_draft("d1", "m1")],
            request_label="test",
        )
    )
    assert primary.calls == 2
    assert retries == 1
    assert fallbacks == 0
    assert repairs == 1
    assert [attempt["backend"] for attempt in attempts] == [
        "online",
        "online",
        "python",
    ]


def test_collected_group_validation_detects_absent_fallback(tmp_path):
    from src.worktrace.analyzers.failover import FailoverAnalyzer
    from src.worktrace.collected_merge import CollectedMergeRunner

    analyzer = FailoverAnalyzer(
        object(), None, LLMUsageRecorder(), primary_backend="online"
    )
    runner = CollectedMergeRunner(
        config=RuntimeConfig(llm_mode="online_only"), analyzer=analyzer
    )
    assert not runner._supports_current_request_fallback()


@pytest.mark.parametrize(
    "failure, expected",
    [
        ("timeout", 2),
        ("429", 2),
        ("500", 2),
        ("empty", 2),
        ("json", 2),
        ("401", 1),
        ("403", 1),
        ("tls", 1),
        ("400", 1),
    ],
)
def test_online_images_retry_same_line_only(
    tmp_path, monkeypatch, failure, expected
):
    import json
    import httpx
    from types import SimpleNamespace
    from openai import (
        APITimeoutError,
        APIConnectionError,
        APIStatusError,
        RateLimitError,
    )
    from src.worktrace.vision import (
        OnlineImageSummarizer,
        ImageSummarySettings,
    )

    online_env(tmp_path)
    image = tmp_path / "sample.png"
    image.write_bytes(b"image")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        request = httpx.Request("POST", "https://example.invalid/v1")
        if failure == "empty":
            return SimpleNamespace(model_dump=lambda: {}, output_text="")
        if failure == "json":
            raise json.JSONDecodeError("invalid", "{", 1)
        if failure == "timeout":
            raise APITimeoutError(request=request)
        if failure == "tls":
            raise APIConnectionError(
                message="TLS handshake failed", request=request
            )
        error_type = RateLimitError if failure == "429" else APIStatusError
        raise error_type(
            "failed",
            response=httpx.Response(int(failure), request=request),
            body=None,
        )

    recorder = LLMUsageRecorder()
    summarizer = OnlineImageSummarizer(
        config=RuntimeConfig(llm_mode="online_only"),
        cwd=tmp_path,
        settings=ImageSummarySettings(True, "摘要", 1, 1024),
        client=SimpleNamespace(responses=SimpleNamespace(create=create)),
        usage_recorder=recorder,
    )
    with pytest.raises(AnalyzerProtocolError):
        summarizer.summarize(image, required=True)
    assert len(calls) == expected
    assert recorder.summary()["fallback_count"] == 0
    assert all(record["backend"] == "online" for record in recorder.records())


def test_online_image_factory_does_not_construct_codex(tmp_path, monkeypatch):
    from src.worktrace.factories import ContentResolverFactory
    from src.worktrace.vision import OnlineImageSummarizer

    monkeypatch.chdir(tmp_path)
    online_env(tmp_path)
    monkeypatch.setattr(
        "src.worktrace.analyzers.codex.CodexAnalyzer", forbid_codex
    )
    resolver = ContentResolverFactory.create_default(
        RuntimeConfig(llm_mode="online_only")
    )
    assert isinstance(resolver.image_summarizer, OnlineImageSummarizer)


@pytest.mark.parametrize(
    "error, expected",
    [
        (RetryableAnalyzerProtocolError("timeout"), 2),
        (AnalyzerProtocolError("HTTP 401"), 1),
        ({}, 2),
    ],
)
def test_online_report_failure_keeps_private_safe_base_report(
    tmp_path, monkeypatch, error, expected
):
    from tests.unit.test_support_report import _result, REPO_ROOT
    from src.worktrace.support_report import generate_support_report

    online_env(tmp_path)
    monkeypatch.setattr(
        "src.worktrace.analyzers.codex.CodexAnalyzer", forbid_codex
    )
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        if isinstance(error, Exception):
            raise error
        return error

    def version(args, **kwargs):
        assert args[0] != "codex"
        return "1.2.3"

    monkeypatch.setattr(
        "src.worktrace.analyzers.online.OnlineLLMAnalyzer.request_function",
        request,
    )
    monkeypatch.setattr(
        "src.worktrace.support_report._command_version", version
    )
    # Use repository report settings while resolving credentials from environment.
    monkeypatch.setenv("WORKTRACE_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("WORKTRACE_LLM_MODEL", "test-model")
    monkeypatch.setenv("WORKTRACE_LLM_API_KEY", "test-key")
    reference = generate_support_report(
        result=_result(tmp_path),
        run_mode="personal",
        config=RuntimeConfig(
            llm_mode="online_only", data_root=tmp_path / "data"
        ),
        cwd=REPO_ROOT,
        elapsed_ms=10,
    )
    assert reference.status == "generated_after_llm_failure"
    assert reference.privacy_check == "passed"
    assert len(calls) == expected
    text = Path(reference.path).read_text()
    assert "Codex 版本 | 未启用" in text
    assert "Alice" not in text


def test_environment_versions_never_run_codex_when_disabled():
    from types import SimpleNamespace
    from src.worktrace.support_report import collect_environment_versions

    calls = []

    def runner(args, **kwargs):
        calls.append(tuple(args))
        assert args[0] != "codex"
        return SimpleNamespace(returncode=0, stdout="1.2.3", stderr="")

    versions = collect_environment_versions(
        runner, config=RuntimeConfig(llm_mode="online_only")
    )
    assert versions["codex_version"] == "not_enabled"
    assert calls == [("lark-cli", "--version")]


def test_online_budget_ignores_codex_combination_profile(
    tmp_path, monkeypatch
):
    import shutil

    (tmp_path / "config").mkdir()
    shutil.copy(
        "config/model_input_budget.json",
        tmp_path / "config/model_input_budget.json",
    )
    monkeypatch.setenv("WORKTRACE_CODEX_MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("WORKTRACE_LLM_MODEL", "qwen3.7-max")
    monkeypatch.setenv("WORKTRACE_LLM_MODE", "online_only")
    config = load_runtime_config_overrides(RuntimeConfig(), cwd=tmp_path)
    assert config.model_input_batch_target_tokens == 7000
    assert not config.model_input_budget_selection.profile_matched
    monkeypatch.setenv("WORKTRACE_LLM_MODE", "codex_with_fallback")
    assert (
        load_runtime_config_overrides(
            RuntimeConfig(), cwd=tmp_path
        ).model_input_batch_target_tokens
        == 20000
    )


def test_checkpoint_isolated_by_mode_and_old_format_misses(tmp_path):
    import json
    from dataclasses import replace
    from src.worktrace.models import SegmentAnalysisBatch
    from src.worktrace.pipeline.llm_checkpoints import LLMCheckpointStore

    config = RuntimeConfig(data_root=tmp_path / "data")
    batch = SegmentAnalysisBatch(
        "2026-07-13", "oc_1", "", "ou_self", "本人", []
    )
    codex = LLMCheckpointStore(config, "2026-07-13")
    online = LLMCheckpointStore(
        replace(config, llm_mode="online_only"), "2026-07-13"
    )
    codex.save_analysis(batch, [], ["codex"], 0)
    assert online.load_analysis(batch) is None
    online.save_analysis(batch, [], ["online"], 0)
    assert codex.load_analysis(batch) == ([], ["codex"], 0)
    assert online.load_analysis(batch) == ([], ["online"], 0)
    for path in config.data_root.rglob("*.json"):
        payload = json.loads(path.read_text())
        payload.pop("llm_mode", None)
        path.write_text(json.dumps(payload))
    assert codex.load_analysis(batch) is None
    assert online.load_analysis(batch) is None


@pytest.mark.parametrize(
    "configured, explicit, expected",
    [
        ("online_only", None, "online_only"),
        ("codex_with_fallback", None, "codex_with_fallback"),
        ("codex_with_fallback", "online", "online_only"),
        ("online_only", "codex", "codex_with_fallback"),
    ],
)
def test_replay_obeys_mode_and_legacy_explicit_override(
    tmp_path, monkeypatch, configured, explicit, expected
):
    import json
    import subprocess
    import scripts.replay_day_with_trace as replay

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKTRACE_LLM_MODE", raising=False)
    (tmp_path / ".env").write_text(f"WORKTRACE_LLM_MODE={configured}\n")
    observed = []

    def cli_main(args, config):
        observed.append(
            load_runtime_config_overrides(config, cwd=tmp_path).llm_mode
        )
        return 0

    def run(args, *, env, stdout_path, stderr_path, **kwargs):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        try:
            exec(args[2], {})
        except SystemExit as exc:
            assert exc.code == 0
        stdout_path.write_text("{}")
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr("src.worktrace.cli.main", cli_main)
    monkeypatch.setattr(replay, "_run_with_live_stderr", run)
    args = ["--date", "2026-07-13", "--data-root", str(tmp_path / "data")]
    if explicit:
        args += ["--analyzer-backend", explicit]
    assert replay.main(args) == 0
    assert observed == [expected]
    summary = json.loads(
        (tmp_path / "data/replay-trace/2026-07-13/summary.json").read_text()
    )
    assert summary["llm_mode"] == expected


@pytest.mark.parametrize(
    "failure, expected",
    [
        ("timeout", 2),
        ("429", 2),
        ("503", 2),
        ("json", 2),
        ("401", 1),
        ("403", 1),
        ("tls", 1),
        ("400", 1),
    ],
)
def test_real_online_text_adapter_retry_contract(
    tmp_path, monkeypatch, failure, expected
):
    import httpx
    from types import SimpleNamespace
    from openai import (
        APITimeoutError,
        APIConnectionError,
        APIStatusError,
        RateLimitError,
    )

    online_env(tmp_path)
    requests = []
    clients = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
            clients.append(kwargs)
            self.responses = SimpleNamespace(create=self.create)
            self.http_client = kwargs["http_client"]

        def close(self):
            self.http_client.close()

        def create(self, **kwargs):
            requests.append(kwargs)
            request = httpx.Request("POST", "https://example.invalid/v1")
            if failure == "json":
                return SimpleNamespace(
                    model_dump=lambda: {
                        "output": [
                            {
                                "type": "function_call",
                                "name": kwargs["tools"][0]["name"],
                                "arguments": "bad json",
                            }
                        ],
                    }
                )
            if failure == "timeout":
                raise APITimeoutError(request=request)
            if failure == "tls":
                cause = APIConnectionError(
                    message="TLS handshake failed", request=request
                )
                raise APIConnectionError(request=request) from cause
            error_type = RateLimitError if failure == "429" else APIStatusError
            raise error_type(
                "failed",
                response=httpx.Response(int(failure), request=request),
                body=None,
            )

    monkeypatch.setattr("src.worktrace.analyzers.online.OpenAI", Client)
    monkeypatch.setattr(
        "src.worktrace.analyzers.codex.CodexAnalyzer", forbid_codex
    )
    recorder = LLMUsageRecorder()
    analyzer = AnalyzerFactory.create_default(
        RuntimeConfig(llm_mode="online_only"),
        cwd=tmp_path,
        usage_recorder=recorder,
    )
    spec = function_call_spec(
        "preflight",
        {
            "type": "object",
            "properties": {"probe": {"type": "string"}},
            "required": ["probe"],
            "additionalProperties": False,
        },
    )
    with pytest.raises(AnalyzerProtocolError):
        analyzer.request_function("提交测试结果", function_spec=spec)
    assert len(requests) == expected
    assert len(recorder.records()) == expected
    assert recorder.summary()["fallback_count"] == 0


def test_personal_pipeline_runs_without_codex(tmp_path, monkeypatch):
    from src.worktrace.factories import (
        RuntimeDependencies,
        ContentResolverFactory,
    )
    from src.worktrace.runner import DailyTraceRunner
    from src.worktrace.stores.markdown import MarkdownEventStore
    from tests.integration.test_runner_segment_batches import (
        _config,
        SegmentSource,
        SegmentBatchAnalyzer,
    )
    from tests.helpers import FunctionRequestStub, NullDelivery

    monkeypatch.chdir(tmp_path)
    online_env(tmp_path)
    monkeypatch.setattr(
        "src.worktrace.analyzers.codex.CodexAnalyzer", forbid_codex
    )
    monkeypatch.setattr(
        "src.worktrace.config.load_codex_llm_settings", forbid_codex
    )
    semantic = SegmentBatchAnalyzer()
    for name in (
        "segment_conversation",
        "analyze_segment_batch",
        "merge_day_candidates",
    ):
        method = getattr(semantic, name)
        monkeypatch.setattr(
            "src.worktrace.analyzers.online.OnlineLLMAnalyzer." + name,
            lambda self, *a, _method=method, **k: _method(*a, **k),
        )
    stub = FunctionRequestStub()
    monkeypatch.setattr(
        "src.worktrace.analyzers.online.OnlineLLMAnalyzer.request_function",
        lambda self, *a, **k: stub.request_function(*a, **k),
    )
    config = _config(
        llm_mode="online_only",
        data_root=tmp_path / "data",
        self_delivery_enabled=False,
    )
    analyzer = AnalyzerFactory.create_default(config, cwd=tmp_path)
    result = DailyTraceRunner(
        config=config,
        dependencies=RuntimeDependencies(
            chat_source=SegmentSource(),
            content_resolver=ContentResolverFactory.create_default(config),
            analyzer=analyzer,
            delivery_channel=NullDelivery(),
            event_store=MarkdownEventStore(config=config),
        ),
    ).run("2026-07-10")
    assert result.status in {
        "success",
        "success_with_warnings",
    }, result.error_summary
    assert result.event_count == 2
    assert result.self_delivery_status == "disabled"
    assert Path(result.output_path).is_file()
    assert result.day_grouping_summary.fallback_count == 0


def test_team_pipeline_runs_without_codex(tmp_path, monkeypatch):
    from src.worktrace.collected_merge import CollectedMergeRunner
    from tests.unit.test_collected_merge import (
        _write_day_doc,
        _event,
        TwoStageAnalyzer,
    )
    from tests.helpers import FunctionRequestStub

    monkeypatch.chdir(tmp_path)
    online_env(tmp_path)
    monkeypatch.setattr(
        "src.worktrace.analyzers.codex.CodexAnalyzer", forbid_codex
    )
    monkeypatch.setattr(
        "src.worktrace.config.load_codex_llm_settings", forbid_codex
    )
    semantic = TwoStageAnalyzer()
    for name in ("group_collected_events", "merge_collected_events"):
        method = getattr(semantic, name)
        monkeypatch.setattr(
            "src.worktrace.analyzers.online.OnlineLLMAnalyzer." + name,
            lambda self, *a, _method=method, **k: _method(*a),
        )
    stub = FunctionRequestStub()
    monkeypatch.setattr(
        "src.worktrace.analyzers.online.OnlineLLMAnalyzer.request_function",
        lambda self, *a, **k: stub.request_function(*a, **k),
    )
    inbox = tmp_path / "merge_inbox/2026/06/29"
    for index in (1, 2):
        _write_day_doc(
            inbox / f"2026-06-29-测试员工{index}.md",
            [
                _event(
                    event_id=f"evt-{index}",
                    title=f"测试事项{index}",
                    content=f"确认测试事项{index}。",
                )
            ],
            tmp_path,
        )
    config = RuntimeConfig(
        llm_mode="online_only",
        data_root=tmp_path / "data",
        self_delivery_enabled=False,
    )
    result = CollectedMergeRunner(config=config, cwd=tmp_path).run(
        "2026-06-29", offline=True, merge_owner_name="测试负责人"
    )
    assert result.status in {
        "success",
        "success_with_warnings",
    }, result.warning_messages
    assert result.source_event_count == 2
    assert result.self_delivery_status == "disabled"
    assert Path(result.output_path).is_file()
