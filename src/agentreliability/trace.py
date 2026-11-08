"""逐步轨迹采集与 JSONL 写出。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TraceCollector:
    run_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    def open(
        self,
        *,
        case_id: str,
        trial: int,
        round_no: int,
        source: str,
        step: str,
        summary: str,
    ) -> dict[str, Any]:
        self._seq += 1
        event: dict[str, Any] = {
            "run_id": self.run_id,
            "case_id": case_id,
            "trial": trial,
            "round": round_no,
            "seq": self._seq,
            "source": source,
            "step": step,
            "status": "running",
            "summary": summary,
            "duration_ms": 0,
        }
        self.events.append(event)
        return event

    def close(
        self,
        event: dict[str, Any],
        status: str,
        summary: str,
        duration_ms: int = 0,
        *,
        sql: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        event["status"] = status
        event["summary"] = summary
        event["duration_ms"] = max(0, int(duration_ms))
        if sql is not None:
            event["sql"] = sql
        if extra:
            event.update(extra)

    @property
    def next_seq(self) -> int:
        return self._seq + 1

    def refs_since(self, start_seq: int, case_id: str, trial: int) -> list[int]:
        return [
            event["seq"]
            for event in self.events
            if event["seq"] >= start_seq
            and event["case_id"] == case_id
            and event["trial"] == trial
        ]


def write_trace(path: Path, events: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
