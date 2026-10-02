from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def diagnostic_settings() -> dict:
    path = Path(__file__).resolve().parents[2] / "config/runtime_diagnostics.json"
    return json.loads(path.read_text(encoding="utf-8"))


def classify_technical_error(text: str) -> tuple[str, str]:
    lowered = text.casefold()
    for rule in diagnostic_settings()["error_rules"]:
        if any(marker.casefold() in lowered for marker in rule["markers"]):
            return rule["code"], rule["summary"]
    return "probe_failed", "Probe failed. Check CLI installation and local configuration."


def structured_warnings(
    messages: list[str], *, delivery_errors: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    settings = diagnostic_settings()
    warnings = []
    delivery_rule = next(
        rule for rule in settings["warning_rules"]
        if rule["code"] == "delivery_warning"
    )
    for message in messages:
        rule = delivery_rule if message and message in delivery_errors else next(
            (rule for rule in settings["warning_rules"]
             if any(marker.casefold() in message.casefold()
                    for marker in rule["markers"])),
            settings["default_warning"],
        )
        warnings.append({key: rule[key] for key in ("code", "stage", "summary")})
    return warnings
