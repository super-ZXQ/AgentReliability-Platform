"""发布候选离线门禁：三类误报边界的纯本地回归。

不依赖真实模型凭据、外部服务或上游 checkout，可在 PR CI 中稳定运行。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from agentreliability import quality
from agentreliability.aig import build_source_zip, run_aig_scan
from agentreliability.matrix import (
    ERROR,
    NOT_RUN,
    PASS,
    STATE_REAL_ENV_UNAVAILABLE,
    STATE_REAL_RESULT_INVALID,
    load_spec,
    matrix_exit_code,
    real_model_config,
    real_result_verdict,
    validate_real_result,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "benchmarks" / "matrix" / "pawbench-2x2.json"


def _complete_model_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for slot in ("A", "B"):
        env[f"PAWBENCH_MODEL_{slot}_API_KEY"] = "placeholder"
        env[f"PAWBENCH_MODEL_{slot}_MODEL"] = f"placeholder-{slot.lower()}"
        env[f"PAWBENCH_MODEL_{slot}_BASE_URL"] = "http://127.0.0.1:9/v1"
    return env


def _scripted_trials() -> list[dict]:
    trials = []
    for model in ("model-a", "model-b"):
        for strategy in ("fixed", "autonomous"):
            for scenario in ("fault_002_slow_database_query", "fault_001_n_plus_one_orders"):
                for trial in (1, 2):
                    trials.append(
                        {
                            "model_id": model,
                            "model_backend": "scripted-fake",
                            "model_identifier": "scripted-fake",
                            "strategy": strategy,
                            "scenario": scenario,
                            "trial": trial,
                            "status": PASS,
                        }
                    )
    return trials


def test_matrix_scripted_artifacts_cannot_produce_real_pass():
    spec, _ = load_spec(SPEC)
    config = real_model_config(spec, _complete_model_env())
    trials = _scripted_trials()
    payload = {
        "status": PASS,
        "execution": {"mode": "model", "tier": "workflow_fake", "model": "scripted-fake"},
    }

    issues = validate_real_result(spec, config, trials, payload)
    assert any("执行来源" in issue for issue in issues)
    assert any("执行层级" in issue for issue in issues)

    status, state, _ = real_result_verdict(trials, payload, issues)
    assert status == ERROR
    assert state == STATE_REAL_RESULT_INVALID
    assert matrix_exit_code(status) == 2


def test_matrix_missing_real_executor_is_not_run_exit_two():
    payload = {"status": "ERROR", "execution": {"mode": "model"}}
    status, state, _ = real_result_verdict([], payload, [])
    assert status == NOT_RUN
    assert state == STATE_REAL_ENV_UNAVAILABLE
    assert matrix_exit_code(status) == 2


def _diagnosis(
    *,
    verified: bool = True,
    causal_facts_match: bool = True,
    evidence_ids: list[str] | None = None,
    evidence_bound: bool = True,
    evidence_supports: bool = True,
) -> dict:
    ids = ["EVD-1"] if evidence_ids is None else list(evidence_ids)
    return {
        "root_cause": {
            "summary": "orders-api degradation is caused by dependency timeout configuration",
            "verified": verified,
        },
        "verification": {
            "diagnosis_present": True,
            "root_cause_verified": verified,
            "causal_facts_match": causal_facts_match,
            "expected_category_match": True,
            "expected_component_match": True,
            "evidence_ids": ids,
            "evidence_bound_to_diagnosis": evidence_bound and bool(ids),
            "evidence_supports_conclusion": evidence_supports and bool(ids),
            "expected_evidence_sources_covered": ["read_metrics"],
        },
    }


def test_causal_fact_coverage_rejects_generic_wrong_explanation():
    from agentreliability._incidentpilot_run import _causal_facts_coverage

    ground_truth = {
        "causal_facts": [
            "slow database path adds request delay",
            "orders endpoint P95 latency increases",
        ]
    }
    wrong = {
        "summary": "orders-api degradation is caused by dependency timeout configuration",
        "causal_facts": ["orders-api degradation is caused by dependency timeout configuration"],
    }
    correct = {
        "summary": "/orders handler sleeps on a slow database query, raising latency",
        "causal_facts": ["slow database query adds request delay"],
    }
    assert _causal_facts_coverage(ground_truth, wrong)[0] is False
    assert _causal_facts_coverage(ground_truth, correct)[0] is True


def test_quality_wrong_root_cause_with_matching_category_requires_review():
    assessment = quality._diagnosis_assessment(_diagnosis(causal_facts_match=False))
    assert assessment["status"] == quality.REVIEW_REQUIRED
    assert "关键因果事实与金标不一致" in assessment["issues"]
    manifest = {"summary": {"verdict": quality.REVIEW_REQUIRED}}
    assert quality.quality_exit_code(manifest) == 1


def test_quality_missing_citations_requires_review():
    assessment = quality._diagnosis_assessment(_diagnosis(evidence_ids=[]))
    assert assessment["status"] == quality.REVIEW_REQUIRED
    assert "缺少支撑诊断的证据" in assessment["issues"]
    assert "证据未绑定到诊断结论" in assessment["issues"]


def test_quality_irrelevant_citations_requires_review():
    assessment = quality._diagnosis_assessment(_diagnosis(evidence_supports=False))
    assert assessment["status"] == quality.REVIEW_REQUIRED
    assert "实际引用的证据不足以支持结论" in assessment["issues"]


def test_quality_unverified_root_cause_requires_review():
    assessment = quality._diagnosis_assessment(_diagnosis(verified=False))
    assert assessment["status"] == quality.REVIEW_REQUIRED
    assert "根因未经上游验证（verified=False）" in assessment["issues"]


def test_aig_symlink_outside_root_never_enters_zip_or_upload(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("OUTSIDE_SECRET = 1\n", encoding="utf-8")
    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "server.py").write_text("print('safe')\n", encoding="utf-8")
    (sample / "linked.py").symlink_to(outside / "secret.py")

    report = build_source_zip(sample, tmp_path / "out.zip")
    assert report["files"] == ["server.py"]
    assert {"path": "linked.py", "reason": "symlink"} in report["rejected_files"]
    with zipfile.ZipFile(tmp_path / "out.zip") as archive:
        assert archive.namelist() == ["server.py"]
        assert b"OUTSIDE_SECRET" not in b"".join(
            archive.read(name) for name in archive.namelist()
        )

    captured: dict[str, bytes] = {}

    def transport(method, url, body, headers, timeout):
        if "/upload" in url:
            assert "linked.py" not in body.decode("latin-1")
            captured["body"] = body
            return (
                200,
                json.dumps(
                    {
                        "status": 0,
                        "message": "ok",
                        "data": {
                            "fileUrl": "http://aig/uploads/source.zip",
                            "filename": "source.zip",
                            "size": len(body),
                        },
                    }
                ).encode(),
                {},
            )
        if url.endswith("/tasks"):
            return 200, json.dumps({"status": 0, "message": "ok", "data": {"session_id": "S-1"}}).encode(), {}
        if "/status/" in url:
            return 200, json.dumps({"status": 0, "message": "ok", "data": {"status": "completed"}}).encode(), {}
        if "/result/" in url:
            payload = {"status": 0, "message": "ok", "data": {"report": "done", "vulnerabilities": []}}
            return 200, json.dumps(payload).encode(), {}
        raise AssertionError(url)

    manifest, _ = run_aig_scan(
        out=tmp_path / "artifacts",
        base_url="http://aig.test",
        timeout=1.0,
        poll_interval=0.0,
        max_poll_seconds=1.0,
        max_poll_attempts=2,
        sample_dir=sample,
        transport=transport,
        environ={},
    )
    assert manifest["status"] == PASS
    assert manifest["sample"]["files"] == ["server.py"]
    body = captured["body"]
    start = body.index(b"\r\n\r\n") + 4
    end = body.rindex(b"\r\n--")
    with zipfile.ZipFile(io.BytesIO(body[start:end])) as archive:
        assert archive.namelist() == ["server.py"]
        assert b"OUTSIDE_SECRET" not in archive.read("server.py")
