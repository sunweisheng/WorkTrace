"""Safe CLI progress; no prompts, IDs, provider fields or payloads accepted."""
from __future__ import annotations

import sys
from contextvars import ContextVar
from threading import Event, Lock, Thread
from time import perf_counter

from .runtime_diagnostics import diagnostic_settings


_reporter: ContextVar[ProgressReporter | None] = ContextVar("progress", default=None)


class ProgressReporter:
    def __init__(self, stage: str, *, interval_seconds: float | None = None):
        settings = diagnostic_settings()
        self.labels = settings["stage_labels"]
        self.interval = (settings["progress_interval_seconds"]
                         if interval_seconds is None else interval_seconds)
        self.stage = self.labels.get(stage, self.labels["model_request"])
        self.completed = 0
        self.total: int | None = None
        self.started_at = perf_counter()
        self.stop = Event()
        self.lock = Lock()
        self.thread = Thread(target=self._wait, daemon=True)

    def __enter__(self):
        self.token = _reporter.set(self)
        self._emit()
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
        _reporter.reset(self.token)

    def set_stage(self, stage: str, total: int | None = None):
        with self.lock:
            self.stage = self.labels.get(stage, self.labels["model_request"])
            self.completed = 0
            self.total = total
        self._emit()

    def _emit(self):
        with self.lock:
            counts = (f"，已完成 {self.completed}/{self.total}"
                      if self.total is not None else "")
            elapsed = perf_counter() - self.started_at
            print(f"进度：{self.stage}{counts}，已耗时 {elapsed:.1f} 秒，正在处理。",
                  file=sys.stderr, flush=True)

    def _wait(self):
        while not self.stop.wait(self.interval):
            self._emit()


def report_stage(stage: str, *, total: int | None = None) -> None:
    if progress := _reporter.get():
        progress.set_stage(stage, total)


def report_completed() -> None:
    if progress := _reporter.get():
        with progress.lock:
            progress.completed += 1
