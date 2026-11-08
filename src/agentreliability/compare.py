"""基线报告与候选报告的可比性校验和逐用例差异。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .oracle import ERROR, FAIL, NOT_RUN, PASS

NOT_COMPARABLE = "NOT_COMPARABLE"


def load_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"报告不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"报告不是合法 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"报告必须是对象：{path}")
    return payload


def _comparability_reasons(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> list[str]:
    reasons: list[str] = []
    baseline_target = baseline.get("target") or {}
    candidate_target = candidate.get("target") or {}
    if not baseline_target.get("commit"):
        reasons.append("基线报告缺少目标提交")
    if not candidate_target.get("commit"):
        reasons.append("候选报告缺少目标提交")
    if baseline_target.get("repository") != candidate_target.get("repository"):
        reasons.append("目标仓库不一致")
    for field, label in (
        ("format_version", "报告格式版本"),
        ("oracle_version", "判定器版本"),
        ("mode", "运行模式"),
    ):
        if baseline.get(field) != candidate.get(field):
            reasons.append(f"{label}不一致：{baseline.get(field)!r} vs {candidate.get(field)!r}")
    baseline_inputs = baseline.get("inputs") or {}
    candidate_inputs = candidate.get("inputs") or {}
    for field, label in (("cases_sha256", "用例校验和"), ("fixture_sha256", "数据校验和")):
        left = baseline_inputs.get(field)
        right = candidate_inputs.get(field)
        if not left or not right:
            reasons.append(f"缺少{label}")
        elif left != right:
            reasons.append(f"{label}不一致")
    if not isinstance(baseline.get("cases"), list) or not isinstance(
        candidate.get("cases"), list
    ):
        reasons.append("报告缺少用例结果")
    return reasons


def _executions(result: dict[str, Any]) -> list[dict[str, Any]]:
    actual = result.get("actual") or {}
    return list(actual.get("executions") or [])


def compare_reports(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    command: str = "",
) -> dict[str, Any]:
    reasons = _comparability_reasons(baseline, candidate)
    baseline_cases = {
        str(case.get("case_id")): case for case in (baseline.get("cases") or [])
    }
    candidate_cases = {
        str(case.get("case_id")): case for case in (candidate.get("cases") or [])
    }
    ordered_ids = [str(case.get("case_id")) for case in (baseline.get("cases") or [])]
    ordered_ids.extend(
        case_id for case_id in candidate_cases if case_id not in baseline_cases
    )

    new_failures: list[str] = []
    fixed: list[str] = []
    environment_errors: list[str] = []
    missing: list[str] = []
    unchanged_failures: list[str] = []
    unchanged_pass: list[str] = []
    diffs: list[dict[str, Any]] = []

    for case_id in ordered_ids:
        baseline_case = baseline_cases.get(case_id)
        candidate_case = candidate_cases.get(case_id)
        baseline_verdict = baseline_case.get("verdict") if baseline_case else None
        candidate_verdict = candidate_case.get("verdict") if candidate_case else None

        if candidate_case is None:
            change = "missing_in_candidate"
            missing.append(case_id)
        elif baseline_case is None:
            if candidate_verdict in (ERROR, NOT_RUN):
                change = "environment_error"
                environment_errors.append(case_id)
            elif candidate_verdict == FAIL:
                change = "new_failure"
                new_failures.append(case_id)
            else:
                change = "missing_in_baseline"
        elif candidate_verdict in (ERROR, NOT_RUN):
            change = "environment_error"
            environment_errors.append(case_id)
        elif candidate_verdict == FAIL and baseline_verdict == PASS:
            change = "new_failure"
            new_failures.append(case_id)
        elif candidate_verdict == PASS and baseline_verdict in (FAIL, ERROR, NOT_RUN):
            change = "fixed"
            fixed.append(case_id)
        elif candidate_verdict == FAIL:
            change = "unchanged_failure"
            unchanged_failures.append(case_id)
        elif candidate_verdict == PASS:
            change = "unchanged_pass"
            unchanged_pass.append(case_id)
        else:
            change = "unknown"

        diffs.append(
            {
                "case_id": case_id,
                "baseline_verdict": baseline_verdict,
                "candidate_verdict": candidate_verdict,
                "change": change,
                "baseline_actual_executions": (
                    _executions(baseline_case) if baseline_case else []
                ),
                "candidate_actual_executions": (
                    _executions(candidate_case) if candidate_case else []
                ),
                "baseline_assertions": (
                    baseline_case.get("assertions") or [] if baseline_case else []
                ),
                "candidate_assertions": (
                    candidate_case.get("assertions") or [] if candidate_case else []
                ),
                "baseline_trace_refs": (
                    baseline_case.get("trace_refs") or [] if baseline_case else []
                ),
                "candidate_trace_refs": (
                    candidate_case.get("trace_refs") or [] if candidate_case else []
                ),
            }
        )

    if reasons:
        verdict = NOT_COMPARABLE
    elif (
        candidate.get("summary", {}).get("verdict") != PASS
        or new_failures
        or environment_errors
        or missing
    ):
        verdict = FAIL
    else:
        verdict = PASS

    summary = {
        "total": len(ordered_ids),
        "new_failures": len(new_failures),
        "fixed": len(fixed),
        "environment_errors": len(environment_errors),
        "missing": len(missing),
        "unchanged_failures": len(unchanged_failures),
        "unchanged_pass": len(unchanged_pass),
        "verdict": verdict,
    }
    return {
        "format_version": "1.0",
        "comparison_version": "1.0",
        "command": command,
        "baseline": {
            "run_id": baseline.get("run_id"),
            "target": baseline.get("target"),
            "variant": baseline.get("variant"),
            "summary": baseline.get("summary"),
        },
        "candidate": {
            "run_id": candidate.get("run_id"),
            "target": candidate.get("target"),
            "variant": candidate.get("variant"),
            "summary": candidate.get("summary"),
        },
        "comparable": not reasons,
        "reasons": reasons,
        "summary": summary,
        "new_failures": new_failures,
        "fixed": fixed,
        "environment_errors": environment_errors,
        "missing": missing,
        "unchanged_failures": unchanged_failures,
        "cases": diffs,
    }


def write_comparison(
    out_dir: Path, comparison: dict[str, Any]
) -> tuple[Path, Path]:
    from .html_report import render_compare_html, write_html

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "comparison.json"
    html_path = out_dir / "comparison.html"
    json_path.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_html(html_path, render_compare_html(comparison))
    return json_path, html_path
