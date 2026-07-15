from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ..config import RuntimeConfig
from ..models import AnchorUnit, ConversationSegmentUnit, SegmentAnalysisBatch, SourceBackedEventDraft
from ..utils.json_io import dump_json


def clear_day_llm_checkpoints(config: RuntimeConfig, target_date: str) -> None:
    path = _day_root(config, target_date)
    if path.exists():
        shutil.rmtree(path)


@dataclass(frozen=True)
class LLMCheckpointStore:
    config: RuntimeConfig
    target_date: str

    def load_segmentation(
        self, anchor_unit: AnchorUnit
    ) -> tuple[list[ConversationSegmentUnit], list[str]] | None:
        payload = self._load("segmentation", anchor_unit.to_dict())
        if payload is None:
            return None
        return (
            [ConversationSegmentUnit.from_dict(item) for item in payload["units"]],
            [str(item) for item in payload.get("warnings", [])],
        )

    def save_segmentation(
        self,
        anchor_unit: AnchorUnit,
        units: list[ConversationSegmentUnit],
        warnings: list[str],
    ) -> None:
        self._save(
            "segmentation",
            anchor_unit.to_dict(),
            {"units": [item.to_dict() for item in units], "warnings": list(warnings)},
        )

    def load_analysis(
        self, batch: SegmentAnalysisBatch
    ) -> tuple[list[SourceBackedEventDraft], list[str], int] | None:
        payload = self._load("analysis", batch.to_dict())
        if payload is None:
            return None
        return (
            [SourceBackedEventDraft.from_dict(item) for item in payload["candidates"]],
            [str(item) for item in payload.get("warnings", [])],
            int(payload.get("skipped_count", 0)),
        )

    def save_analysis(
        self,
        batch: SegmentAnalysisBatch,
        candidates: list[SourceBackedEventDraft],
        warnings: list[str],
        skipped_count: int,
    ) -> None:
        self._save(
            "analysis",
            batch.to_dict(),
            {
                "candidates": [item.to_dict() for item in candidates],
                "warnings": list(warnings),
                "skipped_count": skipped_count,
            },
        )

    def clear(self) -> None:
        clear_day_llm_checkpoints(self.config, self.target_date)

    def _load(self, stage: str, input_payload: dict[str, object]) -> dict[str, object] | None:
        path = self._path(stage, input_payload)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("input") != input_payload:
            return None
        result = payload.get("result")
        return result if isinstance(result, dict) else None

    def _save(self, stage: str, input_payload: dict[str, object], result: dict[str, object]) -> None:
        path = self._path(stage, input_payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(
            dump_json({"input": input_payload, "result": result}, pretty=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _path(self, stage: str, input_payload: dict[str, object]) -> Path:
        fingerprint = sha256(dump_json(input_payload).encode("utf-8")).hexdigest()
        return _day_root(self.config, self.target_date) / stage / f"{fingerprint}.json"


def _day_root(config: RuntimeConfig, target_date: str) -> Path:
    year, month, _day = target_date.split("-")
    return config.data_root / "cache" / "llm" / year / month / target_date
