"""平台化阶段项目 4b：2×2 矩阵执行与汇总测试。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentreliability.matrix import (
    STATE_MODEL_CONFIG,
    STATE_REAL_ENV_UNAVAILABLE,
    STATE_REAL_RESULT_INVALID,
    STATE_SPEC_INVALID,
    STATE_TIMEOUT,
    STATE_UPSTREAM_FAILURE,
    load_spec,
    matrix_exit_code,
    real_model_config,
    run_matrix,
    summarize,
    validate_real_result,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_INCIDENTPILOT = Path.home() / "Developer" / "GitHub" / "incidentpilot"
SPEC = REPO_ROOT / "benchmarks" / "matrix" / "pawbench-2x2.json"


def _require_incidentpilot() -> None:
    if not (REAL_INCIDENTPILOT / ".venv" / "bin" / "python").exists():
        pytest.skip("本机缺少固定版本的 IncidentPilot checkout")


def _trial(model_id: str, strategy: str, scenario: str, trial: int, correct: bool) -> dict:
    return {
        "model_id": model_id,
        "strategy": strategy,
        "scenario": scenario,
        "trial": trial,
        "status": "PASS",
        "duration_seconds": 1.0,
        "usage": {"tokens": None},
        "result": {
            "root_cause_correct": correct,
            "evidence_supported": True,
            "tool_selection_score": 1.0,
        },
    }


def _runner(payload: dict | None, returncode: int = 0):
    def run(command, **kwargs):
        if payload is not None:
            out = Path(command[4])
            out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr="")

    return run


def _timeout_runner(command, **kwargs):
    raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout", 1))


def _payload(trials: list[dict] | None = None) -> dict:
    return {"status": "PASS", "trials": trials or [], "errors": []}


def test_spec_is_2x2_with_two_repetitions():
    spec, path = load_spec(SPEC)
    assert len(spec["models"]) == 2
    assert spec["strategies"] == ["fixed", "autonomous"]
    assert len(spec["scenarios"]) == 2
    assert spec["repetitions"] >= 2
    assert spec["reference"]["commit"] == "0f794a8bb6c27aa9ee4091b2691fa30e4ed9cc8f"


def test_invalid_spec_rejected(tmp_path):
    bad = json.loads(SPEC.read_text(encoding="utf-8"))
    bad["models"] = bad["models"][:1]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        load_spec(path)


def test_summary_groups_model_and_strategy():
    trials = []
    for model in ("model-a", "model-b"):
        for strategy in ("fixed", "autonomous"):
            for scenario in ("s1", "s2"):
                for trial in (1, 2):
                    trials.append(_trial(model, strategy, scenario, trial, trial == 1))
    summary = summarize(trials)
    assert summary["trials"] == 16
    assert summary["pass"] == 16
    assert len(summary["groups"]) == 4
    assert summary["groups"][0]["root_cause_accuracy"] == 0.5
    assert set(summary["by_scenario"]) == {"s1", "s2"}


def test_summary_counts_and_traces_diagnosis_source():
    base = {
        "model_id": "model-a",
        "strategy": "fixed",
        "scenario": "s1",
        "status": "PASS",
        "duration_seconds": 1.0,
        "usage": {"tokens": None},
    }
    trials = []

    def add(trial: int, expected: str, predicted: str, correct: bool, alias: bool, category_ok: bool) -> None:
        item = dict(base, trial=trial)
        item["result"] = {
            "root_cause_correct": correct,
            "fault_category_correct": category_ok,
            "expected_category": expected,
            "predicted_category": predicted,
            "category_alias_covered": alias,
            "tool_selection_score": 1.0,
        }
        trials.append(item)

    add(1, "database_performance", "database_performance", True, True, True)
    add(2, "database_performance", "code_or_database_regression", False, False, False)
    add(3, "database_performance", "code_or_database_regression", False, False, False)
    add(4, "database_performance", "slow_database_query", False, True, False)

    summary = summarize(trials)
    assert summary["workflow_completed"] == 4
    assert summary["judgeable"] == 4
    assert summary["root_cause_correct"] == 1
    assert summary["root_cause_incorrect"] == 3
    assert summary["error"] == 0
    assert summary["root_cause_discrepancy"]["by_source"] == {
        "model_output": 2,
        "scoring_mapping": 1,
    }
    trace = {item["predicted_category"]: item for item in summary["diagnosis_trace"]}
    assert trace["code_or_database_regression"]["source"] == "model_output"
    assert trace["slow_database_query"]["source"] == "scoring_mapping"
    assert trace["database_performance"]["source"] == "matches_ground_truth"


def test_real_model_gate_lists_missing_configs():
    spec, _ = load_spec(SPEC)
    config = real_model_config(spec, environ={})
    assert config["configured"] is False
    assert "PAWBENCH_MODEL_A_API_KEY" in config["missing"]
    assert "PAWBENCH_MODEL_B_MODEL" in config["missing"]


def test_offline_matrix_executes_and_summarizes(tmp_path):
    _require_incidentpilot()
    manifest, run_dir = run_matrix(
        out=tmp_path,
        mode="offline",
        incidentpilot_dir=REAL_INCIDENTPILOT,
        timeout=900,
        environ={},
    )
    assert manifest["status"] == "PASS"
    assert manifest["summary"]["trials"] == 16
    assert manifest["summary"]["expected_trials"] == 16
    assert len(manifest["summary"]["groups"]) == 4
    assert manifest["execution_mode"] == "offline-scripted"
    assert manifest["real_matrix"]["status"] == "NOT_RUN"
    assert manifest["target"]["commit_verified"] is True
    assert (run_dir / "trials.json").exists()
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "/Users/" not in html
    assert "2×2" in html


def _complete_model_env() -> dict[str, str]:
    environ: dict[str, str] = {}
    for slot in ("A", "B"):
        environ[f"PAWBENCH_MODEL_{slot}_API_KEY"] = "placeholder-key"
        environ[f"PAWBENCH_MODEL_{slot}_MODEL"] = f"placeholder-model-{slot.lower()}"
        environ[f"PAWBENCH_MODEL_{slot}_BASE_URL"] = "http://127.0.0.1:9/v1"
    return environ


def _scripted_model_payload() -> dict:
    """一段“配置齐全但产物仍是脚本化替身”的伪造结果。"""
    trials = []
    for model in ("model-a", "model-b"):
        for strategy in ("fixed", "autonomous"):
            for scenario in (
                "fault_002_slow_database_query",
                "fault_001_n_plus_one_orders",
            ):
                for trial in (1, 2):
                    trials.append(
                        {
                            "model_id": model,
                            "model_backend": "scripted-fake",
                            "model_identifier": "scripted-fake",
                            "strategy": strategy,
                            "scenario": scenario,
                            "trial": trial,
                            "status": "PASS",
                            "duration_seconds": 1.0,
                            "usage": {"tokens": None},
                            "result": {
                                "root_cause_correct": True,
                                "tool_selection_score": 1.0,
                            },
                        }
                    )
    return {
        "status": "PASS",
        "trials": trials,
        "errors": [],
        "execution": {
            "mode": "model",
            "tier": "workflow_fake",
            "model": "scripted-fake",
            "tools": "synthetic-fake",
        },
    }


def test_model_mode_rejects_scripted_artifacts_even_with_full_config(tmp_path):
    manifest, _ = run_matrix(
        out=tmp_path,
        mode="model",
        incidentpilot_dir=REAL_INCIDENTPILOT,
        runner=_runner(_scripted_model_payload()),
        reference_probe=lambda url, timeout: (True, "HTTP 200"),
        environ=_complete_model_env(),
    )
    assert manifest["status"] != "PASS"
    assert manifest["state"] == STATE_REAL_RESULT_INVALID
    assert manifest["real_matrix"]["status"] != "PASS"
    assert manifest["real_matrix"]["validation"]["ok"] is False
    assert matrix_exit_code(manifest["status"]) == 2


def test_model_mode_with_unavailable_environment_is_not_run(tmp_path):
    manifest, _ = run_matrix(
        out=tmp_path,
        mode="model",
        incidentpilot_dir=REAL_INCIDENTPILOT,
        reference_probe=lambda url, timeout: (False, "connection refused"),
        environ=_complete_model_env(),
    )
    assert manifest["status"] == "NOT_RUN"
    assert manifest["state"] == STATE_REAL_ENV_UNAVAILABLE
    assert manifest["real_matrix"]["status"] == "NOT_RUN"
    assert manifest["trials"] == []
    assert matrix_exit_code(manifest["status"]) == 2


def test_validate_real_result_rejects_slot_model_mismatch():
    spec, _ = load_spec(SPEC)
    environ = _complete_model_env()
    config = real_model_config(spec, environ)
    payload = {"execution": {"mode": "model", "tier": "real_llm_investigation"}}
    trial = {
        "model_id": "model-a",
        "model_backend": "real-llm",
        "model_identifier": "placeholder-model-b",
        "provider_model": {"provider": "openai", "model": "placeholder-model-b"},
        "execution": {
            "tier": "real_llm_investigation",
            "llm_enabled": True,
            "tool_backend": "mcp",
        },
        "status": "PASS",
    }
    issues = validate_real_result(spec, config, [trial], payload)
    assert any("模型标识" in item for item in issues)
    assert any("provider 模型" in item for item in issues)


def test_model_mode_without_config_is_not_run(tmp_path):
    manifest, _ = run_matrix(
        out=tmp_path,
        mode="model",
        incidentpilot_dir=REAL_INCIDENTPILOT,
        environ={},
    )
    assert manifest["status"] == "NOT_RUN"
    assert manifest["state"] == STATE_MODEL_CONFIG
    assert "PAWBENCH_MODEL_A_API_KEY" in manifest["error"]


def test_spec_invalid_state(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    manifest, _ = run_matrix(
        out=tmp_path / "out", incidentpilot_dir=REAL_INCIDENTPILOT, spec_path=bad, environ={}
    )
    assert manifest["state"] == STATE_SPEC_INVALID


def test_timeout_and_partial_matrix(tmp_path):
    manifest, _ = run_matrix(
        out=tmp_path / "one",
        incidentpilot_dir=REAL_INCIDENTPILOT,
        runner=_timeout_runner,
        environ={},
    )
    assert manifest["state"] == STATE_TIMEOUT
    manifest, _ = run_matrix(
        out=tmp_path / "two",
        incidentpilot_dir=REAL_INCIDENTPILOT,
        runner=_runner(_payload([_trial("model-a", "fixed", "s1", 1, True)])),
        environ={},
    )
    assert manifest["status"] == "ERROR"
    assert manifest["state"] == STATE_UPSTREAM_FAILURE
    assert manifest["summary"]["trials"] == 1


def test_upstream_exit_without_output(tmp_path):
    manifest, _ = run_matrix(
        out=tmp_path,
        incidentpilot_dir=REAL_INCIDENTPILOT,
        runner=_runner(None, returncode=1),
        environ={},
    )
    assert manifest["state"] == STATE_UPSTREAM_FAILURE
