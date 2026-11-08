"""PawBench 参考的 2×2 模型 × Harness 交叉实验清单。"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .html_report import _CSS, _badge, _e, _json
from .oracle import ERROR, NOT_RUN, PASS
from .quality import _checkout_state, resolve_incidentpilot_dir
from .report import sha256_file

PAWBENCH_REPOSITORY = "https://github.com/agentscope-ai/PawBench"
PAWBENCH_COMMIT = "0f794a8bb6c27aa9ee4091b2691fa30e4ed9cc8f"
MATRIX_FORMAT_VERSION = "1.0"
DEFAULT_SPEC = (
    Path(__file__).resolve().parents[2] / "benchmarks" / "matrix" / "pawbench-2x2.json"
)

STATE_UNAVAILABLE = "incidentpilot_unavailable"
STATE_SPEC_INVALID = "matrix_spec_invalid"
STATE_MODEL_CONFIG = "model_config_missing"
STATE_TIMEOUT = "matrix_timeout"
STATE_UPSTREAM_FAILURE = "matrix_upstream_failure"
STATE_MISSING_ARTIFACTS = "matrix_missing_artifacts"
STATE_PARSE_FAILURE = "matrix_parse_failure"


def load_spec(path: Path | str | None = None) -> tuple[dict[str, Any], Path]:
    spec_path = Path(DEFAULT_SPEC if path is None else path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if len(spec.get("models", [])) != 2:
        raise ValueError("2×2 矩阵需要恰好两个模型配置槽")
    if sorted(spec.get("strategies", [])) != ["autonomous", "fixed"]:
        raise ValueError("策略必须为 fixed 与 autonomous")
    if len(spec.get("scenarios", [])) != 2:
        raise ValueError("矩阵需要恰好两个场景")
    if int(spec.get("repetitions", 0)) < 2:
        raise ValueError("每个组合至少重复 2 次")
    return spec, spec_path


def real_model_config(
    spec: dict[str, Any], environ: dict[str, str] | None = None
) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    missing: list[str] = []
    resolved = []
    for model in spec["models"]:
        prefix = "PAWBENCH_MODEL_" + model["id"].split("-")[-1].upper()
        keys = [f"{prefix}_API_KEY", f"{prefix}_MODEL", f"{prefix}_BASE_URL"]
        absent = [key for key in keys if not environ.get(key)]
        missing.extend(absent)
        resolved.append(
            {
                "id": model["id"],
                "model_configured": bool(environ.get(f"{prefix}_MODEL")),
                "base_url_configured": bool(environ.get(f"{prefix}_BASE_URL")),
            }
        )
    return {"configured": not missing, "missing": missing, "models": resolved}


def summarize(trials: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for trial in trials:
        key = f"{trial['model_id']} × {trial['strategy']}"
        group = groups.setdefault(
            key,
            {
                "model_id": trial["model_id"],
                "strategy": trial["strategy"],
                "trials": 0,
                "pass": 0,
                "error": 0,
                "root_cause_correct": 0,
                "tool_selection_sum": 0.0,
                "duration_sum": 0.0,
            },
        )
        group["trials"] += 1
        group["pass"] += trial["status"] == PASS
        group["error"] += trial["status"] != PASS
        group["root_cause_correct"] += bool(trial["result"].get("root_cause_correct"))
        group["tool_selection_sum"] += trial["result"].get("tool_selection_score") or 0.0
        group["duration_sum"] += trial.get("duration_seconds") or 0.0
    for group in groups.values():
        count = group["trials"] or 1
        group["root_cause_accuracy"] = round(group["root_cause_correct"] / count, 4)
        group["avg_tool_selection_score"] = round(group["tool_selection_sum"] / count, 4)
        group["avg_duration_seconds"] = round(group["duration_sum"] / count, 3)
        group.pop("tool_selection_sum")
        group.pop("duration_sum")
    by_scenario: dict[str, dict[str, Any]] = {}
    for trial in trials:
        bucket = by_scenario.setdefault(
            trial["scenario"], {"trials": 0, "pass": 0, "root_cause_correct": 0}
        )
        bucket["trials"] += 1
        bucket["pass"] += trial["status"] == PASS
        bucket["root_cause_correct"] += bool(trial["result"].get("root_cause_correct"))
    for bucket in by_scenario.values():
        bucket["root_cause_accuracy"] = round(
            bucket["root_cause_correct"] / (bucket["trials"] or 1), 4
        )
    return {
        "trials": len(trials),
        "pass": sum(trial["status"] == PASS for trial in trials),
        "error": sum(trial["status"] != PASS for trial in trials),
        "groups": list(groups.values()),
        "by_scenario": by_scenario,
    }


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


def run_matrix(
    *,
    out: Path | str,
    mode: str = "offline",
    command: str = "",
    spec_path: Path | str | None = None,
    incidentpilot_dir: Path | str | None = None,
    timeout: float = 1200.0,
    runner=subprocess.run,
    environ: dict[str, str] | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
) -> tuple[dict[str, Any], Path]:
    if mode not in {"offline", "model"}:
        raise ValueError(f"未知矩阵模式：{mode}")
    environ = os.environ if environ is None else environ
    now = datetime.now(timezone.utc) if now is None else now
    run_id = uuid4().hex if run_id is None else run_id
    base = Path(out)
    base.mkdir(parents=True, exist_ok=True)
    run_dir = _run_dir(base, now, run_id)
    manifest: dict[str, Any] = {
        "format": f"matrix/{MATRIX_FORMAT_VERSION}",
        "run_id": run_id,
        "created_at": now.isoformat(),
        "command": command,
        "mode": mode,
        "status": ERROR,
        "state": None,
        "error": None,
        "execution_mode": "offline-scripted" if mode == "offline" else "real-service",
        "reference": {
            "name": "PawBench",
            "repository": PAWBENCH_REPOSITORY,
            "commit": PAWBENCH_COMMIT,
            "note": "参考其 runner 结构；矩阵为本项目实验，不是官方榜单",
        },
        "spec": {},
        "target": {},
        "budget": {},
        "summary": {},
        "real_matrix": {},
        "trials": [],
        "errors": [],
        "artifacts": [],
        "notes": [],
    }
    try:
        spec, resolved_spec = load_spec(spec_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        manifest["state"] = STATE_SPEC_INVALID
        manifest["error"] = f"矩阵清单不可用：{exc}"
        return _finish(manifest, run_dir, [], None)
    manifest["spec"] = {
        "path": str(Path(spec_path or DEFAULT_SPEC)),
        "sha256": sha256_file(resolved_spec),
        "version": spec.get("version"),
        "models": spec["models"],
        "strategies": spec["strategies"],
        "scenarios": spec["scenarios"],
        "repetitions": spec["repetitions"],
        "judge": spec.get("judge"),
    }
    manifest["budget"] = spec.get("budget") or {}
    manifest["real_matrix"] = {
        "status": NOT_RUN,
        "state": STATE_MODEL_CONFIG,
        "requirements": spec.get("real_model_requirements") or {},
        "command": (
            "uv run --locked agentreliability matrix --mode model "
            f"--incidentpilot-dir {incidentpilot_dir or '<incidentpilot>'} "
            f"--out {out}"
        ),
    }

    root = resolve_incidentpilot_dir(incidentpilot_dir, environ)
    target = _checkout_state(root, "https://github.com/super-ZXQ/incidentpilot.git", "566639ea876eed171c705ad0c1a0d79276b8584b")
    manifest["target"] = {
        key: target.get(key)
        for key in ("repository", "commit", "commit_verified", "status", "error")
    }
    if target["status"] != PASS or root is None:
        manifest["state"] = STATE_UNAVAILABLE
        manifest["error"] = target.get("error") or "IncidentPilot checkout 不可用"
        return _finish(manifest, run_dir, [], root)

    if mode == "model":
        config = real_model_config(spec, environ)
        manifest["real_matrix"].update(config)
        if not config["configured"]:
            manifest["status"] = NOT_RUN
            manifest["state"] = STATE_MODEL_CONFIG
            manifest["error"] = "缺少两个模型配置：" + "、".join(config["missing"])
            manifest["real_matrix"]["status"] = NOT_RUN
            return _finish(manifest, run_dir, [], root)
    else:
        manifest["notes"].append(
            "离线矩阵使用 IncidentPilot 的脚本化模型与合成工具，模型轴为两个占位配置，"
            "只验证矩阵执行与汇总；真实矩阵需两个授权模型配置"
        )

    spec_file = (run_dir / "matrix.spec.json").resolve()
    spec_file.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output_path = (run_dir / "matrix.raw.json").resolve()
    python = root / ".venv" / "bin" / "python"
    if not python.exists():
        manifest["state"] = STATE_UNAVAILABLE
        manifest["error"] = "IncidentPilot 独立 venv 缺失"
        return _finish(manifest, run_dir, [], root)
    script = Path(__file__).resolve().parent / "_incidentpilot_matrix.py"
    child_env = dict(os.environ)
    if mode == "offline":
        for key in (
            "LLM_API_KEY",
            "LLM_BASE_URL",
            "LLM_MODEL",
            "LLM_ENABLED",
        ):
            child_env.pop(key, None)
    child_env["PYTHONPATH"] = str(root / "src")
    child_env["GITHUB_INTEGRATION_ENABLED"] = "false"
    command_list = [str(python), str(script), str(root), str(spec_file), str(output_path)]
    try:
        completed = runner(
            command_list,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        manifest["state"] = STATE_TIMEOUT
        manifest["error"] = f"矩阵执行超时（>{timeout}s）"
        return _finish(manifest, run_dir, [], root)
    except OSError as exc:
        manifest["state"] = STATE_UNAVAILABLE
        manifest["error"] = f"无法启动矩阵执行：{exc}"
        return _finish(manifest, run_dir, [], root)
    manifest["target"]["command_exit_code"] = completed.returncode
    manifest["target"]["stdout_tail"] = (completed.stdout or "")[-500:]
    manifest["target"]["stderr_tail"] = (completed.stderr or "")[-500:]
    if completed.returncode != 0 and not output_path.exists():
        manifest["state"] = STATE_UPSTREAM_FAILURE
        manifest["error"] = f"矩阵进程退出 {completed.returncode}"
        return _finish(manifest, run_dir, [], root)
    if not output_path.exists():
        manifest["state"] = STATE_MISSING_ARTIFACTS
        manifest["error"] = "矩阵进程未写出结果文件"
        return _finish(manifest, run_dir, [], root)
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        manifest["state"] = STATE_PARSE_FAILURE
        manifest["error"] = f"矩阵结果无法解析：{exc}"
        return _finish(manifest, run_dir, [], root)

    trials = payload.get("trials") or []
    errors = payload.get("errors") or []
    expected = (
        len(spec["models"]) * len(spec["strategies"]) * len(spec["scenarios"]) * int(spec["repetitions"])
    )
    manifest["trials"] = trials
    manifest["errors"] = errors
    manifest["summary"] = summarize(trials)
    manifest["summary"]["expected_trials"] = expected
    if payload.get("status") != PASS or len(trials) != expected:
        manifest["status"] = ERROR
        manifest["state"] = STATE_UPSTREAM_FAILURE
        manifest["error"] = f"计划 {expected} 次，实际完成 {len(trials)} 次"
    else:
        manifest["status"] = PASS
        if mode == "model":
            manifest["real_matrix"]["status"] = PASS
    return _finish(manifest, run_dir, trials, root)


def _finish(
    manifest: dict[str, Any],
    run_dir: Path,
    trials: list[dict[str, Any]],
    root: Path | None,
) -> tuple[dict[str, Any], Path]:
    trials_path = run_dir / "trials.json"
    trials_path.write_text(
        json.dumps(trials, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    manifest_path = run_dir / "manifest.json"
    manifest["artifacts"] = [_artifact(run_dir, trials_path)]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    report_path = run_dir / "report.html"
    report_path.write_text(render_matrix_html(manifest, trials), encoding="utf-8")
    manifest["artifacts"] += [
        _artifact(run_dir, manifest_path),
        _artifact(run_dir, report_path),
    ]
    return manifest, run_dir


def matrix_exit_code(status: str) -> int:
    return 0 if status == PASS else 2


def render_matrix_html(manifest: dict[str, Any], trials: list[dict[str, Any]]) -> str:
    summary = manifest.get("summary") or {}
    cards = [
        ("试验", summary.get("trials", 0)),
        ("计划", summary.get("expected_trials", 0)),
        ("通过", summary.get("pass", 0)),
        ("错误", summary.get("error", 0)),
    ]
    card_html = "".join(
        f"<div class='card'><b>{_e(value)}</b><span>{_e(label)}</span></div>"
        for label, value in cards
    )
    group_rows = "".join(
        "<tr>"
        f"<td>{_e(item['model_id'])}</td>"
        f"<td>{_e(item['strategy'])}</td>"
        f"<td>{_e(item['trials'])}</td>"
        f"<td>{_e(item['root_cause_accuracy'])}</td>"
        f"<td>{_e(item['avg_tool_selection_score'])}</td>"
        f"<td>{_e(item['avg_duration_seconds'])}</td>"
        "</tr>"
        for item in summary.get("groups", [])
    )
    trial_rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('model_id'))}</td>"
        f"<td>{_e(item.get('strategy'))}</td>"
        f"<td>{_e(item.get('scenario'))}</td>"
        f"<td>{_e(item.get('trial'))}</td>"
        f"<td>{_badge(item.get('status'))}</td>"
        f"<td>{_e(item.get('final_status'))}</td>"
        f"<td>{_e((item.get('result') or {}).get('root_cause_correct'))}</td>"
        f"<td>{_e((item.get('result') or {}).get('tool_selection_score'))}</td>"
        f"<td>{_e(item.get('duration_seconds'))}</td>"
        f"<td>{_e((item.get('usage') or {}).get('tokens'))}</td>"
        "</tr>"
        for item in trials
    )
    error_rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('model_id'))}</td>"
        f"<td>{_e(item.get('strategy'))}</td>"
        f"<td>{_e(item.get('scenario'))}</td>"
        f"<td>{_e(item.get('trial'))}</td>"
        f"<td>{_e(item.get('error'))}</td>"
        "</tr>"
        for item in manifest.get("errors", [])
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>2×2 矩阵 {_e(manifest['run_id'][:8])}</title><style>{_CSS}</style></head>
<body><main>
<h1>模型 × Harness 2×2 矩阵</h1>
<p class="muted">参考 PawBench：固定场景、数据、判定器与预算；本项目实验</p>
<div class="meta">
<div>状态：{_badge(manifest.get('status'))} {_e(manifest.get('state') or '')}</div>
<div>模式：{_e(manifest['mode'])} ｜ 执行方式：{_e(manifest['execution_mode'])}</div>
<div>场景源码：{_e(manifest['target'].get('repository'))} @ {_e(manifest['target'].get('commit'))} ｜ 已验证：{_e(manifest['target'].get('commit_verified'))}</div>
<div>预算：{_e(_json(manifest.get('budget')))}</div>
<div>真实矩阵：{_badge(manifest['real_matrix'].get('status') or 'NOT_RUN')} {_e(manifest['real_matrix'].get('error') or '待两个授权模型配置')}</div>
{_e(manifest.get('error') or '')}
</div>
<h2>汇总</h2><div class="cards">{card_html}</div>
<h2>模型 × 策略</h2>
<table><thead><tr><th>模型</th><th>策略</th><th>试验</th><th>根因准确率</th><th>工具选择</th><th>平均耗时(s)</th></tr></thead>
<tbody>{group_rows}</tbody></table>
<h2>逐次试验</h2>
<table><thead><tr><th>模型</th><th>策略</th><th>场景</th><th>试验</th><th>状态</th><th>最终状态</th><th>根因正确</th><th>工具选择</th><th>耗时(s)</th><th>用量</th></tr></thead>
<tbody>{trial_rows or '<tr><td colspan=10 class="muted">无</td></tr>'}</tbody></table>
<h2>失败</h2>
<table><thead><tr><th>模型</th><th>策略</th><th>场景</th><th>试验</th><th>错误</th></tr></thead>
<tbody>{error_rows or '<tr><td colspan=5 class="muted">无</td></tr>'}</tbody></table>
<h2>产物</h2><pre>{_json(manifest.get('artifacts'))}</pre>
</main></body></html>
"""
