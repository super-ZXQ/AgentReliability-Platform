"""平台化阶段项目 2：quality 入口与 VibeCheck-AI 适配器测试。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentreliability import quality
from agentreliability.vibecheck import (
    STATE_MISSING_ARTIFACTS,
    STATE_REVISION_MISMATCH,
    STATE_TIMEOUT,
    STATE_UPSTREAM_FAILURE,
    VIBECHECK_AI_COMMIT,
    run_vibecheck_scan,
)
from agentreliability.incidentpilot import (
    INCIDENTPILOT_COMMIT,
    STATE_UNAVAILABLE as INCIDENT_UNAVAILABLE,
    run_incidentpilot_investigation,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_VIBECHECK = Path.home() / "Developer" / "GitHub" / "vibecheck-ai"
REAL_INCIDENTPILOT = Path.home() / "Developer" / "GitHub" / "incidentpilot"


def _require_real_tools() -> None:
    if not (REAL_VIBECHECK / "backend" / ".venv" / "bin" / "python").exists():
        pytest.skip("本机缺少固定版本的 VibeCheck-AI checkout")
    if not REAL_INCIDENTPILOT.exists():
        pytest.skip("本机缺少固定版本的 IncidentPilot checkout")


def _require_docker() -> None:
    completed = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("本机 Docker 守护进程不可用")


def _incident_payload(
    *, baseline_status: str = "FAIL", retest_status: str = "PASS"
) -> dict:
    baseline = {
        "name": "fault_active_latency",
        "status": baseline_status,
        "expected": "FAIL",
        "expected_matched": baseline_status == "FAIL",
        "threshold_ms": 250.0,
        "observed_ms": 500.0 if baseline_status == "FAIL" else 40.0,
        "samples_ms": [510.0, 520.0, 490.0],
        "payload_equal_to_healthy": True,
        "linked_run_id": "RUN-test0001",
        "linked_incident_id": 7,
        "conclusion": "故障复现" if baseline_status == "FAIL" else "故障未复现",
    }
    investigation = {
        "status": "WAITING_APPROVAL",
        "workflow_state": "WAIT_FOR_APPROVAL",
        "run_id": "RUN-test0001",
        "incident_id": 7,
        "trace_id": "TRACE-test",
        "root_cause": {
            "summary": "orders-api degradation is caused by dependency timeout configuration",
            "fault_category": "code_or_database_regression",
            "affected_component": "orders-api",
            "confidence": "MEDIUM",
        },
        "hypotheses": [],
        "evidence": [
            {
                "evidence_id": "EVD-1",
                "source": "metrics",
                "source_type": "tool",
                "tool_call_id": "TC-1",
                "summary": "latency spike",
                "content_hash": "hash-1",
            }
        ],
        "tool_calls": [
            {
                "tool_call_id": "TC-1",
                "tool_name": "read_metrics",
                "status": "SUCCEEDED",
                "latency_ms": 12.0,
            }
        ],
        "patch_artifact": {
            "patch_artifact_id": "PA-test",
            "base_commit_sha": "566639ea876eed171c705ad0c1a0d79276b8584b",
            "patch_hash": "hash-patch",
            "patch_summary": "dependency timeout configuration",
            "test_run_id": "TR-1",
            "patch_diff": '{"type":"remove_fault"}',
        },
        "test_runs": [
            {
                "test_run_id": "TR-1",
                "status": "PASSED",
                "exit_code": 0,
                "duration_ms": 600.0,
                "mode": "docker",
                "base_commit_sha": "566639ea876eed171c705ad0c1a0d79276b8584b",
            }
        ],
        "verification": {
            "diagnosis_present": True,
            "evidence_ids": ["EVD-1"],
            "evidence_bound_to_diagnosis": True,
            "expected_category_match": False,
            "expected_component_match": True,
            "expected_evidence_sources_covered": ["read_metrics"],
        },
        "review": {
            "status": "PENDING_HUMAN_REVIEW",
            "human_approval": "NOT_RUN",
            "note": "等待人工审核",
        },
    }
    test = {
        "profile": "pytest_fault",
        "scope": "fault-active",
        "status": "PASS",
        "exit_code": 0,
        "duration_ms": 500.0,
        "mode": "docker",
    }
    healthy = {
        "profile": "pytest_regression",
        "scope": "healthy-path",
        "status": "PASS",
        "exit_code": 0,
        "duration_ms": 500.0,
        "mode": "docker",
    }
    retest = {
        "status": retest_status,
        "state": None,
        "baseline_sha": "566639ea876eed171c705ad0c1a0d79276b8584b",
        "patch_summary": "dependency timeout configuration",
        "workspace": "isolated-temp",
        "fault_active": test,
        "healthy_regression": healthy,
    }
    if retest_status == "NOT_RUN":
        retest.update(
            {
                "state": "sandbox_unavailable",
                "error": "Docker 不可用",
                "fault_active": None,
                "healthy_regression": None,
            }
        )
    return {
        "status": "PASS",
        "baseline": baseline,
        "investigation": investigation,
        "sandbox_retest": retest,
        "execution": {"model": "scripted-fake", "tools": "synthetic-fake", "sandbox": "docker"},
    }


def _incident_runner(payload: dict | None = None, returncode: int = 0):
    def runner(command, **kwargs):
        if payload is not None:
            out = Path(command[3])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr="")

    return runner


def _payload(findings: list[dict] | None = None) -> dict:
    return {
        "scanner": {"module": "app.scanner.sensitive", "function": "scan_directory"},
        "scan_root": "<scan-root>",
        "total_files_scanned": 1,
        "total_lines_scanned": 2,
        "findings": findings or [],
        "notices": [],
        "skipped_files": [],
        "scan_errors": [],
    }


def _fake_runner(*, payload: dict | None = None, returncode: int = 0, stderr: str = ""):
    def runner(command, **kwargs):
        if payload is not None:
            out = Path(command[3])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr=stderr)

    return runner


def _timeout_runner(command, **kwargs):
    raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout", 1))


def _synthetic_finding(**overrides) -> dict:
    finding = {
        "rule_id": "R001_GITHUB_TOKEN",
        "rule_name": "GitHub Token",
        "severity": "high",
        "confidence": "high",
        "dimension": "sensitive_data_security",
        "file_path": "config.py",
        "line_start": 1,
        "line_end": 1,
        "column_start": 10,
        "column_end": 48,
        "snippet_masked": "ghp_****",
        "is_blocking": True,
        "finding_type": "content",
        "category": "token",
        "secret_type": "github_token",
    }
    finding.update(overrides)
    return finding


def _run_quality(tmp_path: Path, **kwargs):
    return quality.run_quality(
        scenario_id="orders-slow-query",
        out=tmp_path / "quality",
        workspace_root=REPO_ROOT,
        environ={},
        **kwargs,
    )


def _manifest_from(subtasks_scan_state: str) -> dict:
    return {
        "run_id": "manual",
        "scenario": {
            "id": "orders-slow-query",
            "title": "慢查询",
            "source": {"repository": "repo", "commit": "a" * 40, "path": "reference/orders_api"},
            "injected_fault": {
                "id": "fault_002_slow_database_query",
                "type": "slow_database_query",
                "simulated": True,
                "delay_seconds": 0.5,
            },
        },
        "target": {"repository": "repo", "commit": "a" * 40, "status": "PASS"},
        "subtasks": [
            {
                "name": "static_scan",
                "status": "PASS",
                "state": subtasks_scan_state,
                "execution_mode": "real-service",
                "tool": {"repository": "repo", "actual_commit": "b" * 40},
                "artifacts": [{"path": "scan.json"}],
                "findings_count": 1,
                "error": None,
            }
        ],
        "summary": {"total": 1, "pass": 1, "fail": 0, "error": 0, "not_run": 0, "verdict": "PASS"},
    }


def test_real_scan_detects_synthetic_hit_and_masks_it(tmp_path):
    _require_real_tools()
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    (tmp_path / "config.py").write_text(f'GITHUB_TOKEN = "{token}"\n', encoding="utf-8")

    report = run_vibecheck_scan(
        vibecheck_dir=REAL_VIBECHECK,
        scan_root=tmp_path,
        out_raw=tmp_path / "scan.raw.json",
        source_label="synthetic",
        environ={},
    )

    assert report.status == "PASS"
    assert report.execution_mode == "real-service"
    assert report.tool["commit_verified"] is True
    assert report.tool["actual_commit"] == VIBECHECK_AI_COMMIT
    assert report.scope["files_scanned"] == 1
    assert report.scope["findings_count"] >= 1
    rule = [item for item in report.findings if item["rule_id"] == "R001_GITHUB_TOKEN"]
    assert len(rule) == 1
    assert rule[0]["file_path"] == "config.py"
    assert rule[0]["line_start"] == 1
    assert rule[0]["severity"] in {"critical", "high"}
    for finding in report.findings:
        assert token not in finding["snippet_masked"]


def test_quality_manifest_traces_to_real_scan(tmp_path):
    _require_real_tools()
    _require_docker()
    manifest, run_dir = _run_quality(
        tmp_path,
        incidentpilot_dir=REAL_INCIDENTPILOT,
        vibecheck_dir=REAL_VIBECHECK,
    )

    assert quality.quality_exit_code(manifest) == 0
    assert manifest["summary"]["verdict"] == "PASS"
    assert manifest["target"]["commit_verified"] is True
    assert manifest["scenario"]["source"]["commit"] == (
        "566639ea876eed171c705ad0c1a0d79276b8584b"
    )
    assert manifest["scenario"]["injected_fault"]["simulated"] is True
    subtask = manifest["subtasks"][0]
    assert subtask["status"] == "PASS"
    assert subtask["execution_mode"] == "real-service"
    assert subtask["scope"]["source"] == "reference/orders_api"
    assert subtask["scope"]["files_scanned"] > 0

    scan = json.loads((run_dir / "scan.json").read_text(encoding="utf-8"))
    assert len(scan["findings"]) == subtask["findings_count"]
    assert scan["raw_artifact"] == "scan.raw.json"
    scan_artifact = next(a for a in subtask["artifacts"] if a["path"] == "scan.json")
    from agentreliability.report import sha256_file

    assert scan_artifact["sha256"] == sha256_file(run_dir / "scan.json")
    assert (run_dir / "scan.raw.json").exists()

    html_text = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "VibeCheck-AI" in html_text
    assert "注入的模拟延迟" in html_text
    assert "/Users/" not in html_text
    assert "/var/folders" not in html_text

    names = [item["name"] for item in manifest["subtasks"]]
    assert names == ["static_scan", "failure_assertion", "investigation", "sandbox_retest"]
    statuses = {item["name"]: item for item in manifest["subtasks"]}
    assert statuses["failure_assertion"]["status"] == "FAIL"
    assert statuses["failure_assertion"]["expected_matched"] is True
    assert statuses["failure_assertion"]["assertion"]["observed_ms"] >= 250
    assert statuses["investigation"]["status"] == "PASS"
    assert statuses["investigation"]["external_run_id"].startswith("RUN-")
    assert statuses["sandbox_retest"]["status"] == "PASS"
    assert statuses["sandbox_retest"]["fault_active"]["mode"] == "docker"
    assert statuses["sandbox_retest"]["healthy_regression"]["mode"] == "docker"
    assert manifest["review"]["status"] == "PENDING_HUMAN_REVIEW"
    assert manifest["review"]["human_approval"] == "NOT_RUN"

    failure = manifest["failure_assessment"]
    evidence_ids = {item["evidence_id"] for item in manifest["investigation"]["evidence"]}
    assert failure["linked_run_id"] == statuses["investigation"]["external_run_id"]
    assert set(failure["supporting_evidence_ids"]) <= evidence_ids

    separate = manifest["separate_regressions"]
    assert separate["commit"] == json.loads(
        (REPO_ROOT / "target.lock.json").read_text(encoding="utf-8")
    )["commit"]
    assert (run_dir / "failure-assertion.json").exists()
    assert (run_dir / "investigation.json").exists()
    assert (run_dir / "sandbox-retest.json").exists()


def test_rerun_creates_new_run_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        quality,
        "resolve_incidentpilot_dir",
        lambda explicit=None, environ=None: REAL_INCIDENTPILOT,
    )
    fake = _fake_runner(payload=_payload([_synthetic_finding()]))
    first, first_dir = _run_quality(
        tmp_path,
        vibecheck_dir=REAL_VIBECHECK,
        scan_runner=fake,
        incidentpilot_runner=_incident_runner(_incident_payload()),
    )
    second, second_dir = _run_quality(
        tmp_path,
        vibecheck_dir=REAL_VIBECHECK,
        scan_runner=fake,
        incidentpilot_runner=_incident_runner(_incident_payload()),
    )

    assert quality.quality_exit_code(first) == 0
    assert first_dir != second_dir
    assert first_dir.parent == second_dir.parent
    assert len(list(first_dir.parent.iterdir())) == 2
    assert first["subtasks"][0]["execution_mode"] == "test-double"


def test_revision_mismatch_has_explicit_state(tmp_path):
    repo = tmp_path / "vibecheck"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "README.md").write_text("wrong revision\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    manifest, _ = _run_quality(
        tmp_path,
        incidentpilot_dir=REAL_INCIDENTPILOT,
        vibecheck_dir=repo,
        scan_runner=_fake_runner(payload=_payload()),
        incidentpilot_runner=_incident_runner(_incident_payload()),
    )

    assert quality.quality_exit_code(manifest) == 2
    subtask = manifest["subtasks"][0]
    assert subtask["status"] == "ERROR"
    assert subtask["state"] == STATE_REVISION_MISMATCH
    assert "提交不匹配" in subtask["error"]


def test_missing_artifacts_is_not_a_pass(tmp_path):
    _require_real_tools()
    manifest, _ = _run_quality(
        tmp_path,
        incidentpilot_dir=REAL_INCIDENTPILOT,
        vibecheck_dir=REAL_VIBECHECK,
        scan_runner=_fake_runner(payload=None),
        incidentpilot_runner=_incident_runner(_incident_payload()),
    )

    assert quality.quality_exit_code(manifest) == 2
    assert manifest["subtasks"][0]["state"] == STATE_MISSING_ARTIFACTS


def test_upstream_exit_failure_is_not_a_pass(tmp_path):
    _require_real_tools()
    manifest, _ = _run_quality(
        tmp_path,
        incidentpilot_dir=REAL_INCIDENTPILOT,
        vibecheck_dir=REAL_VIBECHECK,
        scan_runner=_fake_runner(payload=_payload(), returncode=3, stderr="boom"),
        incidentpilot_runner=_incident_runner(_incident_payload()),
    )

    assert quality.quality_exit_code(manifest) == 2
    assert manifest["subtasks"][0]["state"] == STATE_UPSTREAM_FAILURE
    assert "退出码 3" in manifest["subtasks"][0]["error"]


def test_timeout_is_not_a_pass(tmp_path):
    _require_real_tools()
    manifest, _ = _run_quality(
        tmp_path,
        incidentpilot_dir=REAL_INCIDENTPILOT,
        vibecheck_dir=REAL_VIBECHECK,
        scan_runner=_timeout_runner,
        incidentpilot_runner=_incident_runner(_incident_payload()),
    )

    assert quality.quality_exit_code(manifest) == 2
    assert manifest["subtasks"][0]["state"] == STATE_TIMEOUT


def test_html_escapes_untrusted_fields(tmp_path):
    manifest = _manifest_from("PASS")
    scan = {
        "scanner": {"module": "app.scanner.sensitive", "function": "scan_directory"},
        "status": "PASS",
        "state": None,
        "execution_mode": "real-service",
        "tool": {"repository": "repo", "actual_commit": "b" * 40},
        "scope": {"source": "reference/orders_api", "findings_count": 1},
        "findings": [
            _synthetic_finding(
                rule_id="<script>alert(1)</script>",
                snippet_masked="<img src=x onerror=alert(2)>",
            )
        ],
        "raw_artifact": "scan.raw.json",
    }
    html_text = quality.render_quality_html(manifest, scan)

    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text
    assert "<img src=x onerror=alert(2)>" not in html_text


def test_source_checkout_unavailable_is_reported(tmp_path):
    not_repo = tmp_path / "incidentpilot"
    not_repo.mkdir()
    manifest, run_dir = _run_quality(
        tmp_path,
        incidentpilot_dir=not_repo,
        vibecheck_dir=REAL_VIBECHECK,
        scan_runner=_fake_runner(payload=_payload()),
    )

    assert quality.quality_exit_code(manifest) == 2
    assert manifest["target"]["status"] == "ERROR"
    assert manifest["subtasks"][0]["status"] == "ERROR"
    assert manifest["subtasks"][0]["state"] == "source_missing"
    assert (run_dir / "manifest.json").exists()


def test_closure_requires_expected_baseline_failure(tmp_path):
    _require_real_tools()
    manifest, _ = _run_quality(
        tmp_path,
        incidentpilot_dir=REAL_INCIDENTPILOT,
        vibecheck_dir=REAL_VIBECHECK,
        scan_runner=_fake_runner(payload=_payload()),
        incidentpilot_runner=_incident_runner(
            _incident_payload(baseline_status="PASS")
        ),
    )

    assert manifest["summary"]["baseline_expected_failure_matched"] is False
    assert manifest["summary"]["verdict"] == "FAIL"
    assert quality.quality_exit_code(manifest) == 1
    assert manifest["review"]["status"] == "PENDING_HUMAN_REVIEW"


def test_docker_unavailable_never_passes(tmp_path):
    _require_real_tools()
    manifest, _ = _run_quality(
        tmp_path,
        incidentpilot_dir=REAL_INCIDENTPILOT,
        vibecheck_dir=REAL_VIBECHECK,
        scan_runner=_fake_runner(payload=_payload()),
        incidentpilot_runner=_incident_runner(
            _incident_payload(retest_status="NOT_RUN")
        ),
    )

    assert manifest["subtasks"][3]["name"] == "sandbox_retest"
    assert manifest["subtasks"][3]["status"] == "NOT_RUN"
    assert manifest["subtasks"][3]["state"] == "sandbox_unavailable"
    assert manifest["summary"]["verdict"] == "ERROR"
    assert quality.quality_exit_code(manifest) == 2


def test_incidentpilot_revision_mismatch_is_explicit(tmp_path):
    repo = tmp_path / "incidentpilot"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "README.md").write_text("wrong revision\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    report = run_incidentpilot_investigation(
        incidentpilot_dir=repo,
        out_raw=tmp_path / "raw.json",
        runner=_incident_runner(_incident_payload()),
        environ={},
    )

    assert report.status == "ERROR"
    assert report.state == "revision_mismatch"
    assert report.tool["actual_commit"] != INCIDENTPILOT_COMMIT


def test_incidentpilot_missing_artifact_and_timeout(tmp_path):
    _require_real_tools()
    missing = run_incidentpilot_investigation(
        incidentpilot_dir=REAL_INCIDENTPILOT,
        out_raw=tmp_path / "missing.json",
        runner=_incident_runner(payload=None),
        environ={},
    )
    assert missing.status == "ERROR"
    assert missing.state == "missing_artifacts"

    def timeout_runner(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout", 1))

    timed_out = run_incidentpilot_investigation(
        incidentpilot_dir=REAL_INCIDENTPILOT,
        out_raw=tmp_path / "timeout.json",
        runner=timeout_runner,
        environ={},
    )
    assert timed_out.status == "ERROR"
    assert timed_out.state == "timeout"


def test_incidentpilot_upstream_failure_is_not_pass(tmp_path):
    _require_real_tools()
    report = run_incidentpilot_investigation(
        incidentpilot_dir=REAL_INCIDENTPILOT,
        out_raw=tmp_path / "failed.json",
        runner=_incident_runner(
            {"status": "ERROR", "error": "workflow exploded", "investigation": {}},
            returncode=1,
        ),
        environ={},
    )
    assert report.status == "ERROR"
    assert "workflow exploded" in report.error


def test_unavailable_incidentpilot_is_not_pass(tmp_path):
    missing_dir = tmp_path / "nowhere"
    report = run_incidentpilot_investigation(
        incidentpilot_dir=missing_dir,
        out_raw=tmp_path / "raw.json",
        runner=_incident_runner(_incident_payload()),
        environ={},
    )
    assert report.status == "ERROR"
    assert report.state == INCIDENT_UNAVAILABLE


def test_quality_html_contains_closure_sections(tmp_path):
    _require_real_tools()
    manifest, run_dir = _run_quality(
        tmp_path,
        incidentpilot_dir=REAL_INCIDENTPILOT,
        vibecheck_dir=REAL_VIBECHECK,
        scan_runner=_fake_runner(payload=_payload([_synthetic_finding()])),
        incidentpilot_runner=_incident_runner(_incident_payload()),
    )
    html_text = (run_dir / "report.html").read_text(encoding="utf-8")
    for section in ("故障复现断言", "诊断证据", "候选补丁", "Docker 沙箱复测", "人工审核状态"):
        assert section in html_text
    assert "PENDING_HUMAN_REVIEW" in html_text
    assert "/var/folders" not in html_text
    assert "/Users/" not in html_text
