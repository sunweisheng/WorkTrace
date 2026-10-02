from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_cli_emits_utf8_json_and_logs_under_legacy_encoding(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    script = '''
from dataclasses import replace
from src.worktrace.cli import main, build_failed_result
from src.worktrace.config import RuntimeConfig
from src.worktrace.logging_utils import configure_logging
from src.worktrace.preflight import PreflightReport
def run(**kwargs):
    configure_logging().info("正在生成中文日报")
    return replace(build_failed_result("2026-10-01", ""),
                   status="success", output_path="C:/测试 目录/个人报告.md")
raise SystemExit(main(["--date", "2026-10-01"],
    config=RuntimeConfig(data_root=__import__('pathlib').Path("data")),
    preflight_func=lambda *a, **k: PreflightReport(ok=True), run_func=run))
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(repo),
             "PYTHONIOENCODING": "cp936"},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout.decode("utf-8"))
    assert payload["output_path"] == "C:/测试 目录/个人报告.md"
    assert "正在生成中文日报" in result.stderr.decode("utf-8")


def test_cli_accepts_embedded_streams_without_reconfigure(monkeypatch):
    import io
    from src.worktrace.cli import main

    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    assert main(["--date", "invalid"]) == 2
    assert json.loads(stream.getvalue())["status"] == "invalid_input"


def test_cli_accepts_stream_that_cannot_be_reconfigured(monkeypatch):
    import io
    from src.worktrace.cli import main

    class EmbeddedStream(io.StringIO):
        def reconfigure(self, **kwargs):
            raise io.UnsupportedOperation("synthetic embedded stream")

    stream = EmbeddedStream()
    monkeypatch.setattr(sys, "stdout", stream)
    assert main(["--date", "invalid"]) == 2
    assert json.loads(stream.getvalue())["status"] == "invalid_input"
