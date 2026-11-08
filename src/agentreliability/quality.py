"""平台化质量闭环：场景清单、静态扫描子任务与可审阅 HTML。"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .html_report import _CSS, _badge, _e, _json
from .incidentpilot import run_incidentpilot_investigation
from .oracle import ERROR, FAIL, NOT_RUN, PASS
from .report import sha256_file
from .vibecheck import STATE_SOURCE_MISSING, ScanReport, run_vibecheck_scan

MANIFEST_FORMAT_VERSION = "1.0"

SCENARIOS: dict[str, dict[str, Any]] = {
    "orders-slow-query": {
        "title": "orders-api 注入慢查询导致 /orders P95 延迟上升",
        "source_repository": "https://github.com/super-ZXQ/incidentpilot.git",
        "source_commit": "566639ea876eed171c705ad0c1a0d79276b8584b",
        "source_dir": "reference/orders_api",
        "fault_case": "benchmarks/fault_cases/fault_002_slow_database_query.yaml",
        "fault_id": "fault_002_slow_database_query",
        "fault_type": "slow_database_query",
        "delay_seconds": 0.5,
        "simulated": True,
    }
}


def resolve_incidentpilot_dir(
    explicit: Path | str | None = None,
    environ: dict[str, str] | None = None,
) -> Path | None:
    import os

    environ = os.environ if environ is None else environ
    if explicit:
        candidate = Path(explicit)
        return candidate.resolve() if candidate.is_dir() else None
    candidates: list[Path] = []
    if environ.get("INCIDENTPILOT_DIR"):
        candidate = Path(environ["INCIDENTPILOT_DIR"])
        return candidate.resolve() if candidate.is_dir() else None
    for base in (Path.cwd() / ".targets", Path.home() / "Developer" / "GitHub"):
        candidates.append(base / "incidentpilot")
        candidates.append(base / "IncidentPilot")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return None


def _git(path: Path, *args: str) -> tuple[str | None, str]:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None, " ".join(completed.stderr.split())[:200]
    return completed.stdout.strip(), ""


def _checkout_state(path: Path | None, repository: str, commit: str) -> dict[str, Any]:
    state: dict[str, Any] = {
        "repository": repository,
        "commit": commit,
        "status": ERROR,
        "commit_verified": False,
        "error": None,
    }
    if path is None:
        state["error"] = "未找到场景源码 checkout（可用 INCIDENTPILOT_DIR 指定）"
        return state
    actual, error = _git(path, "rev-parse", "HEAD")
    if actual is None:
        state["error"] = f"无法读取场景源码提交：{error}"
        return state
    if actual != commit:
        state["error"] = f"场景源码提交不匹配：期望 {commit}，实际 {actual}"
        return state
    dirty, error = _git(path, "status", "--porcelain", "--untracked-files=no")
    if dirty is None:
        state["error"] = f"无法检查场景源码工作树：{error}"
        return state
    if dirty:
        state["error"] = f"场景源码存在未提交改动：{dirty.splitlines()[0]}"
        return state
    state["status"] = PASS
    state["commit_verified"] = True
    return state


def _run_dir(base: Path, now: datetime, run_id: str) -> Path:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = base / "runs" / f"{stamp}-{run_id[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _artifact(run_dir: Path, path: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(run_dir)),
        "sha256": sha256_file(path) if path.exists() else None,
    }


def _test_view(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    return {
        key: item.get(key)
        for key in ("profile", "scope", "status", "exit_code", "duration_ms", "mode")
    }


def _retest_view(retest: dict[str, Any] | None) -> dict[str, Any] | None:
    if not retest:
        return None
    view = {
        key: retest.get(key)
        for key in ("status", "state", "error", "baseline_sha", "patch_summary", "workspace")
    }
    view["fault_active"] = _test_view(retest.get("fault_active"))
    view["healthy_regression"] = _test_view(retest.get("healthy_regression"))
    return view


def _summary(subtasks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        "total": len(subtasks),
        "pass": sum(item["status"] == PASS for item in subtasks),
        "fail": sum(item["status"] == FAIL for item in subtasks),
        "error": sum(item["status"] == ERROR for item in subtasks),
        "not_run": sum(item["status"] == NOT_RUN for item in subtasks),
        "expected_fail": sum(
            item["status"] == FAIL and item.get("expected_status") == FAIL
            for item in subtasks
        ),
    }
    unexpected_fail = any(
        item["status"] == FAIL and item.get("expected_status") != FAIL
        for item in subtasks
    )
    blocking = any(item["status"] in {ERROR, NOT_RUN} for item in subtasks)
    baseline = next(
        (item for item in subtasks if item["name"] == "failure_assertion"), None
    )
    baseline_ok = (
        baseline is not None
        and baseline["status"] == FAIL
        and baseline.get("expected_matched") is True
    )
    required = ("static_scan", "failure_assertion", "investigation", "sandbox_retest")
    present = {item["name"]: item for item in subtasks}
    executable_ok = all(
        present.get(name, {}).get("status") == PASS for name in ("static_scan", "investigation", "sandbox_retest")
    )
    if baseline_ok and executable_ok and not blocking and not unexpected_fail:
        counts["verdict"] = PASS
    elif blocking:
        counts["verdict"] = ERROR
    else:
        counts["verdict"] = FAIL
    counts["baseline_expected_failure_matched"] = baseline_ok
    counts["review_status"] = (
        "PENDING_HUMAN_REVIEW" if baseline_ok and executable_ok else "NOT_REACHED"
    )
    counts["required_subtasks"] = list(required)
    return counts


def _exit_code(verdict: str) -> int:
    if verdict == PASS:
        return 0
    if verdict == FAIL:
        return 1
    return 2


def _separate_regressions(workspace_root: Path, scenario_repository: str) -> dict[str, Any]:
    lock_path = workspace_root / "target.lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"note": "电商 Agent 套件与本闭环场景使用不同目标与提交"}
    return {
        "name": "commerce-agent-suite",
        "repository": lock.get("repository"),
        "commit": lock.get("commit"),
        "report": "artifacts/v1-release/offline/report.json",
        "note": (
            "独立 Agent 回归，不属于本次闭环场景；"
            f"场景源码为 {scenario_repository}"
        ),
    }


def run_quality(
    *,
    scenario_id: str,
    out: Path | str,
    command: str = "",
    incidentpilot_dir: Path | str | None = None,
    vibecheck_dir: Path | str | None = None,
    timeout: float = 180.0,
    investigation_timeout: float = 600.0,
    scan_runner=subprocess.run,
    incidentpilot_runner=subprocess.run,
    environ: dict[str, str] | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
    workspace_root: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    if scenario_id not in SCENARIOS:
        raise ValueError(f"未知场景：{scenario_id}")
    scenario = SCENARIOS[scenario_id]
    now = datetime.now(timezone.utc) if now is None else now
    run_id = uuid4().hex if run_id is None else run_id
    base = Path(out)
    base.mkdir(parents=True, exist_ok=True)
    run_dir = _run_dir(base, now, run_id)
    workspace_root = Path.cwd() if workspace_root is None else workspace_root

    source_root = resolve_incidentpilot_dir(incidentpilot_dir, environ)
    target = _checkout_state(
        source_root, scenario["source_repository"], scenario["source_commit"]
    )

    subtasks: list[dict[str, Any]] = []
    scan: ScanReport
    raw_path = run_dir / "scan.raw.json"
    if target["status"] != PASS or source_root is None:
        scan = ScanReport(
            status=ERROR,
            state=STATE_SOURCE_MISSING,
            error=target.get("error") or "场景源码不可用",
            tool={
                "name": "VibeCheck-AI",
                "locked_commit": None,
                "commit_verified": False,
            },
            execution_mode="real-service",
        )
    else:
        scan = run_vibecheck_scan(
            vibecheck_dir=vibecheck_dir,
            scan_root=source_root / scenario["source_dir"],
            out_raw=raw_path,
            source_label=scenario["source_dir"],
            timeout=timeout,
            runner=scan_runner,
            environ=environ,
        )

    scan_payload: dict[str, Any] = {
        "scanner": {
            "module": "app.scanner.sensitive",
            "function": "scan_directory",
        },
        "status": scan.status,
        "state": scan.state,
        "error": scan.error,
        "execution_mode": scan.execution_mode,
        "tool": scan.tool,
        "command": scan.command,
        "scope": scan.scope,
        "findings": scan.findings,
        "raw_artifact": (
            str(raw_path.relative_to(run_dir)) if raw_path.exists() else None
        ),
    }
    scan_path = run_dir / "scan.json"
    scan_path.write_text(
        json.dumps(scan_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    artifacts = [_artifact(run_dir, scan_path)]
    if raw_path.exists():
        artifacts.append(_artifact(run_dir, raw_path))
    subtasks.append(
        {
            "name": "static_scan",
            "title": "VibeCheck-AI 敏感信息与质量规则静态扫描",
            "status": scan.status,
            "state": scan.state,
            "execution_mode": scan.execution_mode,
            "tool": scan.tool,
            "external_run_id": scan.tool.get("external_run_id"),
            "external_reference": scan.tool.get("external_reference"),
            "source_files": [scenario["source_dir"]],
            "artifacts": artifacts,
            "findings_count": len(scan.findings),
            "scope": scan.scope,
            "error": scan.error,
        }
    )

    incident = run_incidentpilot_investigation(
        incidentpilot_dir=source_root if target["status"] == PASS else None,
        out_raw=run_dir / "incidentpilot.raw.json",
        timeout=investigation_timeout,
        runner=incidentpilot_runner,
        environ=environ,
    )
    incident_payload = incident.payload or {}
    baseline = incident_payload.get("baseline")
    baseline_doc: dict[str, Any] = {}
    investigation = incident_payload.get("investigation")
    retest = incident_payload.get("sandbox_retest")
    raw_incident_path = run_dir / "incidentpilot.raw.json"
    raw_incident_artifact = (
        [_artifact(run_dir, raw_incident_path)] if raw_incident_path.exists() else []
    )

    if baseline:
        baseline_path = run_dir / "failure-assertion.json"
        baseline_doc = {
            "scenario": scenario_id,
            "source": {
                "repository": scenario["source_repository"],
                "commit": scenario["source_commit"],
                "path": scenario["source_dir"],
            },
            "assertion": baseline,
            "supporting_evidence_ids": (
                (investigation or {}).get("verification", {}).get("evidence_ids") or []
            ),
        }
        baseline_path.write_text(
            json.dumps(baseline_doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        subtasks.append(
            {
                "name": "failure_assertion",
                "title": "原版本故障条件断言（预期失败）",
                "status": baseline.get("status"),
                "expected_status": FAIL,
                "expected_matched": bool(baseline.get("expected_matched")),
                "state": None,
                "execution_mode": "real-local-assertion",
                "tool": {
                    "name": "reference/orders_api TestClient",
                    "entrypoint": "app.create_app + /admin/fault",
                },
                "external_run_id": baseline.get("linked_run_id"),
                "external_reference": f"{scenario['fault_id']}:{baseline.get('name')}",
                "source_files": [scenario["source_dir"]],
                "artifacts": [_artifact(run_dir, baseline_path)] + raw_incident_artifact,
                "assertion": {
                    "threshold_ms": baseline.get("threshold_ms"),
                    "observed_ms": baseline.get("observed_ms"),
                    "samples_ms": baseline.get("samples_ms"),
                    "payload_equal_to_healthy": baseline.get(
                        "payload_equal_to_healthy"
                    ),
                },
                "linked_run_id": baseline.get("linked_run_id"),
                "error": None,
            }
        )
    else:
        subtasks.append(
            {
                "name": "failure_assertion",
                "title": "原版本故障条件断言（预期失败）",
                "status": NOT_RUN,
                "expected_status": FAIL,
                "expected_matched": False,
                "state": incident.state or "investigation_not_completed",
                "execution_mode": incident.execution_mode,
                "tool": incident.tool,
                "external_run_id": None,
                "source_files": [scenario["source_dir"]],
                "artifacts": raw_incident_artifact,
                "error": incident.error or "调查未产出失败断言",
            }
        )

    investigation_ok = bool(
        incident.status == PASS
        and investigation
        and investigation.get("run_id")
        and (investigation.get("root_cause") or {}).get("summary")
        and investigation.get("patch_artifact")
        and (investigation.get("review") or {}).get("status") == "PENDING_HUMAN_REVIEW"
    )
    if investigation:
        investigation_path = run_dir / "investigation.json"
        investigation_path.write_text(
            json.dumps(investigation, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        patch = investigation.get("patch_artifact") or {}
        inv_summary = {
            "run_id": investigation.get("run_id"),
            "incident_id": investigation.get("incident_id"),
            "trace_id": investigation.get("trace_id"),
            "status": investigation.get("status"),
            "workflow_state": investigation.get("workflow_state"),
            "root_cause": investigation.get("root_cause"),
            "hypotheses": investigation.get("hypotheses"),
            "evidence": [
                {
                    key: item.get(key)
                    for key in (
                        "evidence_id",
                        "source",
                        "source_type",
                        "tool_call_id",
                        "summary",
                        "content_hash",
                    )
                }
                for item in investigation.get("evidence") or []
            ],
            "tool_calls": [
                {
                    key: item.get(key)
                    for key in (
                        "tool_call_id",
                        "tool_name",
                        "status",
                        "latency_ms",
                    )
                }
                for item in investigation.get("tool_calls") or []
            ],
            "patch_artifact": {
                key: patch.get(key)
                for key in (
                    "patch_artifact_id",
                    "base_commit_sha",
                    "patch_hash",
                    "patch_summary",
                    "test_run_id",
                )
            },
            "test_runs": [
                _test_view(item) for item in investigation.get("test_runs") or []
            ],
            "verification": investigation.get("verification"),
            "review": investigation.get("review"),
        }
        investigation_state = None if investigation_ok else "workflow_incomplete"
        investigation_error = (
            None
            if investigation_ok
            else "调查未产出根因、补丁或人工审核状态"
        )
        subtasks.append(
            {
                "name": "investigation",
                "title": "IncidentPilot 离线调查（脚本化模型与工具）",
                "status": PASS if investigation_ok else ERROR,
                "state": investigation_state,
                "execution_mode": incident.execution_mode,
                "tool": incident.tool,
                "external_run_id": investigation.get("run_id"),
                "external_reference": incident.tool.get("external_reference"),
                "source_files": [scenario["source_dir"]],
                "artifacts": [_artifact(run_dir, investigation_path)]
                + raw_incident_artifact,
                "error": investigation_error,
            }
        )
    else:
        inv_summary = None
        subtasks.append(
            {
                "name": "investigation",
                "title": "IncidentPilot 离线调查（脚本化模型与工具）",
                "status": NOT_RUN,
                "state": incident.state or "investigation_not_completed",
                "execution_mode": incident.execution_mode,
                "tool": incident.tool,
                "external_run_id": None,
                "source_files": [scenario["source_dir"]],
                "artifacts": raw_incident_artifact,
                "error": incident.error or "调查未执行",
            }
        )

    retest_ok = bool(retest and retest.get("status") == PASS)
    if retest:
        retest_path = run_dir / "sandbox-retest.json"
        retest_path.write_text(
            json.dumps(retest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        modes = {
            (retest.get("fault_active") or {}).get("mode"),
            (retest.get("healthy_regression") or {}).get("mode"),
        }
        retest_mode = "real-docker" if modes == {"docker"} else "offline-scripted"
        subtasks.append(
            {
                "name": "sandbox_retest",
                "title": "Docker 沙箱补丁复测（故障断言 + 健康回归）",
                "status": retest.get("status"),
                "state": retest.get("state"),
                "execution_mode": retest_mode,
                "tool": {
                    "name": "IncidentPilot SandboxManager",
                    "image": "incidentpilot-sandbox:py312",
                    "external_run_id": None,
                },
                "external_run_id": None,
                "source_files": [scenario["source_dir"]],
                "artifacts": [_artifact(run_dir, retest_path)] + raw_incident_artifact,
                "baseline_sha": retest.get("baseline_sha"),
                "patch_summary": retest.get("patch_summary"),
                "fault_active": _test_view(retest.get("fault_active")),
                "healthy_regression": _test_view(retest.get("healthy_regression")),
                "error": retest.get("error"),
            }
        )
    else:
        subtasks.append(
            {
                "name": "sandbox_retest",
                "title": "Docker 沙箱补丁复测（故障断言 + 健康回归）",
                "status": NOT_RUN,
                "state": "missing_patch_artifact",
                "execution_mode": incident.execution_mode,
                "tool": {
                    "name": "IncidentPilot SandboxManager",
                    "image": "incidentpilot-sandbox:py312",
                },
                "external_run_id": None,
                "source_files": [scenario["source_dir"]],
                "artifacts": raw_incident_artifact,
                "error": incident.error or "未产出可复测的补丁",
            }
        )

    fault_case = (
        source_root / scenario["fault_case"] if source_root is not None else None
    )
    summary = _summary(subtasks)
    manifest: dict[str, Any] = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "manifest_version": MANIFEST_FORMAT_VERSION,
        "run_id": run_id,
        "created_at": now.astimezone(timezone.utc).isoformat(),
        "command": command,
        "scenario": {
            "id": scenario_id,
            "title": scenario["title"],
            "source": {
                "repository": scenario["source_repository"],
                "commit": scenario["source_commit"],
                "path": scenario["source_dir"],
            },
            "injected_fault": {
                "id": scenario["fault_id"],
                "type": scenario["fault_type"],
                "simulated": scenario["simulated"],
                "delay_seconds": scenario["delay_seconds"],
                "case_file": scenario["fault_case"],
                "case_sha256": (
                    sha256_file(fault_case) if fault_case and fault_case.exists() else None
                ),
            },
            "execution_modes": {
                "static_scan": scan.execution_mode,
                "failure_assertion": "real-local-assertion",
                "investigation": incident.execution_mode,
                "sandbox_retest": (
                    next(
                        (
                            item["execution_mode"]
                            for item in subtasks
                            if item["name"] == "sandbox_retest"
                        ),
                        NOT_RUN,
                    )
                ),
            },
        },
        "target": target,
        "variant": {
            "name": "baseline-with-candidate-patch",
            "patch_summary": (
                (retest or {}).get("patch_summary")
                or ((investigation or {}).get("patch_artifact") or {}).get(
                    "patch_summary"
                )
            ),
            "note": "原版本失败断言 → 候选补丁 → Docker 双重复测",
        },
        "failure_assessment": (
            {
                "status": baseline.get("status"),
                "expected": baseline.get("expected"),
                "expected_matched": baseline.get("expected_matched"),
                "threshold_ms": baseline.get("threshold_ms"),
                "observed_ms": baseline.get("observed_ms"),
                "samples_ms": baseline.get("samples_ms"),
                "linked_run_id": baseline.get("linked_run_id"),
                "supporting_evidence_ids": baseline_doc.get("supporting_evidence_ids", []),
                "conclusion": baseline.get("conclusion"),
            }
            if baseline
            else None
        ),
        "investigation": inv_summary,
        "sandbox_retest": _retest_view(retest),
        "review": (
            (investigation or {}).get("review")
            or {
                "status": "NOT_REACHED",
                "human_approval": "NOT_RUN",
                "note": "调查未到达人工审核阶段",
            }
        ),
        "subtasks": subtasks,
        "summary": summary,
        "separate_regressions": _separate_regressions(
            workspace_root, scenario["source_repository"]
        ),
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    html_path = run_dir / "report.html"
    html_path.write_text(
        render_quality_html(manifest, scan_payload), encoding="utf-8"
    )
    return manifest, run_dir


def quality_exit_code(manifest: dict[str, Any]) -> int:
    return _exit_code(manifest["summary"]["verdict"])


def render_quality_html(manifest: dict[str, Any], scan: dict[str, Any]) -> str:
    scenario = manifest.get("scenario") or {}
    source = scenario.get("source") or {}
    fault = scenario.get("injected_fault") or {}
    target = manifest.get("target") or {}
    summary = manifest.get("summary") or {}
    tool = (scan.get("tool") or {})

    rows = [
        ("run_id", manifest.get("run_id")),
        ("场景", f"{scenario.get('id')} — {scenario.get('title')}"),
        ("注入故障", f"{fault.get('id')}（{fault.get('type')}）"),
        ("故障性质", "注入的模拟延迟" if fault.get("simulated") else "真实故障"),
        ("模拟延迟", f"{fault.get('delay_seconds')} 秒"),
        ("场景源码", f"{source.get('repository')} @ {source.get('commit')}"),
        ("源码目录", source.get("path")),
        ("目标源码核验", target.get("status")),
        ("VibeCheck-AI", f"{tool.get('repository')} @ {tool.get('actual_commit') or '未读取'}"),
        ("扫描入口", tool.get("entrypoint")),
        ("执行方式", scan.get("execution_mode")),
        ("扫描范围", (scan.get("scope") or {}).get("source")),
    ]
    meta = "".join(
        f'<div><span class="muted">{_e(label)}：</span><code>{_e(value)}</code></div>'
        for label, value in rows
    )

    cards = [
        ("子任务", summary.get("total", 0)),
        ("通过", summary.get("pass", 0)),
        ("预期失败", summary.get("expected_fail", 0)),
        ("失败", summary.get("fail", 0)),
        ("错误", summary.get("error", 0)),
        ("未执行", summary.get("not_run", 0)),
    ]
    cards_html = "".join(
        f'<div class="card"><b>{_e(value)}</b><span>{_e(label)}</span></div>'
        for label, value in cards
    )
    cards_html += (
        f'<div class="card"><b>{_badge(summary.get("verdict", NOT_RUN))}</b>'
        f'<span>结论</span></div>'
    )
    review = manifest.get("review") or {}
    cards_html += (
        f'<div class="card"><b>{_badge(review.get("status", NOT_RUN))}</b>'
        f'<span>人工审核</span></div>'
    )

    subtask_rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('name'))}</td>"
        f"<td>{_badge(item.get('status'))}<div class=\"muted\">{_e(item.get('state') or '')}</div>"
        f"{'（预期失败）' if item.get('expected_status') == FAIL else ''}</td>"
        f"<td>{_e(item.get('execution_mode'))}</td>"
        f"<td>{_e(item.get('external_run_id') or '无（该子任务不提供运行 ID）')}</td>"
        f"<td>{_e(item.get('findings_count') if item.get('findings_count') is not None else '-')}</td>"
        f"<td>{_e(', '.join(a.get('path', '') for a in item.get('artifacts') or []))}</td>"
        f"<td>{_e(item.get('error') or '')}</td>"
        "</tr>"
        for item in manifest.get("subtasks") or []
    )

    scope = scan.get("scope") or {}
    scope_rows = [
        ("扫描文件数", scope.get("files_scanned")),
        ("扫描行数", scope.get("lines_scanned")),
        ("发现数", scope.get("findings_count")),
        ("跳过文件", scope.get("skipped_files_count")),
        ("读取错误", scope.get("scan_errors_count")),
        ("按等级", scope.get("by_severity")),
        ("按维度", scope.get("by_dimension")),
    ]
    scope_html = "".join(
        f'<div><span class="muted">{_e(label)}：</span><code>{_e(value)}</code></div>'
        for label, value in scope_rows
    )

    findings = scan.get("findings") or []
    finding_rows = "".join(
        "<tr>"
        f"<td>{_badge(str(item.get('severity', '')).upper())}</td>"
        f"<td><code>{_e(item.get('rule_id'))}</code><div class=\"muted\">{_e(item.get('rule_name'))}</div></td>"
        f"<td>{_e(item.get('dimension'))}<div class=\"muted\">{_e(item.get('category'))}</div></td>"
        f"<td><code>{_e(item.get('file_path'))}</code>:"
        f"{_e(item.get('line_start') if item.get('line_start') is not None else '-')}</td>"
        f"<td><code>{_e(item.get('snippet_masked'))}</code></td>"
        f"<td>{'是' if item.get('is_blocking') else '否'}</td>"
        "</tr>"
        for item in findings
    )
    if not finding_rows:
        finding_rows = '<tr><td colspan="6" class="muted">本次扫描未发现条目</td></tr>'

    error_block = ""
    if scan.get("error"):
        error_block = (
            f'<h2>阻塞原因</h2><div class="meta warn">'
            f"{_e(scan.get('error'))}</div>"
        )

    failure = manifest.get("failure_assessment") or {}
    investigation = manifest.get("investigation") or {}
    retest = manifest.get("sandbox_retest") or {}
    failure_html = ""
    if failure:
        expected_note = (
            "预期失败已命中" if failure.get("expected_matched") else "故障未按预期复现"
        )
        failure_rows = [
            ("断言", "fault_active_latency（原版本）"),
            ("阈值", f"{failure.get('threshold_ms')} 毫秒"),
            ("观测中位数", f"{failure.get('observed_ms')} 毫秒"),
            ("样本", failure.get("samples_ms")),
            ("响应与健康版本一致", failure.get("payload_equal_to_healthy")),
            ("关联运行 ID", failure.get("linked_run_id")),
            ("支持证据", failure.get("supporting_evidence_ids")),
            ("结论", f"{failure.get('conclusion')}（{expected_note}）"),
        ]
        failure_html = (
            "<h2>故障复现断言</h2><div class=\"meta\">"
            + "".join(
                f'<div><span class="muted">{_e(label)}：</span><code>{_e(value)}</code></div>'
                for label, value in failure_rows
            )
            + "</div>"
        )

    root_cause = investigation.get("root_cause") or {}
    verification = investigation.get("verification") or {}
    evidence_rows = "".join(
        "<tr>"
        f"<td><code>{_e(item.get('evidence_id'))}</code></td>"
        f"<td>{_e(item.get('source'))}</td>"
        f"<td><code>{_e(item.get('tool_call_id'))}</code></td>"
        f"<td>{_e(item.get('summary'))}</td>"
        f"<td><code>{_e(item.get('content_hash'))}</code></td>"
        "</tr>"
        for item in investigation.get("evidence") or []
    )
    if not evidence_rows:
        evidence_rows = '<tr><td colspan="5" class="muted">无证据记录</td></tr>'
    tool_rows = "".join(
        "<tr>"
        f"<td><code>{_e(item.get('tool_call_id'))}</code></td>"
        f"<td>{_e(item.get('tool_name'))}</td>"
        f"<td>{_badge(item.get('status'))}</td>"
        f"<td>{_e(item.get('latency_ms') if item.get('latency_ms') is not None else '-')}</td>"
        "</tr>"
        for item in investigation.get("tool_calls") or []
    )
    if not tool_rows:
        tool_rows = '<tr><td colspan="4" class="muted">无工具调用</td></tr>'
    inv_html = ""
    if investigation:
        inv_rows = [
            ("运行 ID", investigation.get("run_id")),
            ("事件 ID", investigation.get("incident_id")),
            ("工作流状态", investigation.get("workflow_state")),
            ("根因摘要", root_cause.get("summary")),
            ("故障类别", root_cause.get("fault_category")),
            ("影响组件", root_cause.get("affected_component")),
            ("置信度", root_cause.get("confidence")),
            ("证据绑定", verification.get("evidence_bound_to_diagnosis")),
            ("类别与预期一致", verification.get("expected_category_match")),
            ("组件与预期一致", verification.get("expected_component_match")),
            ("预期证据源", verification.get("expected_evidence_sources")),
            ("实际覆盖证据源", verification.get("expected_evidence_sources_covered")),
        ]
        inv_html = (
            "<h2>诊断证据</h2><div class=\"meta\">"
            + "".join(
                f'<div><span class="muted">{_e(label)}：</span><code>{_e(value)}</code></div>'
                for label, value in inv_rows
            )
            + "</div><h3>工具调用</h3><table><thead><tr>"
            "<th>调用 ID</th><th>工具</th><th>状态</th><th>耗时 ms</th>"
            f"</tr></thead><tbody>{tool_rows}</tbody></table>"
            "<h3>证据</h3><table><thead><tr>"
            "<th>证据 ID</th><th>来源</th><th>工具调用</th><th>摘要</th><th>内容哈希</th>"
            f"</tr></thead><tbody>{evidence_rows}</tbody></table>"
        )

    patch = investigation.get("patch_artifact") or {}
    patch_html = ""
    if patch:
        patch_rows = [
            ("补丁产物 ID", patch.get("patch_artifact_id")),
            ("基线提交", patch.get("base_commit_sha")),
            ("补丁哈希", patch.get("patch_hash")),
            ("补丁摘要", patch.get("patch_summary")),
            ("上游测试运行 ID", patch.get("test_run_id")),
        ]
        patch_html = (
            "<h2>候选补丁</h2><div class=\"meta\">"
            + "".join(
                f'<div><span class="muted">{_e(label)}：</span><code>{_e(value)}</code></div>'
                for label, value in patch_rows
            )
            + "</div><div class=\"muted\">完整补丁保存在 investigation.json。</div>"
        )

    retest_html = ""
    if retest:
        retest_rows = [
            ("状态", retest.get("status")),
            ("基线 SHA", retest.get("baseline_sha")),
            ("补丁摘要", retest.get("patch_summary")),
            ("工作区", retest.get("workspace")),
        ]
        test_rows = ""
        for name, label in (
            ("fault_active", "故障条件断言"),
            ("healthy_regression", "健康路径回归"),
        ):
            item = retest.get(name) or {}
            test_rows += (
                "<tr>"
                f"<td>{_e(label)}</td>"
                f"<td>{_badge(item.get('status'))}</td>"
                f"<td>{_e(item.get('exit_code'))}</td>"
                f"<td>{_e(item.get('mode'))}</td>"
                f"<td>{_e(item.get('duration_ms'))}</td>"
                "</tr>"
            )
        if not test_rows:
            test_rows = '<tr><td colspan="5" class="muted">无测试结果</td></tr>'
        retest_html = (
            "<h2>Docker 沙箱复测</h2><div class=\"meta\">"
            + "".join(
                f'<div><span class="muted">{_e(label)}：</span><code>{_e(value)}</code></div>'
                for label, value in retest_rows
            )
            + "</div><table><thead><tr>"
            "<th>测试</th><th>状态</th><th>退出码</th><th>模式</th><th>耗时 ms</th>"
            f"</tr></thead><tbody>{test_rows}</tbody></table>"
            "<div class=\"muted\">完整命令与输出保存在 sandbox-retest.json。</div>"
        )

    review_html = (
        "<h2>人工审核状态</h2><div class=\"meta\">"
        f'<div><span class="muted">状态：</span>{_badge(review.get("status", NOT_RUN))}</div>'
        f'<div><span class="muted">人工审批：</span><code>{_e(review.get("human_approval", "NOT_RUN"))}</code></div>'
        f'<div>{_e(review.get("note") or "")}</div></div>'
    )

    separate = manifest.get("separate_regressions") or {}
    separate_html = ""
    if separate.get("name"):
        separate_html = (
            "<h2>独立回归</h2><div class=\"meta\">"
            f'<div><span class="muted">套件：</span><code>{_e(separate.get("name"))}</code></div>'
            f'<div><span class="muted">目标：</span><code>{_e(separate.get("repository"))} @ '
            f'{_e(separate.get("commit"))}</code></div>'
            f'<div><span class="muted">报告：</span><code>{_e(separate.get("report"))}</code></div>'
            f'<div>{_e(separate.get("note"))}</div></div>'
        )

    body = (
        "<h1>质量闭环运行报告</h1>"
        f'<p class="muted">场景 {_e(scenario.get("id"))} · 运行 ID {_e(manifest.get("run_id"))}</p>'
        f'<div class="cards">{cards_html}</div>'
        f'<h2>运行元数据</h2><div class="meta">{meta}</div>'
        "<h2>子任务</h2><table><thead><tr>"
        "<th>子任务</th><th>状态</th><th>执行方式</th><th>外部运行 ID</th>"
        "<th>发现</th><th>产物</th><th>错误</th>"
        f"</tr></thead><tbody>{subtask_rows}</tbody></table>"
        f"{error_block}"
        f"<h2>扫描范围</h2><div class=\"meta\">{scope_html}</div>"
        "<h2>扫描发现（上游脱敏片段）</h2><table><thead><tr>"
        "<th>等级</th><th>规则</th><th>维度 / 类别</th><th>位置</th>"
        "<th>脱敏片段</th><th>阻断</th>"
        f"</tr></thead><tbody>{finding_rows}</tbody></table>"
        f"{failure_html}{inv_html}{patch_html}{retest_html}{review_html}"
        f"{separate_html}"
        "<h2>运行清单</h2>"
        f"<details><summary>manifest.json</summary><div><pre>{_json(manifest)}</pre></div></details>"
        "<details><summary>scan.json（脱敏字段）</summary><div><pre>"
        f"{_json({key: scan.get(key) for key in ('scanner', 'status', 'state', 'execution_mode', 'tool', 'scope', 'findings', 'raw_artifact')})}"
        "</pre></div></details>"
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>质量闭环运行报告</title>'
        f"<style>{_CSS}</style></head><body><main>{body}</main></body></html>\n"
    )
