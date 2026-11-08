"""report.json 与 trace.jsonl 产物。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ORACLE_VERSION, REPORT_FORMAT_VERSION
from .oracle import ERROR, FAIL, NOT_RUN, PASS


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _top_verdict(results: list[dict[str, Any]]) -> str:
    verdicts = {result["verdict"] for result in results}
    if ERROR in verdicts or NOT_RUN in verdicts:
        return ERROR
    if FAIL in verdicts:
        return FAIL
    return PASS


def build_report(
    *,
    run_id: str,
    mode: str,
    target: dict[str, Any] | None,
    cases_path: Path,
    fixture_path: Path,
    results: list[dict[str, Any]],
    started_at: datetime,
    finished_at: datetime,
    error: str | None = None,
) -> dict[str, Any]:
    summary = {
        "total": len(results),
        "pass": sum(result["verdict"] == PASS for result in results),
        "fail": sum(result["verdict"] == FAIL for result in results),
        "error": sum(result["verdict"] == ERROR for result in results),
        "not_run": sum(result["verdict"] == NOT_RUN for result in results),
        "verdict": _top_verdict(results),
    }
    report: dict[str, Any] = {
        "format_version": REPORT_FORMAT_VERSION,
        "oracle_version": ORACLE_VERSION,
        "run_id": run_id,
        "mode": mode,
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "finished_at": finished_at.astimezone(timezone.utc).isoformat(),
        "target": target,
        "inputs": {
            "cases_path": str(cases_path),
            "cases_sha256": sha256_file(cases_path) if cases_path.exists() else None,
            "fixture_path": str(fixture_path),
            "fixture_sha256": sha256_file(fixture_path) if fixture_path.exists() else None,
        },
        "summary": summary,
        "cases": results,
    }
    if error:
        report["error"] = error
    return report


def write_artifacts(
    out_dir: Path,
    report: dict[str, Any],
    trace_events: list[dict[str, Any]],
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.json"
    trace_path = out_dir / "trace.jsonl"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with trace_path.open("w", encoding="utf-8") as handle:
        for event in trace_events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return report_path, trace_path
