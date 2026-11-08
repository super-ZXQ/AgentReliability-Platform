"""OpenJudge 语义评分适配器：证据支持判定、冻结样本与独立计分。

语义评分只用于独立展示，不参与离线硬门禁；离线硬门禁仍由确定性断言负责。
"""

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
from .report import sha256_file

OPENJUDGE_REPOSITORY = "https://github.com/agentscope-ai/OpenJudge"
OPENJUDGE_COMMIT = "b83f446b09821257da9c2070ca405fa71cc22690"
GRADER_NAME = "reasoning_groundedness"

STATE_UNAVAILABLE = "openjudge_unavailable"
STATE_REVISION_MISMATCH = "openjudge_revision_mismatch"
STATE_DIRTY = "openjudge_checkout_dirty"
STATE_SAMPLES_CHANGED = "samples_changed_after_freeze"
STATE_SPLIT_UNKNOWN = "unknown_split"
STATE_MODEL_CONFIG = "model_config_missing"
STATE_LABELS_UNREVIEWED = "labels_not_human_reviewed"
STATE_TIMEOUT = "grader_timeout"
STATE_UPSTREAM_FAILURE = "grader_failure"
STATE_PARSE_FAILURE = "prediction_parse_failure"

DEFAULT_SAMPLES_DIR = (
    Path(__file__).resolve().parents[2] / "benchmarks" / "semantic_groundedness"
)


def resolve_openjudge_dir(
    explicit: Path | str | None = None,
    environ: dict[str, str] | None = None,
) -> Path | None:
    environ = os.environ if environ is None else environ
    if explicit:
        candidate = Path(explicit)
        return candidate.resolve() if candidate.is_dir() else None
    if environ.get("OPENJUDGE_DIR"):
        candidate = Path(environ["OPENJUDGE_DIR"])
        return candidate.resolve() if candidate.is_dir() else None
    for base in (Path.cwd() / ".targets", Path.home() / "Developer" / "GitHub"):
        for name in ("openjudge", "OpenJudge"):
            candidate = base / name
            if candidate.is_dir():
                return candidate.resolve()
    return None


def _git(path: Path, *args: str) -> tuple[str | None, str]:
    completed = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        return None, " ".join(completed.stderr.split())[:200]
    return completed.stdout.strip(), ""


def _checkout_state(path: Path | None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "repository": OPENJUDGE_REPOSITORY,
        "commit": OPENJUDGE_COMMIT,
        "actual_commit": None,
        "commit_verified": False,
        "status": ERROR,
        "error": None,
    }
    if path is None:
        state["error"] = "未找到 OpenJudge checkout（可用 OPENJUDGE_DIR 指定）"
        state["state"] = STATE_UNAVAILABLE
        return state
    actual, error = _git(path, "rev-parse", "HEAD")
    state["actual_commit"] = actual
    if actual is None:
        state["error"] = f"无法读取 OpenJudge 提交：{error}"
        state["state"] = STATE_UNAVAILABLE
        return state
    if actual != OPENJUDGE_COMMIT:
        state["error"] = f"OpenJudge 提交不匹配：期望 {OPENJUDGE_COMMIT}，实际 {actual}"
        state["state"] = STATE_REVISION_MISMATCH
        return state
    dirty, error = _git(path, "status", "--porcelain", "--untracked-files=no")
    if dirty is None:
        state["error"] = f"无法检查 OpenJudge 工作树：{error}"
        state["state"] = STATE_UNAVAILABLE
        return state
    if dirty:
        state["error"] = f"OpenJudge 工作树存在未提交改动：{dirty.splitlines()[0]}"
        state["state"] = STATE_DIRTY
        return state
    python = path / ".venv" / "bin" / "python"
    if not python.exists():
        state["error"] = "OpenJudge 独立 venv 缺失（先执行 uv venv 并安装 py-openjudge）"
        state["state"] = STATE_UNAVAILABLE
        return state
    state["status"] = PASS
    state["commit_verified"] = True
    state["state"] = None
    return state


def load_samples(
    samples_dir: Path | str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    directory = Path(DEFAULT_SAMPLES_DIR if samples_dir is None else samples_dir)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples_path = directory / manifest["samples_file"]
    actual = sha256_file(samples_path)
    if actual != manifest["samples_sha256"]:
        raise SamplesChanged(
            f"样本文件校验和不匹配：期望 {manifest['samples_sha256']}，实际 {actual}"
        )
    payload = json.loads(samples_path.read_text(encoding="utf-8"))
    if payload.get("version") != manifest.get("version"):
        raise SamplesChanged(
            f"样本版本不匹配：清单 {manifest.get('version')}，样本 {payload.get('version')}"
        )
    return manifest, payload["samples"], samples_path


class SamplesChanged(Exception):
    pass


def model_config(environ: dict[str, str] | None = None) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    api_key = environ.get("OPENJUDGE_API_KEY") or environ.get("LLM_API_KEY")
    model = environ.get("OPENJUDGE_MODEL") or environ.get("LLM_MODEL")
    base_url = environ.get("OPENJUDGE_BASE_URL") or environ.get("LLM_BASE_URL")
    missing = []
    if not api_key:
        missing.append("OPENJUDGE_API_KEY 或 LLM_API_KEY")
    if not model:
        missing.append("OPENJUDGE_MODEL 或 LLM_MODEL")
    return {
        "configured": bool(api_key and model),
        "model": model,
        "base_url_configured": bool(base_url),
        "missing": missing,
    }


def summarize(
    predictions: list[dict[str, Any]], samples: list[dict[str, Any]]
) -> dict[str, Any]:
    by_id = {item["id"]: item for item in predictions}
    wrong = []
    errors = []
    confusion: dict[str, dict[str, int]] = {}
    clear_total = 0
    clear_agree = 0
    preferred_total = 0
    preferred_agree = 0
    borderline_total = 0
    borderline_distribution: dict[str, int] = {"supported": 0, "unsupported": 0}
    for sample in samples:
        item = by_id.get(sample["id"])
        if item is None or item.get("predicted") is None:
            errors.append({"id": sample["id"], "error": (item or {}).get("error")})
            continue
        predicted = item["predicted"]
        confusion.setdefault(sample["label"], {}).setdefault(predicted, 0)
        confusion[sample["label"]][predicted] += 1
        preferred_total += 1
        match_preferred = predicted == sample["preferred_label"]
        if match_preferred:
            preferred_agree += 1
        if sample["label"] == "borderline":
            borderline_total += 1
            borderline_distribution[predicted] = (
                borderline_distribution.get(predicted, 0) + 1
            )
        else:
            clear_total += 1
            if predicted == sample["label"]:
                clear_agree += 1
        if not match_preferred:
            wrong.append(
                {
                    "id": sample["id"],
                    "label": sample["label"],
                    "boundary": sample["label"] == "borderline",
                    "preferred_label": sample["preferred_label"],
                    "predicted": predicted,
                    "reason": (item.get("reason") or "")[:400],
                }
            )
    return {
        "clear_total": clear_total,
        "clear_agreements": clear_agree,
        "clear_accuracy": round(clear_agree / clear_total, 4) if clear_total else None,
        "borderline_total": borderline_total,
        "borderline_prediction_distribution": borderline_distribution,
        "preferred_total": preferred_total,
        "preferred_agreements": preferred_agree,
        "preferred_agreement_rate": (
            round(preferred_agree / preferred_total, 4) if preferred_total else None
        ),
        "confusion": confusion,
        "misjudgments": [item for item in wrong if not item["boundary"]],
        "disagreements": wrong,
        "prediction_errors": errors,
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


def run_semantic_evaluation(
    *,
    split: str,
    out: Path | str,
    mode: str = "offline",
    command: str = "",
    openjudge_dir: Path | str | None = None,
    samples_dir: Path | str | None = None,
    timeout: float = 300.0,
    runner=subprocess.run,
    environ: dict[str, str] | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
) -> tuple[dict[str, Any], Path]:
    if split not in {"calibration", "holdout"}:
        raise ValueError(f"未知划分：{split}")
    if mode not in {"offline", "model"}:
        raise ValueError(f"未知模式：{mode}")
    environ = os.environ if environ is None else environ
    now = datetime.now(timezone.utc) if now is None else now
    run_id = uuid4().hex if run_id is None else run_id
    base = Path(out)
    base.mkdir(parents=True, exist_ok=True)
    run_dir = _run_dir(base, now, run_id)

    manifest: dict[str, Any] = {
        "format": "semantic-evaluation/1.0",
        "run_id": run_id,
        "created_at": now.isoformat(),
        "command": command,
        "split": split,
        "mode": mode,
        "status": ERROR,
        "state": None,
        "error": None,
        "execution_mode": "test-double" if mode == "offline" else "real-service",
        "hard_gate": False,
        "hard_gate_note": "语义评分独立展示；离线硬门禁仍为确定性断言",
        "grader": {
            "name": GRADER_NAME,
            "repository": OPENJUDGE_REPOSITORY,
            "commit": OPENJUDGE_COMMIT,
        },
        "model": {"configured": False, "name": None, "missing": []},
        "samples": {
            "version": None,
            "sha256": None,
            "split_policy": None,
            "label_source": None,
            "labels_human_reviewed": False,
            "total": 0,
            "in_split": 0,
        },
        "metrics": {},
        "artifacts": [],
        "notes": [],
    }

    samples_path: Path | None = None
    samples: list[dict[str, Any]] = []
    try:
        samples_manifest, all_samples, samples_path = load_samples(samples_dir)
    except SamplesChanged as exc:
        manifest["state"] = STATE_SAMPLES_CHANGED
        manifest["error"] = str(exc)
        manifest["status"] = ERROR
        return _finish(manifest, run_dir, samples_path, [])
    manifest["samples"].update(
        {
            "version": samples_manifest["version"],
            "sha256": samples_manifest["samples_sha256"],
            "split_policy": samples_manifest.get("split_policy"),
            "label_source": samples_manifest.get("label_source"),
            "labels_human_reviewed": bool(
                samples_manifest.get("labels_human_reviewed")
            ),
            "total": len(all_samples),
        }
    )
    samples = [item for item in all_samples if item["split"] == split]
    manifest["samples"]["in_split"] = len(samples)
    if not samples:
        manifest["state"] = STATE_SPLIT_UNKNOWN
        manifest["error"] = f"冻结样本中没有 {split} 划分"
        manifest["status"] = ERROR
        return _finish(manifest, run_dir, samples_path, [])

    if split == "holdout" and not manifest["samples"]["labels_human_reviewed"]:
        manifest["state"] = STATE_LABELS_UNREVIEWED
        manifest["error"] = (
            "留出集指标需要人审标签；当前标签来源为项目编写，尚未人工复核"
        )
        manifest["status"] = NOT_RUN
        manifest["notes"].append(
            "完成人审后把 manifest.json 的 labels_human_reviewed 设为 true 并冻结新版本"
        )
        return _finish(manifest, run_dir, samples_path, samples)

    if mode == "model":
        config = model_config(environ)
        manifest["model"] = config
        if not config["configured"]:
            manifest["state"] = STATE_MODEL_CONFIG
            manifest["error"] = "缺少模型配置：" + "、".join(config["missing"])
            manifest["status"] = NOT_RUN
            return _finish(manifest, run_dir, samples_path, samples)
    else:
        manifest["model"] = {
            "configured": False,
            "name": "lexical-overlap-test-double",
            "missing": [],
        }
        manifest["notes"].append(
            "离线模式使用词面覆盖测试替身，只验证适配器和计分逻辑，不代表语义质量"
        )

    checkout = _checkout_state(resolve_openjudge_dir(openjudge_dir, environ))
    manifest["grader"].update(checkout)
    if checkout["status"] != PASS:
        manifest["state"] = checkout["state"]
        manifest["error"] = checkout["error"]
        manifest["status"] = ERROR
        return _finish(manifest, run_dir, samples_path, samples)

    samples_json = (run_dir / "grading.samples.json").resolve()
    samples_json.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output_path = (run_dir / "grading.raw.json").resolve()
    openjudge_root = Path(openjudge_dir).resolve() if openjudge_dir else resolve_openjudge_dir(
        None, environ
    )
    python = openjudge_root / ".venv" / "bin" / "python"
    script = Path(__file__).resolve().parent / "_openjudge_grade.py"
    child_env = dict(os.environ)
    if mode == "offline":
        for key in ("OPENJUDGE_API_KEY", "LLM_API_KEY", "OPENJUDGE_BASE_URL", "LLM_BASE_URL"):
            child_env.pop(key, None)
    command_list = [str(python), str(script), mode, str(samples_json), str(output_path)]
    try:
        completed = runner(
            command_list, cwd=str(openjudge_root), capture_output=True, text=True,
            timeout=timeout, env=child_env, check=False,
        )
    except subprocess.TimeoutExpired:
        manifest["state"] = STATE_TIMEOUT
        manifest["error"] = f"OpenJudge 评分超时（>{timeout}s）"
        manifest["status"] = ERROR
        return _finish(manifest, run_dir, samples_path, samples)
    except OSError as exc:
        manifest["state"] = STATE_UNAVAILABLE
        manifest["error"] = f"无法启动 OpenJudge 评分：{exc}"
        manifest["status"] = ERROR
        return _finish(manifest, run_dir, samples_path, samples)

    manifest["grader"]["command_exit_code"] = completed.returncode
    manifest["grader"]["stdout_tail"] = (completed.stdout or "")[-500:]
    manifest["grader"]["stderr_tail"] = (completed.stderr or "")[-500:]
    if completed.returncode != 0:
        manifest["state"] = STATE_UPSTREAM_FAILURE
        manifest["error"] = f"OpenJudge 评分进程退出 {completed.returncode}"
        manifest["status"] = ERROR
        return _finish(manifest, run_dir, samples_path, samples)

    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        manifest["state"] = STATE_PARSE_FAILURE
        manifest["error"] = f"评分结果无法解析：{exc}"
        manifest["status"] = ERROR
        return _finish(manifest, run_dir, samples_path, samples)
    if payload.get("status") != PASS or not isinstance(payload.get("predictions"), list):
        manifest["state"] = STATE_PARSE_FAILURE
        manifest["error"] = f"评分结果状态异常：{payload.get('status')}"
        manifest["status"] = ERROR
        return _finish(manifest, run_dir, samples_path, samples)

    predictions = payload["predictions"]
    manifest["execution_mode"] = payload.get("execution_mode", manifest["execution_mode"])
    manifest["model"]["name"] = payload.get("model")
    metrics = summarize(predictions, samples)
    manifest["metrics"] = metrics
    manifest["status"] = ERROR if metrics["prediction_errors"] else PASS
    if metrics["prediction_errors"]:
        manifest["state"] = STATE_PARSE_FAILURE
        manifest["error"] = "部分样本评分失败"
    return _finish(manifest, run_dir, samples_path, samples, predictions)


def _finish(
    manifest: dict[str, Any],
    run_dir: Path,
    samples_path: Path | None,
    samples: list[dict[str, Any]],
    predictions: list[dict[str, Any] | None] | None = None,
) -> tuple[dict[str, Any], Path]:
    samples_file = run_dir / "samples.json"
    samples_file.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    predictions_file = run_dir / "predictions.json"
    predictions_file.write_text(
        json.dumps(predictions or [], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_path = run_dir / "manifest.json"
    manifest["artifacts"] = [
        _artifact(run_dir, samples_file),
        _artifact(run_dir, predictions_file),
    ]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    report_path = run_dir / "report.html"
    report_path.write_text(
        render_semantic_html(manifest, samples, predictions or []), encoding="utf-8"
    )
    manifest["artifacts"].append(_artifact(run_dir, manifest_path))
    manifest["artifacts"].append(_artifact(run_dir, report_path))
    return manifest, run_dir


def semantic_exit_code(status: str) -> int:
    return 0 if status == PASS else 2


def render_semantic_html(
    manifest: dict[str, Any],
    samples: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
) -> str:
    metrics = manifest.get("metrics") or {}
    by_id = {item["id"]: item for item in predictions}
    cards = [
        ("样本", manifest["samples"].get("in_split", 0)),
        ("清晰一致率", metrics.get("clear_accuracy")),
        ("偏好一致率", metrics.get("preferred_agreement_rate")),
        ("误判", len(metrics.get("misjudgments", []) or [])),
        ("错误", len(metrics.get("prediction_errors", []) or [])),
    ]
    card_html = "".join(
        f"<div class='card'><b>{_e(value)}</b><span>{_e(label)}</span></div>"
        for label, value in cards
    )
    row_html = []
    for sample in samples:
        item = by_id.get(sample["id"]) or {}
        row_html.append(
            "<tr>"
            f"<td>{_e(sample['id'])}</td>"
            f"<td>{_e(sample['label'])}</td>"
            f"<td>{_e(sample['preferred_label'])}</td>"
            f"<td>{_badge(item.get('predicted') or 'NOT_RUN')}</td>"
            f"<td>{_e(item.get('raw_score'))}</td>"
            f"<td>{_e(sample['diagnosis'])}</td>"
            f"<td>{_e(sample['observation'])}</td>"
            f"<td>{_e(item.get('reason') or item.get('error') or '')}</td>"
            "</tr>"
        )
    mis_html = []
    for item in metrics.get("misjudgments", []) or []:
        mis_html.append(
            "<tr>"
            f"<td>{_e(item['id'])}</td>"
            f"<td>{_e(item['label'])}</td>"
            f"<td>{_e(item['predicted'])}</td>"
            f"<td>{_e(item.get('reason'))}</td>"
            "</tr>"
        )
    disagreement_html = []
    for item in metrics.get("disagreements", []) or []:
        disagreement_html.append(
            "<tr>"
            f"<td>{_e(item['id'])}</td>"
            f"<td>{_e(item['label'])}</td>"
            f"<td>{_e(item['preferred_label'])}</td>"
            f"<td>{_e(item['predicted'])}</td>"
            f"<td>{'是' if item['boundary'] else '否'}</td>"
            f"<td>{_e(item.get('reason'))}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>语义评分 {_e(manifest['run_id'][:8])}</title><style>{_CSS}</style></head>
<body><main>
<h1>证据支持语义评分</h1>
<p class="muted">OpenJudge · {_e(manifest['grader'].get('name'))} · 独立展示，不参与离线硬门禁</p>
<div class="meta">
<div>状态：{_badge(manifest.get('status'))} {_e(manifest.get('state') or '')}</div>
<div>划分：{_e(manifest['split'])} ｜ 模式：{_e(manifest['mode'])} ｜ 执行方式：{_e(manifest['execution_mode'])}</div>
<div>样本版本：{_e(manifest['samples'].get('version'))} ｜ 校验和：{_e(manifest['samples'].get('sha256'))}</div>
<div>标签来源：{_e(manifest['samples'].get('label_source'))} ｜ 人审：{_e(manifest['samples'].get('labels_human_reviewed'))}</div>
<div>工具提交：{_e(manifest['grader'].get('actual_commit') or manifest['grader'].get('commit'))} ｜ 已验证：{_e(manifest['grader'].get('commit_verified'))}</div>
<div>模型：{_e(manifest['model'].get('name') or '未配置')}</div>
<div class="muted">{_e(manifest.get('hard_gate_note'))}</div>
{_e(manifest.get('error') or '')}
</div>
<h2>指标</h2><div class="cards">{card_html}</div>
<h2>误判（清晰样本）</h2>
<table><thead><tr><th>ID</th><th>标签</th><th>预测</th><th>说明</th></tr></thead>
<tbody>{''.join(mis_html) or '<tr><td colspan=4 class="muted">无</td></tr>'}</tbody></table>
<h2>分歧例（含边界）</h2>
<table><thead><tr><th>ID</th><th>标签</th><th>偏好标签</th><th>预测</th><th>边界</th><th>说明</th></tr></thead>
<tbody>{''.join(disagreement_html) or '<tr><td colspan=6 class="muted">无</td></tr>'}</tbody></table>
<h2>样本与预测</h2>
<table><thead><tr><th>ID</th><th>标签</th><th>偏好</th><th>预测</th><th>分数</th><th>诊断摘要</th><th>观察证据</th><th>评分说明</th></tr></thead>
<tbody>{''.join(row_html)}</tbody></table>
<h2>产物</h2><pre>{_json(manifest.get('artifacts'))}</pre>
</main></body></html>
"""
