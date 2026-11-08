"""M3 验收：HTML 报告转义、对比分类、演示变体与 CI 用例矩阵校验。"""

from __future__ import annotations

import json
from pathlib import Path

from agentreliability import ORACLE_VERSION, REPORT_FORMAT_VERSION
from agentreliability.cli import main
from agentreliability.compare import NOT_COMPARABLE, compare_reports
from agentreliability.html_report import render_compare_html, render_run_html
from agentreliability.report import build_report
from agentreliability.runner import run_cases_sync

from scripts.agent_regression import validate_candidate_report

CASES_SHA = "a" * 64
FIXTURE_SHA = "b" * 64


def _case(case_id="REL-01", verdict="PASS", *, executions=None, assertions=None):
    return {
        "case_id": case_id,
        "trial": 1,
        "variant": None,
        "duration_ms": 10,
        "verdict": verdict,
        "expected": {"rows": [{"sales": 42}]},
        "actual": {"rows": [{"sales": 42}], "executions": executions or []},
        "assertions": assertions or [],
        "trace_refs": [1, 2],
    }


def _report(cases, *, mode="offline", cases_sha=CASES_SHA, verdict="PASS"):
    counts = {
        "total": len(cases),
        "pass": sum(case["verdict"] == "PASS" for case in cases),
        "fail": sum(case["verdict"] == "FAIL" for case in cases),
        "error": sum(case["verdict"] == "ERROR" for case in cases),
        "not_run": sum(case["verdict"] == "NOT_RUN" for case in cases),
        "verdict": verdict,
    }
    return {
        "format_version": REPORT_FORMAT_VERSION,
        "oracle_version": ORACLE_VERSION,
        "run_id": "test-run",
        "mode": mode,
        "variant": None,
        "selection": None,
        "command": "uv run --locked agentreliability run --mode offline",
        "target": {
            "repository": "https://github.com/super-ZXQ/ai-commerce-intelligence-platform.git",
            "commit": "5e63127268dd45c4f526635dcc97d51fc5b41de8",
            "path": ".targets/commerce",
        },
        "inputs": {
            "cases_path": "benchmarks/commerce/cases.jsonl",
            "cases_sha256": cases_sha,
            "fixture_path": "benchmarks/commerce/fixture.sql",
            "fixture_sha256": FIXTURE_SHA,
        },
        "summary": counts,
        "cases": cases,
    }


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_run_html_escapes_inputs_and_outputs():
    malicious = "<script>alert('x')</script>"
    report = _report(
        [
            {
                **_case(),
                "expected": {"questions": [malicious]},
                "actual": {"answer": malicious},
            }
        ]
    )
    trace = [
        {
            "seq": 1,
            "round": 1,
            "source": "model",
            "step": "generate_sql",
            "status": "success",
            "summary": malicious,
            "sql": malicious,
        }
    ]

    html = render_run_html(report, trace)
    assert malicious not in html
    assert "&lt;script&gt;alert" in html
    assert "单例复现" in html
    assert "--case REL-01" in html


def test_run_report_html_is_written_to_out(tmp_path, cases, target, fixture_sql):
    case = next(item for item in cases if item["case_id"] == "REL-01")
    results, trace = run_cases_sync(
        [case], target=target, fixture_sql=fixture_sql, run_id="test-m3-run"
    )
    report = build_report(
        run_id="test-m3-run",
        mode="offline",
        target={"repository": target.repository, "commit": target.commit},
        cases_path=Path("benchmarks/commerce/cases.jsonl"),
        fixture_path=Path("benchmarks/commerce/fixture.sql"),
        results=results,
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        finished_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        command="uv run --locked agentreliability run --mode offline",
        selection=["REL-01"],
    )
    html_path = tmp_path / "report.html"
    html_path.write_text(render_run_html(report, trace), encoding="utf-8")

    assert html_path.exists()
    assert "REL-01" in html_path.read_text(encoding="utf-8")


def test_demo_fault_disables_retry_and_recovers(cases, target, fixture_sql):
    case = next(item for item in cases if item["case_id"] == "REL-01")

    broken, _ = run_cases_sync(
        [case],
        target=target,
        fixture_sql=fixture_sql,
        run_id="test-m3-demo-broken",
        variant="disable-sql-retry",
    )
    result = broken[0]
    assert result["variant"] == "disable-sql-retry"
    assert result["verdict"] == "FAIL"
    assert [item["status"] for item in result["actual"]["executions"]] == ["error"]
    failed = {
        assertion["name"]
        for assertion in result["assertions"]
        if assertion["verdict"] == "FAIL"
    }
    assert {"executions", "retry_context"} <= failed
    assert "缺少第二次模型调用" in json.dumps(
        result["assertions"], ensure_ascii=False
    )

    restored, _ = run_cases_sync(
        [case], target=target, fixture_sql=fixture_sql, run_id="test-m3-demo-restored"
    )
    assert restored[0]["verdict"] == "PASS"
    assert [item["status"] for item in restored[0]["actual"]["executions"]] == [
        "error",
        "success",
    ]


def test_compare_classifies_new_failure_and_fixed():
    failing_assertion = {
        "name": "executions",
        "verdict": "FAIL",
        "expected": {"count": 2},
        "actual": {"count": 1},
    }
    baseline = _report([_case()])
    candidate = _report(
        [_case(verdict="FAIL", assertions=[failing_assertion])], verdict="FAIL"
    )

    comparison = compare_reports(baseline, candidate, command="compare")
    assert comparison["comparable"] is True
    assert comparison["summary"]["verdict"] == "FAIL"
    assert comparison["summary"]["new_failures"] == 1
    assert comparison["new_failures"] == ["REL-01"]

    restored = compare_reports(candidate, baseline, command="compare")
    assert restored["summary"]["verdict"] == "PASS"
    assert restored["summary"]["fixed"] == 1
    assert restored["fixed"] == ["REL-01"]


def test_compare_rejects_mismatched_cases_mode_or_data():
    baseline = _report([_case()])
    mismatched_cases = _report([_case()], cases_sha="c" * 64)
    mismatched_mode = _report([_case()], mode="live")

    comparison = compare_reports(baseline, mismatched_cases)
    assert comparison["comparable"] is False
    assert comparison["summary"]["verdict"] == NOT_COMPARABLE
    assert any("用例校验和" in reason for reason in comparison["reasons"])

    comparison = compare_reports(baseline, mismatched_mode)
    assert comparison["summary"]["verdict"] == NOT_COMPARABLE
    assert any("运行模式" in reason for reason in comparison["reasons"])


def test_compare_marks_missing_case_and_environment_error_as_failure():
    baseline = _report([_case("REL-01"), _case("REL-02")])
    candidate = _report([_case("REL-01"), _case("REL-02", verdict="ERROR")])

    comparison = compare_reports(baseline, candidate)
    assert comparison["summary"]["verdict"] == "FAIL"
    assert comparison["summary"]["environment_errors"] == 1
    assert comparison["environment_errors"] == ["REL-02"]

    missing = compare_reports(baseline, _report([_case("REL-01")]))
    assert missing["summary"]["verdict"] == "FAIL"
    assert missing["summary"]["missing"] == 1
    assert missing["missing"] == ["REL-02"]


def test_compare_html_escapes_reasons_and_assertions():
    baseline = _report([_case()])
    candidate = _report(
        [
            _case(
                verdict="FAIL",
                assertions=[
                    {
                        "name": "<img src=x>",
                        "verdict": "FAIL",
                        "expected": "<script>bad()</script>",
                        "actual": "<script>bad()</script>",
                    }
                ],
            )
        ],
        verdict="FAIL",
    )
    html = render_compare_html(
        compare_reports(baseline, candidate, command="`<script>bad()</script>`")
    )
    assert "<script>bad()</script>" not in html
    assert "<img src=x>" not in html
    assert "&lt;script&gt;bad()" in html


def test_validate_candidate_report_requires_20_unique_ids(cases):
    expected_ids = [case["case_id"] for case in cases]
    assert len(expected_ids) == 20
    full = _report([_case(case_id) for case_id in expected_ids])
    assert validate_candidate_report(full, expected_ids) == []

    missing = _report([_case(case_id) for case_id in expected_ids[:-1]])
    errors = validate_candidate_report(missing, expected_ids)
    assert any("缺失用例" in error for error in errors)
    assert any("不是 20" in error for error in errors)

    duplicated = _report(
        [_case(case_id) for case_id in expected_ids] + [_case(expected_ids[0])]
    )
    errors = validate_candidate_report(duplicated, expected_ids)
    assert any("重复用例 ID" in error for error in errors)


def test_cli_compare_exit_codes(tmp_path):
    baseline_path = _write(tmp_path / "baseline.json", _report([_case()]))
    failed_path = _write(
        tmp_path / "failed.json",
        _report([_case(verdict="FAIL")], verdict="FAIL"),
    )
    mismatched_path = _write(
        tmp_path / "mismatched.json", _report([_case()], cases_sha="d" * 64)
    )

    new_failure = main(
        [
            "compare",
            str(baseline_path),
            str(failed_path),
            "--out",
            str(tmp_path / "out-failure"),
        ]
    )
    assert new_failure == 1
    assert (tmp_path / "out-failure" / "comparison.json").exists()

    fixed = main(
        [
            "compare",
            str(failed_path),
            str(baseline_path),
            "--out",
            str(tmp_path / "out-fixed"),
        ]
    )
    assert fixed == 0

    not_comparable = main(
        [
            "compare",
            str(baseline_path),
            str(mismatched_path),
            "--out",
            str(tmp_path / "out-mismatched"),
        ]
    )
    assert not_comparable == 2
    assert (tmp_path / "out-mismatched" / "comparison.html").exists()
