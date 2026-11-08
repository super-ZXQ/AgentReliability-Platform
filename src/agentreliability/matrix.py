"""PawBench 参考的 2×2 模型 × Harness 交叉实验清单。"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ._incidentpilot_matrix import ReferenceResetError, local_reference_endpoint, reset_reference
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
STATE_REAL_ENV_UNAVAILABLE = "matrix_real_env_unavailable"
STATE_REAL_RESULT_INVALID = "matrix_real_result_invalid"


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


def model_prefix(spec: dict[str, Any], model: dict[str, Any]) -> str:
    """按清单元数据解析每个模型槽独立的环境变量前缀。"""
    requirement = (spec.get("real_model_requirements") or {}).get(model["id"]) or []
    if requirement:
        first = str(requirement[0])
        for suffix in ("_API_KEY", "_MODEL", "_BASE_URL"):
            if first.endswith(suffix):
                return first[: -len(suffix)]
        return first
    return "PAWBENCH_MODEL_" + model["id"].split("-")[-1].upper()


def real_model_config(
    spec: dict[str, Any], environ: dict[str, str] | None = None
) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    missing: list[str] = []
    resolved = []
    for model in spec["models"]:
        prefix = model_prefix(spec, model)
        keys = [f"{prefix}_API_KEY", f"{prefix}_MODEL", f"{prefix}_BASE_URL"]
        absent = [key for key in keys if not environ.get(key)]
        missing.extend(absent)
        resolved.append(
            {
                "id": model["id"],
                "prefix": prefix,
                "model": environ.get(f"{prefix}_MODEL") or None,
                "base_url": environ.get(f"{prefix}_BASE_URL") or None,
                "model_configured": bool(environ.get(f"{prefix}_MODEL")),
                "base_url_configured": bool(environ.get(f"{prefix}_BASE_URL")),
            }
        )
    return {"configured": not missing, "missing": missing, "models": resolved}


def _basic_counts(trials: list[dict[str, Any]]) -> dict[str, Any]:
    judgeable = [
        trial
        for trial in trials
        if (trial.get("result") or {}).get("root_cause_correct") is not None
    ]
    correct = sum(
        bool((trial.get("result") or {}).get("root_cause_correct"))
        for trial in judgeable
    )
    return {
        "trials": len(trials),
        "workflow_completed": sum(trial["status"] == PASS for trial in trials),
        "judgeable": len(judgeable),
        "root_cause_correct": correct,
        "root_cause_incorrect": len(judgeable) - correct,
        "error": sum(trial["status"] != PASS for trial in trials),
    }


def _diagnosis_trace(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for trial in trials:
        result = trial.get("result") or {}
        if result.get("root_cause_correct") is None:
            continue
        expected = str(result.get("expected_category") or "")
        predicted = str(result.get("predicted_category") or "")
        model_id = trial.get("model_id")
        key = (str(model_id or ""), expected, predicted)
        bucket = buckets.setdefault(
            key,
            {
                "expected_category": expected,
                "predicted_category": predicted,
                "count": 0,
                "root_cause_correct": 0,
                "fault_category_correct": 0,
                "category_alias_covered": bool(
                    result.get("category_alias_covered")
                ),
                "model_id": model_id,
                "model_backend": trial.get("model_backend"),
            },
        )
        bucket["count"] += 1
        bucket["root_cause_correct"] += bool(result.get("root_cause_correct"))
        bucket["fault_category_correct"] += bool(
            result.get("fault_category_correct")
        )
    trace: list[dict[str, Any]] = []
    for bucket in buckets.values():
        bucket["incorrect"] = bucket["count"] - bucket["root_cause_correct"]
        if bucket["incorrect"] == 0:
            bucket["source"] = "matches_ground_truth"
        elif not bucket["predicted_category"]:
            bucket["source"] = "model_output_missing"
        elif (
            bucket["category_alias_covered"]
            and bucket["fault_category_correct"] == 0
        ):
            bucket["source"] = "scoring_mapping"
        else:
            bucket["source"] = "model_output"
        trace.append(bucket)
    trace.sort(key=lambda item: (-item["count"], item["predicted_category"]))
    return trace


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
                "judgeable": 0,
                "root_cause_correct": 0,
                "root_cause_incorrect": 0,
                "tool_selection_sum": 0.0,
                "duration_sum": 0.0,
            },
        )
        result = trial.get("result") or {}
        group["trials"] += 1
        group["pass"] += trial["status"] == PASS
        group["error"] += trial["status"] != PASS
        if result.get("root_cause_correct") is not None:
            group["judgeable"] += 1
            group["root_cause_correct"] += bool(result.get("root_cause_correct"))
            group["root_cause_incorrect"] += not bool(
                result.get("root_cause_correct")
            )
        group["tool_selection_sum"] += result.get("tool_selection_score") or 0.0
        group["duration_sum"] += trial.get("duration_seconds") or 0.0
    for group in groups.values():
        judgeable = group["judgeable"] or 1
        group["root_cause_accuracy"] = round(
            group["root_cause_correct"] / judgeable, 4
        )
        group["avg_tool_selection_score"] = round(
            group["tool_selection_sum"] / (group["trials"] or 1), 4
        )
        group["avg_duration_seconds"] = round(
            group["duration_sum"] / (group["trials"] or 1), 3
        )
        group.pop("tool_selection_sum")
        group.pop("duration_sum")
    by_scenario: dict[str, dict[str, Any]] = {}
    for trial in trials:
        bucket = by_scenario.setdefault(
            trial["scenario"],
            {
                "trials": 0,
                "pass": 0,
                "judgeable": 0,
                "root_cause_correct": 0,
                "root_cause_incorrect": 0,
            },
        )
        result = trial.get("result") or {}
        bucket["trials"] += 1
        bucket["pass"] += trial["status"] == PASS
        if result.get("root_cause_correct") is not None:
            bucket["judgeable"] += 1
            bucket["root_cause_correct"] += bool(result.get("root_cause_correct"))
            bucket["root_cause_incorrect"] += not bool(
                result.get("root_cause_correct")
            )
    for bucket in by_scenario.values():
        bucket["root_cause_accuracy"] = round(
            bucket["root_cause_correct"] / (bucket["judgeable"] or 1), 4
        )
    totals = _basic_counts(trials)
    trace = _diagnosis_trace(trials)
    by_source: dict[str, int] = {}
    for item in trace:
        if item["incorrect"] <= 0:
            continue
        by_source[item["source"]] = by_source.get(item["source"], 0) + item["incorrect"]
    return {
        **totals,
        "pass": totals["workflow_completed"],
        "groups": list(groups.values()),
        "by_scenario": by_scenario,
        "diagnosis_trace": trace,
        "root_cause_discrepancy": {
            "incorrect": totals["root_cause_incorrect"],
            "by_source": by_source,
        },
    }


def reference_orders_api(environ: dict[str, str]) -> str:
    return (
        environ.get("PAWBENCH_REFERENCE_ORDERS_API_URL")
        or environ.get("REFERENCE_ORDERS_API_URL")
        or "http://127.0.0.1:8001"
    )


def _default_reference_probe(url: str, timeout: float) -> tuple[bool, str]:
    import urllib.error
    import urllib.request

    try:
        request = urllib.request.Request(url.rstrip("/") + "/orders")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status) in {200, 500, 504}, f"HTTP {response.status}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"


def validate_real_result(
    spec: dict[str, Any],
    config: dict[str, Any],
    trials: list[dict[str, Any]],
    payload: dict[str, Any],
) -> list[str]:
    """真实结果校验：执行来源、模型标识与实际 provider 元数据必须一致。"""
    issues: list[str] = []
    execution = payload.get("execution") or {}
    if execution.get("mode") != "model":
        issues.append(f"结果文件执行模式不是 model：{execution.get('mode')}")
    if execution.get("tier") != "real_llm_investigation":
        issues.append(f"结果文件执行层级不是 real_llm_investigation：{execution.get('tier')}")
    slot_models = {item["id"]: item.get("model") for item in config.get("models", [])}
    if not slot_models:
        issues.append("缺少模型槽配置")
    for index, trial in enumerate(trials, start=1):
        model_id = trial.get("model_id")
        declared = slot_models.get(model_id)
        label = f"试验 {index}（{model_id} / {trial.get('strategy')} / {trial.get('scenario')}）"
        if declared is None:
            issues.append(f"{label} 使用了未知模型槽")
            continue
        if trial.get("model_backend") != "real-llm":
            issues.append(f"{label} 执行来源为 {trial.get('model_backend')}，不是真实模型")
        trial_execution = trial.get("execution") or {}
        if trial_execution.get("tier") != "real_llm_investigation":
            issues.append(f"{label} 执行层级为 {trial_execution.get('tier')}")
        if not trial_execution.get("llm_enabled"):
            issues.append(f"{label} 未启用真实模型")
        if trial_execution.get("tool_backend") != "mcp":
            issues.append(f"{label} 工具后端为 {trial_execution.get('tool_backend')}，不是 mcp")
        if trial.get("model_identifier") != declared:
            issues.append(
                f"{label} 模型标识 {trial.get('model_identifier')} 与槽配置 {declared} 不一致"
            )
        provider_model = trial.get("provider_model")
        actual = provider_model.get("model") if isinstance(provider_model, dict) else None
        if trial.get("status") == PASS and not actual:
            issues.append(f"{label} 缺少真实 provider 模型元数据")
        elif actual and actual != declared:
            issues.append(f"{label} 实际 provider 模型 {actual} 与槽配置 {declared} 不一致")
    return issues


def real_result_verdict(
    trials: list[dict[str, Any]],
    payload: dict[str, Any],
    issues: list[str],
) -> tuple[str, str | None, str | None]:
    """真实模式的唯一放行路径：无试验 → NOT_RUN；校验不通过 → ERROR。"""
    if not trials and not (payload.get("errors") or payload.get("error")):
        return (
            NOT_RUN,
            STATE_REAL_ENV_UNAVAILABLE,
            "真实执行器未产出任何试验",
        )
    if payload.get("status") != PASS:
        return (
            ERROR,
            STATE_UPSTREAM_FAILURE,
            "真实执行器未完成计划试验",
        )
    if issues:
        return (
            ERROR,
            STATE_REAL_RESULT_INVALID,
            "真实结果校验失败：" + "；".join(issues[:5]),
        )
    return PASS, None, None


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
    reference_probe=None,
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
        url = reference_orders_api(environ)
        probe = reference_probe or _default_reference_probe
        try:
            local_reference_endpoint(url)
            reachable, detail = probe(url, 5.0)
        except Exception as exc:  # noqa: BLE001 - 环境探测失败即视为不可用
            reachable, detail = False, f"{type(exc).__name__}: {str(exc)[:200]}"
        server = root / "ops_mcp" / "ops_readonly" / "server.py"
        manifest["real_matrix"]["reference_orders_api"] = {
            "url": url,
            "reachable": reachable,
            "detail": detail,
        }
        manifest["real_matrix"]["mcp_server_present"] = server.exists()
        if not reachable or not server.exists():
            reasons = []
            if not reachable:
                reasons.append(f"reference orders API 不可达（{url}）：{detail}")
            if not server.exists():
                reasons.append("IncidentPilot 只读 MCP 服务缺失")
            manifest["status"] = NOT_RUN
            manifest["state"] = STATE_REAL_ENV_UNAVAILABLE
            manifest["error"] = "；".join(reasons)
            manifest["real_matrix"]["status"] = NOT_RUN
            manifest["notes"].append(
                "真实矩阵需要可用的 reference orders API 与只读 MCP 服务；未生成任何真实结论"
            )
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
    child_env.update(environ)
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
    command_list = [
        str(python),
        str(script),
        str(root),
        str(spec_file),
        str(output_path),
        mode,
    ]
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
        if mode == "model":
            try:
                reset_reference(url)
            except ReferenceResetError as exc:
                manifest["error"] += f"。{exc}"
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
    if mode == "model":
        issues = validate_real_result(spec, config, trials, payload)
        if len(trials) != expected:
            issues.append(f"试验数量不符：计划 {expected}，实际 {len(trials)}")
        manifest["real_matrix"]["validation"] = {"ok": not issues, "issues": issues}
        status, state, error = real_result_verdict(trials, payload, issues)
        manifest["status"] = status
        manifest["state"] = state
        if error:
            if payload.get("error"):
                error += "：" + str(payload["error"])
            if not trials and errors:
                error += "：" + "；".join(
                    str(item.get("error") or item) for item in errors[:3]
                )
            manifest["error"] = error
        manifest["real_matrix"]["status"] = status
    elif payload.get("status") != PASS or len(trials) != expected:
        manifest["status"] = ERROR
        manifest["state"] = STATE_UPSTREAM_FAILURE
        manifest["error"] = f"计划 {expected} 次，实际完成 {len(trials)} 次"
    else:
        manifest["status"] = PASS
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
        ("流程完成", summary.get("workflow_completed", 0)),
        ("可判定样本", summary.get("judgeable", 0)),
        ("根因正确", summary.get("root_cause_correct", 0)),
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
        f"<td>{_e(item['workflow_completed'] if 'workflow_completed' in item else item['pass'])}</td>"
        f"<td>{_e(item.get('judgeable', 0))}</td>"
        f"<td>{_e(item.get('root_cause_correct', 0))}</td>"
        f"<td>{_e(item.get('root_cause_incorrect', 0))}</td>"
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
        f"<td>{_e((item.get('result') or {}).get('expected_category') or '-')}</td>"
        f"<td>{_e((item.get('result') or {}).get('predicted_category') or '-')}</td>"
        f"<td>{_e((item.get('result') or {}).get('tool_selection_score'))}</td>"
        f"<td>{_e(item.get('duration_seconds'))}</td>"
        f"<td>{_e((item.get('usage') or {}).get('tokens'))}</td>"
        "</tr>"
        for item in trials
    )
    trace_rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('model_id') or '')}</td>"
        f"<td>{_e(item.get('expected_category') or '-')}</td>"
        f"<td>{_e(item.get('predicted_category') or '-')}</td>"
        f"<td>{_e(item.get('count'))}</td>"
        f"<td>{_e(item.get('root_cause_correct'))}</td>"
        f"<td>{_e(item.get('incorrect'))}</td>"
        f"<td>{_e(item.get('category_alias_covered'))}</td>"
        f"<td>{_e(item.get('source'))}</td>"
        "</tr>"
        for item in summary.get("diagnosis_trace", [])
    )
    discrepancy = summary.get("root_cause_discrepancy") or {}
    scenario_rows = "".join(
        "<tr>"
        f"<td>{_e(name)}</td>"
        f"<td>{_e(item.get('trials'))}</td>"
        f"<td>{_e(item.get('judgeable', 0))}</td>"
        f"<td>{_e(item.get('root_cause_correct', 0))}</td>"
        f"<td>{_e(item.get('root_cause_incorrect', 0))}</td>"
        f"<td>{_e(item.get('root_cause_accuracy'))}</td>"
        "</tr>"
        for name, item in (summary.get("by_scenario") or {}).items()
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
<div>根因差异：{_e(_json(discrepancy))}</div>
{_e(manifest.get('error') or '')}
</div>
<h2>汇总</h2><div class="cards">{card_html}</div>
<h2>模型 × 策略</h2>
<table><thead><tr><th>模型</th><th>策略</th><th>试验</th><th>流程完成</th><th>可判定</th><th>根因正确</th><th>根因错误</th><th>根因准确率</th><th>工具选择</th><th>平均耗时(s)</th></tr></thead>
<tbody>{group_rows}</tbody></table>
<h2>按场景</h2>
<table><thead><tr><th>场景</th><th>试验</th><th>可判定</th><th>根因正确</th><th>根因错误</th><th>根因准确率</th></tr></thead>
<tbody>{scenario_rows or '<tr><td colspan=6 class="muted">无</td></tr>'}</tbody></table>
<h2>根因差异追溯</h2>
<table><thead><tr><th>模型</th><th>预期类别（金标）</th><th>实际类别（模型输出）</th><th>样本</th><th>正确</th><th>错误</th><th>别名覆盖</th><th>差异来源</th></tr></thead>
<tbody>{trace_rows or '<tr><td colspan=8 class="muted">无</td></tr>'}</tbody></table>
<h2>逐次试验</h2>
<table><thead><tr><th>模型</th><th>策略</th><th>场景</th><th>试验</th><th>状态</th><th>最终状态</th><th>根因正确</th><th>预期类别</th><th>实际类别</th><th>工具选择</th><th>耗时(s)</th><th>用量</th></tr></thead>
<tbody>{trial_rows or '<tr><td colspan=12 class="muted">无</td></tr>'}</tbody></table>
<h2>失败</h2>
<table><thead><tr><th>模型</th><th>策略</th><th>场景</th><th>试验</th><th>错误</th></tr></thead>
<tbody>{error_rows or '<tr><td colspan=5 class="muted">无</td></tr>'}</tbody></table>
<h2>产物</h2><pre>{_json(manifest.get('artifacts'))}</pre>
</main></body></html>
"""
