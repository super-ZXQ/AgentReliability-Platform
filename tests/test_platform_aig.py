"""平台化阶段项目 4c：A.I.G MCP 扫描适配器测试。"""

from __future__ import annotations

import io
import json
import urllib.error
import zipfile
from pathlib import Path

from agentreliability.aig import (
    STATE_INSTANCE_MISSING,
    STATE_PARSE_FAILURE,
    STATE_SAMPLE_INVALID,
    STATE_TASK_FAILED,
    STATE_TIMEOUT,
    STATE_UNREACHABLE,
    build_source_zip,
    normalize_findings,
    run_aig_scan,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE = REPO_ROOT / "benchmarks" / "aig_samples" / "synthetic_mcp_server"


def _envelope(data: dict | None = None, status: int = 0, message: str = "ok") -> bytes:
    return json.dumps({"status": status, "message": message, "data": data or {}}).encode()


def _transport(sequence: dict[str, list[bytes]]):
    counter = {"status": 0}

    def transport(method, url, body, headers, timeout):
        if "/upload" in url:
            return 200, sequence["upload"][0], {}
        if url.endswith("/tasks"):
            return 200, sequence["tasks"][0], {}
        if "/status/" in url:
            index = min(counter["status"], len(sequence["status"]) - 1)
            counter["status"] += 1
            return 200, sequence["status"][index], {}
        if "/result/" in url:
            return 200, sequence["result"][0], {}
        raise AssertionError(url)

    return transport


def _success_transport():
    return _transport(
        {
            "upload": [_envelope({"fileUrl": "http://aig/uploads/source.zip", "filename": "source.zip", "size": 120})],
            "tasks": [_envelope({"session_id": "S-1"})],
            "status": [
                _envelope({"status": "running", "log": "scanning"}),
                _envelope({"status": "completed", "log": "done"}),
            ],
            "result": [
                _envelope(
                    {
                        "report": "A.I.G 扫描完成",
                        "vulnerabilities": [
                            {
                                "title": "命令注入",
                                "severity": "high",
                                "file": "mcp_server.py",
                                "description": "subprocess with shell=True",
                                "solution": "避免 shell=True",
                            },
                            {
                                "title": "路径穿越",
                                "severity": "medium",
                                "file": "mcp_server.py",
                                "description": "open arbitrary path",
                            },
                        ],
                    }
                )
            ],
        }
    )


def _run(tmp_path: Path, **kwargs):
    return run_aig_scan(
        out=tmp_path,
        base_url="http://aig.test",
        timeout=1.0,
        poll_interval=0.0,
        max_poll_seconds=1.0,
        max_poll_attempts=3,
        sample_dir=SAMPLE,
        transport=_success_transport(),
        **kwargs,
    )


def test_sample_zip_is_whitelist_only(tmp_path):
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "server.py").write_text("print('x')", encoding="utf-8")
    (sample_dir / ".env").write_text("SECRET=1", encoding="utf-8")
    (sample_dir / "key.pem").write_text("key", encoding="utf-8")
    report = build_source_zip(sample_dir, tmp_path / "out.zip")
    assert report["files"] == ["server.py"]
    assert ".env" in report["skipped_files"]
    assert "key.pem" in report["skipped_files"]
    assert (tmp_path / "out.zip").exists()


def _sample_with_external_symlink(tmp_path: Path, marker: str = "OUTSIDE_SECRET") -> tuple[Path, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_text(f"{marker} = 1\n", encoding="utf-8")
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "server.py").write_text("print('safe')\n", encoding="utf-8")
    (sample_dir / "linked.py").symlink_to(secret)
    return sample_dir, secret


def _zip_from_upload_body(body: bytes) -> zipfile.ZipFile:
    start = body.index(b"\r\n\r\n") + 4
    end = body.rindex(b"\r\n--")
    return zipfile.ZipFile(io.BytesIO(body[start:end]))


def test_symlink_outside_sample_is_rejected_from_zip(tmp_path):
    sample_dir, secret = _sample_with_external_symlink(tmp_path)
    report = build_source_zip(sample_dir, tmp_path / "out.zip")

    assert report["files"] == ["server.py"]
    assert {"path": "linked.py", "reason": "symlink"} in report["rejected_files"]
    assert "linked.py" in report["skipped_files"]
    with zipfile.ZipFile(tmp_path / "out.zip") as archive:
        assert archive.namelist() == ["server.py"]
        assert b"OUTSIDE_SECRET" not in b"".join(
            archive.read(name) for name in archive.namelist()
        )
    assert secret.read_text(encoding="utf-8").startswith("OUTSIDE_SECRET")


def test_upload_never_receives_outside_content(tmp_path):
    sample_dir, _ = _sample_with_external_symlink(tmp_path)
    captured: dict[str, bytes] = {}
    base = _success_transport()

    def transport(method, url, body, headers, timeout):
        if "/upload" in url:
            captured["body"] = body
        return base(method, url, body, headers, timeout)

    manifest, _ = run_aig_scan(
        out=tmp_path / "artifacts",
        base_url="http://aig.test",
        timeout=1.0,
        poll_interval=0.0,
        max_poll_seconds=1.0,
        max_poll_attempts=3,
        sample_dir=sample_dir,
        transport=transport,
        environ={},
    )

    assert manifest["status"] == "PASS"
    assert manifest["sample"]["files"] == ["server.py"]
    assert {"path": "linked.py", "reason": "symlink"} in manifest["sample"]["rejected_files"]
    with _zip_from_upload_body(captured["body"]) as archive:
        assert archive.namelist() == ["server.py"]
        assert b"OUTSIDE_SECRET" not in b"".join(
            archive.read(name) for name in archive.namelist()
        )


def test_relative_symlink_inside_sample_is_still_rejected(tmp_path):
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "server.py").write_text("print('safe')\n", encoding="utf-8")
    (sample_dir / "linked.py").symlink_to(sample_dir / "server.py")
    report = build_source_zip(sample_dir, tmp_path / "out.zip")

    assert report["files"] == ["server.py"]
    assert {"path": "linked.py", "reason": "symlink"} in report["rejected_files"]
    with zipfile.ZipFile(tmp_path / "out.zip") as archive:
        assert archive.namelist() == ["server.py"]


def test_missing_sample_dir_is_error(tmp_path):
    manifest, _ = run_aig_scan(
        out=tmp_path, sample_dir=tmp_path / "missing", environ={}, base_url="http://aig.test"
    )
    assert manifest["status"] == "ERROR"
    assert manifest["state"] == STATE_SAMPLE_INVALID


def test_successful_scan_normalizes_and_writes_artifacts(tmp_path):
    manifest, run_dir = _run(tmp_path)
    assert manifest["status"] == "PASS"
    assert manifest["execution_mode"] == "test-double"
    assert manifest["task"]["session_id"] == "S-1"
    assert manifest["polling"]["attempts"] == 2
    assert manifest["findings"]["total"] == 2
    assert manifest["findings"]["severity_counts"] == {"high": 1, "medium": 1}
    for name in ("upload.json", "task.json", "status.json", "result.raw.json", "findings.json", "report.html"):
        assert (run_dir / name).exists(), name
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "/Users/" not in html
    assert "命令注入" in html


def test_missing_instance_is_not_run(tmp_path):
    manifest, _ = run_aig_scan(
        out=tmp_path, sample_dir=SAMPLE, environ={}, transport=_success_transport()
    )
    assert manifest["status"] == "NOT_RUN"
    assert manifest["state"] == STATE_INSTANCE_MISSING
    assert manifest["findings"] == {}
    assert (manifest["sample"]["archive"])


def test_unreachable_is_not_run(tmp_path):
    def transport(method, url, body, headers, timeout):
        raise urllib.error.URLError("connection refused")

    manifest, _ = run_aig_scan(
        out=tmp_path,
        base_url="http://aig.test",
        sample_dir=SAMPLE,
        transport=transport,
        environ={},
    )
    assert manifest["status"] == "NOT_RUN"
    assert manifest["state"] == STATE_UNREACHABLE


def test_poll_timeout_never_generates_pass(tmp_path):
    transport = _transport(
        {
            "upload": [_envelope({"fileUrl": "http://aig/uploads/x.zip"})],
            "tasks": [_envelope({"session_id": "S-2"})],
            "status": [_envelope({"status": "running"})],
            "result": [],
        }
    )
    manifest, _ = run_aig_scan(
        out=tmp_path,
        base_url="http://aig.test",
        sample_dir=SAMPLE,
        transport=transport,
        poll_interval=0.0,
        max_poll_seconds=10.0,
        max_poll_attempts=2,
        environ={},
    )
    assert manifest["status"] == "ERROR"
    assert manifest["state"] == STATE_TIMEOUT


def test_failed_task_state(tmp_path):
    transport = _transport(
        {
            "upload": [_envelope({"fileUrl": "http://aig/uploads/x.zip"})],
            "tasks": [_envelope({"session_id": "S-3"})],
            "status": [_envelope({"status": "failed", "log": "model failed"})],
            "result": [],
        }
    )
    manifest, _ = run_aig_scan(
        out=tmp_path,
        base_url="http://aig.test",
        sample_dir=SAMPLE,
        transport=transport,
        poll_interval=0.0,
        environ={},
    )
    assert manifest["status"] == "ERROR"
    assert manifest["state"] == STATE_TASK_FAILED


def test_service_failure_envelope(tmp_path):
    transport = _transport(
        {
            "upload": [_envelope({"fileUrl": "http://aig/uploads/x.zip"})],
            "tasks": [_envelope(status=1, message="model not configured")],
            "status": [],
            "result": [],
        }
    )
    manifest, _ = run_aig_scan(
        out=tmp_path,
        base_url="http://aig.test",
        sample_dir=SAMPLE,
        transport=transport,
        environ={},
    )
    assert manifest["status"] == "ERROR"
    assert manifest["state"] == STATE_TASK_FAILED
    assert "model not configured" in manifest["error"]


def test_parse_failure_never_generates_pass(tmp_path):
    transport = _transport(
        {
            "upload": [_envelope({"fileUrl": "http://aig/uploads/x.zip"})],
            "tasks": [_envelope({"session_id": "S-4"})],
            "status": [_envelope({"status": "completed"})],
            "result": [b"not json"],
        }
    )
    manifest, _ = run_aig_scan(
        out=tmp_path,
        base_url="http://aig.test",
        sample_dir=SAMPLE,
        transport=transport,
        environ={},
    )
    assert manifest["status"] == "ERROR"
    assert manifest["state"] == STATE_PARSE_FAILURE


def test_normalization_handles_nested_and_unrecognized_shapes():
    nested = normalize_findings(
        {"data": {"issues": [{"name": "hardcoded secret", "risk": "critical"}]}}
    )
    assert nested["total"] == 1
    assert nested["findings"][0]["severity"] == "critical"
    empty = normalize_findings({"report": "no structured findings"})
    assert empty["total"] == 0
    assert empty["normalization"] == "unrecognized"
