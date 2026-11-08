"""平台化质量闭环：场景清单、静态扫描子任务与可审阅 HTML。"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .html_report import _CSS, _badge, _e, _json
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
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if environ.get("INCIDENTPILOT_DIR"):
        candidates.append(Path(environ["INCIDENTPILOT_DIR"]))
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


def _summary(subtasks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        "total": len(subtasks),
        "pass": sum(item["status"] == PASS for item in subtasks),
        "fail": sum(item["status"] == FAIL for item in subtasks),
        "error": sum(item["status"] == ERROR for item in subtasks),
        "not_run": sum(item["status"] == NOT_RUN for item in subtasks),
    }
    if counts["error"] or counts["not_run"]:
        counts["verdict"] = ERROR
    elif counts["fail"]:
        counts["verdict"] = FAIL
    else:
        counts["verdict"] = PASS
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
    scan_runner=subprocess.run,
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
                "dynamic_retest": "NOT_RUN（待项目 3 接入）",
            },
        },
        "target": target,
        "variant": {
            "name": "baseline",
            "patch_summary": None,
            "note": "原版本，未应用候选补丁",
        },
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

    subtask_rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('name'))}</td>"
        f"<td>{_badge(item.get('status'))}<div class=\"muted\">{_e(item.get('state') or '')}</div></td>"
        f"<td>{_e(item.get('execution_mode'))}</td>"
        f"<td>{_e(item.get('external_run_id') or '无（扫描器不提供运行 ID）')}</td>"
        f"<td>{_e(item.get('findings_count'))}</td>"
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
