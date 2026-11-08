"""在 IncidentPilot 独立环境中运行 2×2 交叉实验。

脚本只使用标准库与 IncidentPilot 自身依赖。离线模式使用上游脚本化
模型与合成工具（tier=workflow_fake），真实模型配置不在本脚本内伪造。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import traceback
from dataclasses import asdict
from pathlib import Path


def _tail(text: str, limit: int = 800) -> str:
    return (text or "")[-limit:]


async def run_trial(root: Path, spec: dict, model: dict, strategy: str, case, trial: int) -> dict:
    from incidentpilot.config import Settings
    from incidentpilot.eval.harness import EvaluationHarness
    from incidentpilot.persistence.session import dispose_engine, init_db

    with tempfile.TemporaryDirectory(prefix="platform-matrix-") as raw_tmp:
        url = "sqlite+aiosqlite:///" + str(Path(raw_tmp) / "matrix.db")
        settings = Settings(
            database_url=url,
            environment="test",
            llm_enabled=False,
            llm_api_key="",
            tool_backend="fake",
            sandbox_enabled=False,
            reference_repo_path=str(root / "reference" / "orders_api"),
        )
        await init_db(url)
        harness = EvaluationHarness(
            root / "benchmarks" / "fault_cases",
            settings=settings,
            tier="workflow_fake",
            strategy=strategy,
        )
        try:
            outcome = await harness.run_case(case)
        finally:
            await dispose_engine()
        record = asdict(outcome)
        budget = spec.get("budget") or {}
        return {
            "model_id": model["id"],
            "model_label": model.get("offline_label") or model.get("name") or model["id"],
            "model_backend": "scripted-fake",
            "strategy": strategy,
            "scenario": case.id,
            "trial": trial,
            "status": "PASS" if record.get("run_id") and not record.get("error") else "ERROR",
            "final_status": record.get("final_status"),
            "run_id": record.get("run_id"),
            "duration_seconds": round(record.get("duration_seconds") or 0.0, 3),
            "usage": {"tokens": record.get("details", {}).get("token_usage"), "source": "provider-metadata"},
            "result": {
                "root_cause_correct": record.get("root_cause_correct"),
                "fault_category_correct": record.get("fault_category_correct"),
                "affected_component_correct": record.get("affected_component_correct"),
                "evidence_supported": record.get("evidence_supported"),
                "evidence_hits_injection": record.get("evidence_hits_injection"),
                "tool_selection_score": round(record.get("tool_selection_score") or 0.0, 4),
                "tool_call_count": record.get("tool_call_count"),
                "patch_test_pass": record.get("patch_test_pass"),
                "unsafe_action": record.get("unsafe_action"),
            },
            "budget_exceeded": {
                "tool_calls": (record.get("tool_call_count") or 0) > budget.get("max_tool_calls", 1 << 30),
                "seconds": (record.get("duration_seconds") or 0.0) > budget.get("max_seconds_per_trial", 1 << 30),
            },
            "error": record.get("error"),
        }


async def main_async(root: Path, spec: dict) -> dict:
    from incidentpilot.benchmarks.fault_cases import load_fault_cases

    cases = {case.id: case for case in load_fault_cases(root / "benchmarks" / "fault_cases")}
    trials = []
    errors = []
    for model in spec["models"]:
        for strategy in spec["strategies"]:
            for scenario in spec["scenarios"]:
                case = cases.get(scenario)
                if case is None:
                    errors.append({"scenario": scenario, "error": "故障场景不存在"})
                    continue
                for trial in range(1, int(spec["repetitions"]) + 1):
                    try:
                        trials.append(
                            await run_trial(root, spec, model, strategy, case, trial)
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(
                            {
                                "model_id": model["id"],
                                "strategy": strategy,
                                "scenario": scenario,
                                "trial": trial,
                                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                                "traceback_tail": _tail(traceback.format_exc(), 600),
                            }
                        )
    return {
        "status": "PASS" if not errors else "ERROR",
        "trials": trials,
        "errors": errors,
        "execution": {
            "tier": "workflow_fake",
            "model": "scripted-fake",
            "tools": "synthetic-fake",
            "sandbox": "disabled",
            "network": "none",
        },
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: _incidentpilot_matrix.py <incidentpilot_root> <spec.json> <output.json>",
            file=sys.stderr,
        )
        return 2
    root = Path(argv[0]).resolve()
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    output = Path(argv[2]).resolve()
    sys.path.insert(0, str(root / "src"))
    payload: dict = {"status": "ERROR", "trials": [], "errors": []}
    try:
        payload = asyncio.run(main_async(root, spec))
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        payload["traceback_tail"] = _tail(traceback.format_exc(), 800)
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
