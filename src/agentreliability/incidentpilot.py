"""IncidentPilot 单场景调查适配器。

上游 checkout 保持只读，脚本在其虚拟环境中以独立进程运行；
模型与工具是上游脚本化替身，Docker 复测是真实容器。任何异常路径
都返回显式状态，不会把未执行的结果写成通过。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .oracle import ERROR, PASS

INCIDENTPILOT_REPOSITORY = "https://github.com/super-ZXQ/incidentpilot.git"
INCIDENTPILOT_COMMIT = "566639ea876eed171c705ad0c1a0d79276b8584b"
INCIDENTPILOT_ENTRYPOINT = "incidentpilot.agent.graph.run_agent_workflow"
SCENARIO_ID = "fault_002_slow_database_query"

STATE_UNAVAILABLE = "incidentpilot_unavailable"
STATE_REVISION_MISMATCH = "revision_mismatch"
STATE_SOURCE_MISSING = "source_missing"
STATE_TIMEOUT = "timeout"
STATE_UPSTREAM_FAILURE = "upstream_exit_failure"
STATE_MISSING_ARTIFACTS = "missing_artifacts"
STATE_PARSE_FAILURE = "response_parse_failure"


@dataclass
class IncidentReport:
    status: str
    state: str | None = None
    error: str | None = None
    payload: dict[str, Any] | None = None
    command: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    tool: dict[str, Any] = field(default_factory=dict)
    execution_mode: str = "offline-scripted"


def resolve_incidentpilot_checkout(
    explicit: Path | str | None = None,
    environ: dict[str, str] | None = None,
) -> Path | None:
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


def _clean(value: str | None, limit: int = 300) -> str:
    if not value:
        return ""
    home = str(Path.home())
    text = " ".join(str(value).split())
    if home:
        text = text.replace(home, "~")
    return text[:limit]


def _git(path: Path, *args: str) -> tuple[str | None, str]:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None, _clean(completed.stderr, 160)
    return completed.stdout.strip(), ""


def _tool(commit: str | None, run_id: str | None = None) -> dict[str, Any]:
    return {
        "name": "IncidentPilot",
        "repository": INCIDENTPILOT_REPOSITORY,
        "locked_commit": INCIDENTPILOT_COMMIT,
        "actual_commit": commit,
        "commit_verified": commit == INCIDENTPILOT_COMMIT,
        "entrypoint": INCIDENTPILOT_ENTRYPOINT,
        "sandbox": "incidentpilot-sandbox:py312",
        "external_run_id": run_id,
        "external_reference": f"{INCIDENTPILOT_COMMIT}:{INCIDENTPILOT_ENTRYPOINT}",
    }


def run_incidentpilot_investigation(
    *,
    incidentpilot_dir: Path | str | None,
    out_raw: Path,
    timeout: float = 600.0,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    environ: dict[str, str] | None = None,
) -> IncidentReport:
    root = resolve_incidentpilot_checkout(incidentpilot_dir, environ)
    if root is None:
        return IncidentReport(
            status=ERROR,
            state=STATE_UNAVAILABLE,
            error="未找到 IncidentPilot checkout（可用 INCIDENTPILOT_DIR 指定）",
            tool=_tool(None),
        )
    commit, git_error = _git(root, "rev-parse", "HEAD")
    if commit != INCIDENTPILOT_COMMIT:
        return IncidentReport(
            status=ERROR,
            state=STATE_REVISION_MISMATCH,
            error=(
                f"IncidentPilot 提交不匹配：期望 {INCIDENTPILOT_COMMIT}，"
                f"实际 {commit or git_error or '无法读取'}"
            ),
            tool=_tool(commit),
        )
    dirty, dirty_error = _git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty is None or dirty:
        return IncidentReport(
            status=ERROR,
            state=STATE_SOURCE_MISSING,
            error=f"IncidentPilot 工作树不可用：{dirty or dirty_error}",
            tool=_tool(commit),
        )

    reference_dir = root / "reference" / "orders_api"
    if not reference_dir.is_dir():
        return IncidentReport(
            status=ERROR,
            state=STATE_SOURCE_MISSING,
            error="场景源码目录 reference/orders_api 不存在",
            tool=_tool(commit),
        )
    python = root / ".venv" / "bin" / "python"
    if not python.exists():
        return IncidentReport(
            status=ERROR,
            state=STATE_UNAVAILABLE,
            error="IncidentPilot 虚拟环境解释器不存在",
            tool=_tool(commit),
        )

    runner_script = Path(__file__).with_name("_incidentpilot_run.py")
    out_raw = Path(out_raw).resolve()
    out_raw.parent.mkdir(parents=True, exist_ok=True)
    command = [str(python), str(runner_script), str(root), str(out_raw)]
    env = dict(os.environ)
    for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_ENABLED"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(root / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    env["GITHUB_INTEGRATION_ENABLED"] = "false"
    execution_mode = "offline-scripted" if runner is subprocess.run else "test-double"

    try:
        completed = runner(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return IncidentReport(
            status=ERROR,
            state=STATE_TIMEOUT,
            error=f"IncidentPilot 调查超时（{timeout:g} 秒）",
            command=command,
            stdout_tail=_clean(getattr(exc, "stdout", "")),
            stderr_tail=_clean(getattr(exc, "stderr", "")),
            tool=_tool(commit),
            execution_mode=execution_mode,
        )

    if not out_raw.exists():
        return IncidentReport(
            status=ERROR,
            state=STATE_MISSING_ARTIFACTS,
            error="IncidentPilot 未写出调查产物",
            command=command,
            stdout_tail=_clean(completed.stdout),
            stderr_tail=_clean(completed.stderr),
            tool=_tool(commit),
            execution_mode=execution_mode,
        )
    try:
        payload = json.loads(out_raw.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return IncidentReport(
            status=ERROR,
            state=STATE_PARSE_FAILURE,
            error=f"调查产物无法解析：{type(exc).__name__}",
            command=command,
            stdout_tail=_clean(completed.stdout),
            stderr_tail=_clean(completed.stderr),
            tool=_tool(commit),
            execution_mode=execution_mode,
        )
    if not isinstance(payload, dict):
        return IncidentReport(
            status=ERROR,
            state=STATE_PARSE_FAILURE,
            error="调查产物不是对象",
            command=command,
            stdout_tail=_clean(completed.stdout),
            stderr_tail=_clean(completed.stderr),
            tool=_tool(commit),
            execution_mode=execution_mode,
        )

    investigation = payload.get("investigation") or {}
    run_id = investigation.get("run_id")
    if payload.get("status") != PASS or not run_id:
        error = _clean(payload.get("error")) or f"上游流程状态 {payload.get('status')}"
        return IncidentReport(
            status=ERROR,
            state=STATE_UPSTREAM_FAILURE,
            error=error,
            payload=payload,
            command=command,
            stdout_tail=_clean(completed.stdout),
            stderr_tail=_clean(completed.stderr),
            tool=_tool(commit, run_id),
            execution_mode=execution_mode,
        )

    return IncidentReport(
        status=PASS,
        payload=payload,
        command=command,
        stdout_tail=_clean(completed.stdout),
        stderr_tail=_clean(completed.stderr),
        tool=_tool(commit, run_id),
        execution_mode=execution_mode,
    )
