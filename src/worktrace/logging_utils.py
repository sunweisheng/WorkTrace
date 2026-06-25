from __future__ import annotations

import json
import logging
import sys
from time import perf_counter


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("worktrace")
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        logger.addHandler(handler)

    logger.propagate = False
    return logger


def log_timing(
    logger: logging.Logger,
    event: str,
    started_at: float,
    **fields: object,
) -> float:
    duration_ms = (perf_counter() - started_at) * 1000
    rendered_fields = " ".join(
        f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in fields.items()
    )
    suffix = f" {rendered_fields}" if rendered_fields else ""
    logger.info("%s duration_ms=%.1f%s", event, duration_ms, suffix)
    return duration_ms
