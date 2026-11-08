"""在 IncidentPilot 独立环境中运行 2×2 交叉实验。

脚本只使用标准库与 IncidentPilot 自身依赖。离线模式使用上游脚本化
模型与合成工具（tier=workflow_fake），真实模型配置不在本脚本内伪造。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import traceback
from dataclasses import asdict
from pathlib import Path


def _tail(text: str, limit: int = 800) -> str:
    return (text or "")[-limit:]


def _model_prefix(spec: dict, model: dict) -> str:
    requirement = (spec.get("real_model_requirements") or {}).get(model["id"]) or []
    if requirement:
        first = str(requirement[0])
        for suffix in ("_API_KEY", "_MODEL", "_BASE_URL"):
            if first.endswith(suffix):
                return first[: -len(suffix)]
        return first
    return "PAWBENCH_MODEL_" + model["id"].split("-")[-1].upper()


def _slot_config(spec: dict, model: dict, environ: dict) -> dict:
    prefix = _model_prefix(spec, model)
    return {
        "prefix": prefix,
        "api_key": environ.get(f"{prefix}_API_KEY") or "",
        "model": environ.get(f"{prefix}_MODEL") or "",
        "base_url": environ.get(f"{prefix}_BASE_URL") or "",
    }


def _reference_api(environ: dict) -> str:
    return (
        environ.get("PAWBENCH_REFERENCE_ORDERS_API_URL")
        or environ.get("REFERENCE_ORDERS_API_URL")
        or "http://127.0.0.1:8001"
    )


async def _load_root_cause(run_id: str | None) -> dict:
    """读取运行记录中的根因，用于追溯模型输出与金标的差异。"""
    if not run_id:
        return {}
    try:
        from sqlalchemy import select

        from incidentpilot.models.db import AgentRun
        from incidentpilot.persistence.session import get_session_factory

        async with get_session_factory()() as session:
            run = (
                await session.execute(
                    select(AgentRun).where(AgentRun.run_id == run_id)
                )
            ).scalars().first()
            return dict(run.root_cause or {}) if run else {}
    except Exception:  # noqa: BLE001
        return {}


async def run_trial(
    root: Path,
    spec: dict,
    model: dict,
    strategy: str,
    case,
    trial: int,
    *,
    mode: str,
    environ: dict,
) -> dict:
    from incidentpilot.config import Settings
    from incidentpilot.eval.harness import EvaluationHarness
    from incidentpilot.eval.scoring import CATEGORY_ALIASES
    from incidentpilot.persistence.session import dispose_engine, init_db

    slot = _slot_config(spec, model, environ)
    with tempfile.TemporaryDirectory(prefix="platform-matrix-") as raw_tmp:
        url = "sqlite+aiosqlite:///" + str(Path(raw_tmp) / "matrix.db")
        common = {
            "database_url": url,
            "environment": "test",
            "sandbox_enabled": False,
            "reference_repo_path": str(root / "reference" / "orders_api"),
        }
        if mode == "model":
            tier = "real_llm_investigation"
            settings = Settings(
                **common,
                llm_enabled=True,
                llm_provider="openai-compatible",
                llm_api_key=slot["api_key"],
                llm_model=slot["model"],
                llm_base_url=slot["base_url"],
                tool_backend="mcp",
                reference_orders_api_url=_reference_api(environ),
                reference_logs_path=str(
                    root / "reference" / "orders_api" / "var" / "logs"
                ),
            )
        else:
            tier = "workflow_fake"
            settings = Settings(
                **common,
                llm_enabled=False,
                llm_api_key="",
                tool_backend="fake",
            )
        await init_db(url)
        harness = EvaluationHarness(
            root / "benchmarks" / "fault_cases",
            settings=settings,
            tier=tier,
            strategy=strategy,
        )
        try:
            outcome = await harness.run_case(case)
            predicted_root_cause = await _load_root_cause(outcome.run_id)
        finally:
            await dispose_engine()
        record = asdict(outcome)
        ground_truth = case.ground_truth or {}
        expected_category = str(
            ground_truth.get("fault_category")
            or ground_truth.get("expected_fault_category")
            or ""
        )
        expected_component = str(ground_truth.get("affected_component") or "")
        predicted_category = str(predicted_root_cause.get("fault_category") or "")
        predicted_component = str(predicted_root_cause.get("affected_component") or "")
        aliases = sorted(CATEGORY_ALIASES.get(expected_category.lower(), set()))
        alias_covered = bool(
            expected_category
            and (
                predicted_category.lower() == expected_category.lower()
                or predicted_category.lower() in aliases
            )
        )
        budget = spec.get("budget") or {}
        details = record.get("details") or {}
        provider_model = details.get("model")
        provider_model = provider_model if isinstance(provider_model, dict) else None
        real = mode == "model"
        return {
            "model_id": model["id"],
            "model_label": (
                (slot["model"] or model["id"])
                if real
                else (model.get("offline_label") or model.get("name") or model["id"])
            ),
            "model_identifier": slot["model"] if real else "scripted-fake",
            "model_backend": "real-llm" if real else "scripted-fake",
            "provider_model": provider_model,
            "execution": {
                "mode": mode,
                "tier": tier,
                "tool_backend": settings.tool_backend,
                "llm_enabled": settings.llm_enabled,
                "llm_model": slot["model"] or None,
            },
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
                "expected_category": expected_category,
                "predicted_category": predicted_category,
                "expected_component": expected_component,
                "predicted_component": predicted_component,
                "category_alias_covered": alias_covered,
                "root_cause_summary": record.get("root_cause_summary"),
            },
            "budget_exceeded": {
                "tool_calls": (record.get("tool_call_count") or 0) > budget.get("max_tool_calls", 1 << 30),
                "seconds": (record.get("duration_seconds") or 0.0) > budget.get("max_seconds_per_trial", 1 << 30),
            },
            "error": record.get("error"),
        }


async def main_async(root: Path, spec: dict, mode: str, environ: dict) -> dict:
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
                            await run_trial(
                                root,
                                spec,
                                model,
                                strategy,
                                case,
                                trial,
                                mode=mode,
                                environ=environ,
                            )
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
    completed = not errors and all(item["status"] == "PASS" for item in trials)
    real = mode == "model"
    return {
        "status": "PASS" if completed else "ERROR",
        "trials": trials,
        "errors": errors,
        "execution": {
            "mode": mode,
            "tier": "real_llm_investigation" if real else "workflow_fake",
            "model": "per-slot-env" if real else "scripted-fake",
            "tools": "mcp" if real else "synthetic-fake",
            "sandbox": "disabled",
            "network": "required" if real else "none",
        },
    }


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: _incidentpilot_matrix.py <incidentpilot_root> <spec.json> "
            "<output.json> <offline|model>",
            file=sys.stderr,
        )
        return 2
    root = Path(argv[0]).resolve()
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    output = Path(argv[2]).resolve()
    mode = argv[3]
    if mode not in {"offline", "model"}:
        print(f"unknown matrix mode: {mode}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(root / "src"))
    payload: dict = {"status": "ERROR", "trials": [], "errors": [], "execution": {"mode": mode}}
    try:
        payload = asyncio.run(main_async(root, spec, mode, dict(os.environ)))
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
