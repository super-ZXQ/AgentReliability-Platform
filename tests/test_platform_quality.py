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

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_VIBECHECK = Path.home() / "Developer" / "GitHub" / "vibecheck-ai"
REAL_INCIDENTPILOT = Path.home() / "Developer" / "GitHub" / "incidentpilot"


def _require_real_tools() -> None:
    if not (REAL_VIBECHECK / "backend" / ".venv" / "bin" / "python").exists():
        pytest.skip("本机缺少固定版本的 VibeCheck-AI checkout")
    if not REAL_INCIDENTPILOT.exists():
        pytest.skip("本机缺少固定版本的 IncidentPilot checkout")


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

    separate = manifest["separate_regressions"]
    assert separate["commit"] == json.loads(
        (REPO_ROOT / "target.lock.json").read_text(encoding="utf-8")
    )["commit"]


def test_rerun_creates_new_run_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        quality,
        "resolve_incidentpilot_dir",
        lambda explicit=None, environ=None: REAL_INCIDENTPILOT,
    )
    fake = _fake_runner(payload=_payload([_synthetic_finding()]))
    first, first_dir = _run_quality(
        tmp_path, vibecheck_dir=REAL_VIBECHECK, scan_runner=fake
    )
    second, second_dir = _run_quality(
        tmp_path, vibecheck_dir=REAL_VIBECHECK, scan_runner=fake
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
