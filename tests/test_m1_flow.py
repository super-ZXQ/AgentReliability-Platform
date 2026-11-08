"""真实 AgentRuntime.astream 的 M1 验收测试。"""

from __future__ import annotations

import json
from pathlib import Path

from agentreliability.cli import main
from agentreliability.runner import run_cases_sync


def _run_one(case, target, fixture_sql):
    results, trace = run_cases_sync(
        [case], target=target, fixture_sql=fixture_sql, run_id="test-run"
    )
    return results[0], trace


def test_data_01_returns_42_with_single_execution(cases, target, fixture_sql):
    case = next(item for item in cases if item["case_id"] == "DATA-01")
    result, trace = _run_one(case, target, fixture_sql)

    assert result["verdict"] == "PASS"
    assert result["actual"]["rows"] == [{"sales": 42}]
    assert [item["status"] for item in result["actual"]["executions"]] == ["success"]
    assert len(result["actual"]["executions"]) == 1
    assert result["trace_refs"]
    assert any(
        event["source"] == "sql_port" and event["status"] == "success" for event in trace
    )


def test_rel_01_retries_once_and_returns_42(cases, target, fixture_sql):
    case = next(item for item in cases if item["case_id"] == "REL-01")
    result, trace = _run_one(case, target, fixture_sql)

    assert result["verdict"] == "PASS"
    assert result["actual"]["rows"] == [{"sales": 42}]
    assert [item["status"] for item in result["actual"]["executions"]] == [
        "error",
        "success",
    ]
    second_call = result["actual"]["model_sql_calls"][1]
    assert second_call["previous_error"]
    assert any(
        event["source"] == "agent" and event["step"] == "generate_sql" for event in trace
    )


def test_negative_missing_upper_bound_executes_but_fails(cases, target, fixture_sql):
    base = next(item for item in cases if item["case_id"] == "DATA-01")
    case = {
        **base,
        "case_id": "NEG-MISSING-UPPER-BOUND",
        "sql_script": [
            "SELECT SUM(payment_amount) AS sales FROM orders "
            "WHERE order_date >= '2025-01-01'"
        ],
    }
    result, _ = _run_one(case, target, fixture_sql)

    assert result["verdict"] == "FAIL"
    assert [item["status"] for item in result["actual"]["executions"]] == ["success"]
    assert result["actual"]["rows"] == [{"sales": 116}]
    rows_assertion = result["assertions"][0]
    assert rows_assertion["expected"]["rows"] == [{"sales": 42}]
    assert rows_assertion["actual"]["rows"] == [{"sales": 116}]


def test_cli_writes_report_and_trace(repo_root: Path, target, tmp_path: Path):
    out_dir = tmp_path / "m1"
    exit_code = main(
        [
            "run",
            "--mode",
            "offline",
            "--target-dir",
            str(target.path),
            "--out",
            str(out_dir),
            "--target-lock",
            str(repo_root / "target.lock.json"),
            "--cases",
            str(repo_root / "benchmarks" / "commerce" / "cases.jsonl"),
            "--fixture",
            str(repo_root / "benchmarks" / "commerce" / "fixture.sql"),
        ]
    )

    assert exit_code == 0
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert report["format_version"] == "1.0"
    assert report["oracle_version"] == "1.0.0-m2"
    assert report["mode"] == "offline"
    assert report["target"]["commit"] == target.commit
    assert report["summary"]["verdict"] == "PASS"
    assert report["summary"]["total"] == 20
    assert report["summary"]["pass"] == 20
    assert report["summary"]["fail"] == 0
    assert report["summary"]["error"] == 0
    assert report["summary"]["not_run"] == 0
    assert report["inputs"]["cases_sha256"]
    assert report["inputs"]["fixture_sha256"]
    assert [case["case_id"] for case in report["cases"]] == [
        "DATA-01",
        "DATA-02",
        "DATA-03",
        "DATA-04",
        "DATA-05",
        "DATA-06",
        "DATA-07",
        "DATA-08",
        "CONV-01",
        "CONV-02",
        "CONV-03",
        "CONV-04",
        "REL-01",
        "REL-02",
        "REL-03",
        "REL-04",
        "SEC-01",
        "SEC-02",
        "SEC-03",
        "SEC-04",
    ]
    for case in report["cases"]:
        assert case["verdict"] == "PASS"
        assert case["trace_refs"]

    trace_events = [
        json.loads(line)
        for line in (out_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert trace_events
    required_fields = {
        "run_id",
        "case_id",
        "trial",
        "round",
        "seq",
        "source",
        "step",
        "status",
        "duration_ms",
    }
    assert required_fields <= set(trace_events[0])
