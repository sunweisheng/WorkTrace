from __future__ import annotations

import json
import io
import urllib.error
from pathlib import Path

import pytest

from src.worktrace.analyzers.hook import HookAnalyzer
from src.worktrace.config import RuntimeConfig
from src.worktrace.errors import AnalyzerProtocolError
from src.worktrace.models import (
    AnalysisBatch,
    ConversationSlice,
    NormalizedMessage,
)
from src.worktrace.hook_runner import (
    _build_chat_completions_request_body,
    _extract_text_from_chat_completions_payload,
    _build_responses_request_body,
    _extract_text_from_responses_payload,
    _run_chat_completions_http,
    _run_codex_via_stdin,
    _run_responses_http,
)


def sample_batch() -> AnalysisBatch:
    return AnalysisBatch(
        target_date="2026-06-23",
        batch_id="batch-001",
        retry_round=0,
        estimated_tokens=123,
        slices=[
            ConversationSlice(
                slice_id="slice-1",
                conversation_id="oc_1",
                conversation_name="项目群",
                anchor_message_ids=["om_1"],
                in_day_message_ids=["om_1"],
                messages=[
                    NormalizedMessage(
                        conversation_id="oc_1",
                        conversation_name="项目群",
                        message_id="om_1",
                        sender_open_id="ou_1",
                        sender_name="Alice",
                        send_time="2026-06-23T10:00:00+08:00",
                        message_type="text",
                        text="推进发布",
                        reply_to_message_id=None,
                        quote_message_id=None,
                    )
                ],
            )
        ],
    )


def test_hook_analyzer_reads_json_from_stdout(tmp_path: Path) -> None:
    def fake_runner(args, *, cwd=None, timeout=None, input_text=None, env=None):
        class Result:
            returncode = 0
            stdout = json.dumps({"candidate_events": [], "context_requests": []})
            stderr = ""

        return Result()

    analyzer = HookAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            analyzer_backend="hook",
            hook_command="mock-hook",
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    result = analyzer.analyze_batch("2026-06-23", sample_batch())

    assert result.candidate_events == []
    assert result.context_requests == []


def test_hook_analyzer_passes_schema_path_via_env(tmp_path: Path) -> None:
    captured_env: dict[str, str] = {}

    def fake_runner(args, *, cwd=None, timeout=None, input_text=None, env=None):
        nonlocal captured_env
        captured_env = dict(env or {})

        class Result:
            returncode = 0
            stdout = json.dumps({"candidate_events": [], "context_requests": []})
            stderr = ""

        return Result()

    analyzer = HookAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            analyzer_backend="hook",
            hook_command="mock-hook",
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    analyzer.analyze_batch("2026-06-23", sample_batch())

    assert "WORKTRACE_HOOK_SCHEMA_PATH" in captured_env
    assert captured_env["WORKTRACE_HOOK_SCHEMA_PATH"].endswith(".json")


def test_hook_analyzer_normalizes_enveloped_json_stdout(tmp_path: Path) -> None:
    def fake_runner(args, *, cwd=None, timeout=None, input_text=None, env=None):
        class Result:
            returncode = 0
            stdout = json.dumps(
                {"result": {"candidate_events": [], "context_requests": []}}
            )
            stderr = ""

        return Result()

    analyzer = HookAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            analyzer_backend="hook",
            hook_command="mock-hook",
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    result = analyzer.analyze_batch("2026-06-23", sample_batch())

    assert result.candidate_events == []
    assert result.context_requests == []


def test_hook_analyzer_surfaces_stderr_tail_on_failure(tmp_path: Path) -> None:
    def fake_runner(args, *, cwd=None, timeout=None, input_text=None, env=None):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "line1\nline2\nline3\nline4\n"

        return Result()

    analyzer = HookAnalyzer(
        config=RuntimeConfig(
            data_root=tmp_path / "data",
            analyzer_backend="hook",
            hook_command="mock-hook",
        ),
        command_runner=fake_runner,
        cwd=tmp_path,
    )

    with pytest.raises(AnalyzerProtocolError) as exc_info:
        analyzer.analyze_batch("2026-06-23", sample_batch())

    message = str(exc_info.value)
    assert "returncode=1" in message
    assert "line2 | line3 | line4" in message


def test_hook_runner_reports_missing_output_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 0
        stderr = ""

    def fake_run(args, **kwargs):
        output_index = args.index("-o") + 1
        Path(args[output_index]).unlink(missing_ok=True)
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    code = _run_codex_via_stdin("prompt", cwd=tmp_path)

    assert code == 1


def test_hook_runner_passes_output_schema_to_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen_command: list[str] = []
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"type":"object"}', encoding="utf-8")

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(args, **kwargs):
        nonlocal seen_command
        seen_command = list(args)
        output_index = args.index("-o") + 1
        Path(args[output_index]).write_text(
            '{"candidate_events":[],"context_requests":[]}',
            encoding="utf-8",
        )
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setenv("WORKTRACE_HOOK_SCHEMA_PATH", str(schema_path))
    monkeypatch.setattr("sys.stdout", io.StringIO())

    code = _run_codex_via_stdin("prompt", cwd=tmp_path)

    assert code == 0
    assert "--output-schema" in seen_command
    assert str(schema_path) in seen_command


def test_hook_runner_normalizes_json_fragment_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 0
        stderr = ""

    def fake_run(args, **kwargs):
        output_index = args.index("-o") + 1
        Path(args[output_index]).write_text(
            'prefix {"candidate_events":[],"context_requests":[]} suffix',
            encoding="utf-8",
        )
        return Result()

    stdout_capture = io.StringIO()
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("sys.stdout", stdout_capture)

    code = _run_codex_via_stdin("prompt", cwd=tmp_path)

    assert code == 0
    assert json.loads(stdout_capture.getvalue()) == {
        "candidate_events": [],
        "context_requests": [],
    }


def test_build_responses_request_body_includes_schema(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"type":"object","required":["candidate_events"]}', encoding="utf-8")

    body = _build_responses_request_body(
        "prompt",
        settings=__import__("src.worktrace.config", fromlist=["HookLLMSettings"]).HookLLMSettings(
            base_url="https://llm.example/v1",
            model="provider-model",
            api_key="secret",
            timeout_seconds=30,
        ),
        schema_path=str(schema_path),
    )

    assert body["model"] == "provider-model"
    assert body["input"] == "prompt"
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["schema"]["required"] == ["candidate_events"]


def test_extract_text_from_chat_completions_payload_supports_string_content() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": '{"candidate_events":[],"context_requests":[]}',
                }
            }
        ]
    }

    assert (
        _extract_text_from_chat_completions_payload(payload)
        == '{"candidate_events":[],"context_requests":[]}'
    )


def test_extract_text_from_chat_completions_payload_supports_list_content() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"candidate_events":[],"context_requests":[]}',
                        }
                    ]
                }
            }
        ]
    }

    assert (
        _extract_text_from_chat_completions_payload(payload)
        == '{"candidate_events":[],"context_requests":[]}'
    )


def test_build_chat_completions_request_body_includes_schema(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"type":"object","required":["candidate_events"]}', encoding="utf-8")

    body = _build_chat_completions_request_body(
        "prompt",
        settings=__import__("src.worktrace.config", fromlist=["HookLLMSettings"]).HookLLMSettings(
            base_url="https://llm.example/v1",
            model="provider-model",
            api_key="secret",
            timeout_seconds=30,
        ),
        schema_path=str(schema_path),
    )

    assert body["model"] == "provider-model"
    assert body["messages"] == [{"role": "user", "content": "prompt"}]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["required"] == [
        "candidate_events"
    ]


def test_hook_runner_chat_completions_http_normalizes_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n",
        encoding="utf-8",
    )
    stdout_capture = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout_capture)
    monkeypatch.setattr(
        "src.worktrace.hook_runner._post_chat_completions_request",
        lambda prompt, *, settings, schema_path: {
            "choices": [
                {
                    "message": {
                        "content": '{"candidate_events":[],"context_requests":[]}',
                    }
                }
            ]
        },
    )

    code = _run_chat_completions_http("prompt", cwd=tmp_path, config=RuntimeConfig())

    assert code == 0
    assert json.loads(stdout_capture.getvalue()) == {
        "candidate_events": [],
        "context_requests": [],
    }


def test_hook_runner_chat_completions_http_surfaces_runtime_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n",
        encoding="utf-8",
    )
    stderr_capture = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr_capture)
    monkeypatch.setattr(
        "src.worktrace.hook_runner._post_chat_completions_request",
        lambda prompt, *, settings, schema_path: (_ for _ in ()).throw(
            RuntimeError("HTTP 504")
        ),
    )

    code = _run_chat_completions_http("prompt", cwd=tmp_path, config=RuntimeConfig())

    assert code == 1
    assert "HTTP 504" in stderr_capture.getvalue()


def test_extract_text_from_responses_payload_supports_output_content() -> None:
    payload = {
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"candidate_events":[],"context_requests":[]}',
                    }
                ]
            }
        ]
    }

    assert (
        _extract_text_from_responses_payload(payload)
        == '{"candidate_events":[],"context_requests":[]}'
    )


def test_hook_runner_responses_http_normalizes_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n",
        encoding="utf-8",
    )
    stdout_capture = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout_capture)
    monkeypatch.setattr(
        "src.worktrace.hook_runner._post_responses_request",
        lambda prompt, *, settings, schema_path: {
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"candidate_events":[],"context_requests":[]}',
                        }
                    ]
                }
            ]
        },
    )

    code = _run_responses_http("prompt", cwd=tmp_path, config=RuntimeConfig())

    assert code == 0
    assert json.loads(stdout_capture.getvalue()) == {
        "candidate_events": [],
        "context_requests": [],
    }


def test_hook_runner_responses_http_requires_local_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stderr_capture = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr_capture)

    code = _run_responses_http("prompt", cwd=tmp_path, config=RuntimeConfig())

    assert code == 2
    assert "Missing online LLM configuration" in stderr_capture.getvalue()


def test_hook_runner_responses_http_surfaces_http_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n",
        encoding="utf-8",
    )
    stderr_capture = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr_capture)
    monkeypatch.setattr(
        "src.worktrace.hook_runner._post_responses_request",
        lambda prompt, *, settings, schema_path: (_ for _ in ()).throw(
            urllib.error.HTTPError(
                url="https://llm.example/v1/responses",
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=io.BytesIO(b'{"error":"bad key"}'),
            )
        ),
    )

    code = _run_responses_http("prompt", cwd=tmp_path, config=RuntimeConfig())

    assert code == 1
    assert "HTTP 401" in stderr_capture.getvalue()


def test_hook_runner_responses_http_surfaces_network_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "WORKTRACE_LLM_BASE_URL=https://llm.example/v1\n"
        "WORKTRACE_LLM_MODEL=provider-model\n"
        "WORKTRACE_LLM_API_KEY=file-key\n",
        encoding="utf-8",
    )
    stderr_capture = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr_capture)
    monkeypatch.setattr(
        "src.worktrace.hook_runner._post_responses_request",
        lambda prompt, *, settings, schema_path: (_ for _ in ()).throw(
            urllib.error.URLError("connection reset")
        ),
    )

    code = _run_responses_http("prompt", cwd=tmp_path, config=RuntimeConfig())

    assert code == 1
    assert "Network error" in stderr_capture.getvalue()
