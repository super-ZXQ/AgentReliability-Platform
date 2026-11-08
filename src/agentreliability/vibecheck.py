"""VibeCheck-AI 静态扫描适配器。

上游以固定提交锁定，扫描在独立进程中执行，平台只读取白名单字段。
任何缺失、版本错配、超时或上游失败都返回明确状态，不会降级为通过。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .oracle import ERROR, NOT_RUN, PASS

VIBECHECK_AI_REPOSITORY = "https://github.com/super-ZXQ/vibecheck-ai.git"
VIBECHECK_AI_COMMIT = "3a0e22c7e49eb317cbecb80c4d7616668938300e"
VIBECHECK_AI_ENTRYPOINT = "app.scanner.sensitive.scan_directory"

STATE_UNAVAILABLE = "vibecheck_unavailable"
STATE_REVISION_MISMATCH = "revision_mismatch"
STATE_SOURCE_MISSING = "source_missing"
STATE_TIMEOUT = "timeout"
STATE_UPSTREAM_FAILURE = "upstream_exit_failure"
STATE_MISSING_ARTIFACTS = "missing_artifacts"
STATE_PARSE_FAILURE = "response_parse_failure"
STATE_EMPTY_SCOPE = "empty_scan_scope"

_FINDING_FIELDS = (
    "rule_id",
    "rule_name",
    "severity",
    "confidence",
    "dimension",
    "file_path",
    "line_start",
    "line_end",
    "column_start",
    "column_end",
    "snippet_masked",
    "is_blocking",
    "finding_type",
    "category",
    "secret_type",
)


@dataclass
class ScanReport:
    status: str
    state: str | None = None
    error: str | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    scope: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None
    command: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_ms: int = 0
    tool: dict[str, Any] = field(default_factory=dict)
    execution_mode: str = "real-service"


def resolve_vibecheck_dir(
    explicit: Path | str | None = None,
    environ: dict[str, str] | None = None,
) -> Path | None:
    environ = os.environ if environ is None else environ
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if environ.get("VIBECHECK_AI_DIR"):
        candidates.append(Path(environ["VIBECHECK_AI_DIR"]))
    for base in (Path.cwd() / ".targets", Path.home() / "Developer" / "GitHub"):
        candidates.append(base / "vibecheck-ai")
        candidates.append(base / "VibeCheck-AI")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return None


def _clean_text(value: str | None, limit: int = 300) -> str:
    if not value:
        return ""
    text = " ".join(str(value).split())
    home = str(Path.home())
    if home and home in text:
        text = text.replace(home, "~")
    return text[:limit]


def _git_revision(path: Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _tool_metadata(commit: str | None, verified: bool) -> dict[str, Any]:
    return {
        "name": "VibeCheck-AI",
        "repository": VIBECHECK_AI_REPOSITORY,
        "locked_commit": VIBECHECK_AI_COMMIT,
        "actual_commit": commit,
        "commit_verified": verified,
        "entrypoint": VIBECHECK_AI_ENTRYPOINT,
        "external_run_id": None,
        "external_reference": f"{VIBECHECK_AI_COMMIT}:{VIBECHECK_AI_ENTRYPOINT}",
    }


def _normalize_finding(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    normalized: dict[str, Any] = {}
    for key in _FINDING_FIELDS:
        normalized[key] = item.get(key)
    for key in ("line_start", "line_end", "column_start", "column_end"):
        if normalized[key] is not None and not isinstance(normalized[key], int):
            return None
    if not isinstance(normalized.get("rule_id"), str):
        return None
    if not isinstance(normalized.get("snippet_masked"), str):
        return None
    return normalized


def _summarize_counts(findings: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    by_severity: dict[str, int] = {}
    by_dimension: dict[str, int] = {}
    for finding in findings:
        severity = str(finding.get("severity") or "unknown")
        dimension = str(finding.get("dimension") or "unknown")
        by_severity[severity] = by_severity.get(severity, 0) + 1
        by_dimension[dimension] = by_dimension.get(dimension, 0) + 1
    return dict(sorted(by_severity.items())), dict(sorted(by_dimension.items()))


def run_vibecheck_scan(
    *,
    vibecheck_dir: Path | str | None,
    scan_root: Path,
    out_raw: Path,
    source_label: str,
    timeout: float = 180.0,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    environ: dict[str, str] | None = None,
) -> ScanReport:
    """调用真实扫描器；所有异常路径都映射为显式状态。"""
    root = resolve_vibecheck_dir(vibecheck_dir, environ)
    if root is None:
        return ScanReport(
            status=NOT_RUN,
            state=STATE_UNAVAILABLE,
            error="未找到 VibeCheck-AI checkout（可用 VIBECHECK_AI_DIR 指定）",
            tool=_tool_metadata(None, False),
        )

    commit = _git_revision(root)
    tool = _tool_metadata(commit, commit == VIBECHECK_AI_COMMIT)
    if commit != VIBECHECK_AI_COMMIT:
        return ScanReport(
            status=ERROR,
            state=STATE_REVISION_MISMATCH,
            error=(
                f"VibeCheck-AI 提交不匹配：期望 {VIBECHECK_AI_COMMIT}，"
                f"实际 {commit or '无法读取'}"
            ),
            tool=tool,
        )

    backend = root / "backend"
    python = backend / ".venv" / "bin" / "python"
    if not python.exists():
        return ScanReport(
            status=NOT_RUN,
            state=STATE_UNAVAILABLE,
            error="VibeCheck-AI 后端虚拟环境解释器不存在",
            tool=tool,
        )

    scan_root = Path(scan_root).resolve()
    if not scan_root.is_dir():
        return ScanReport(
            status=ERROR,
            state=STATE_SOURCE_MISSING,
            error=f"扫描源码目录不存在：{source_label}",
            tool=tool,
        )

    runner_script = Path(__file__).with_name("_vibecheck_scan.py")
    out_raw = Path(out_raw).resolve()
    command = [
        str(python),
        str(runner_script),
        str(scan_root),
        str(out_raw),
    ]
    env = dict(os.environ) if environ is None else dict(environ)
    env["PYTHONPATH"] = str(backend)
    env["PYTHONIOENCODING"] = "utf-8"
    execution_mode = "real-service" if runner is subprocess.run else "test-double"

    try:
        completed = runner(
            command,
            cwd=str(backend),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return ScanReport(
            status=ERROR,
            state=STATE_TIMEOUT,
            error=f"VibeCheck-AI 扫描超时（{timeout:g} 秒）",
            command=command,
            stdout_tail=_clean_text(getattr(exc, "stdout", "")),
            stderr_tail=_clean_text(getattr(exc, "stderr", "")),
            tool=tool,
            execution_mode=execution_mode,
        )

    stdout_tail = _clean_text(getattr(completed, "stdout", ""))
    stderr_tail = _clean_text(getattr(completed, "stderr", ""))
    if completed.returncode != 0:
        return ScanReport(
            status=ERROR,
            state=STATE_UPSTREAM_FAILURE,
            error=f"VibeCheck-AI 进程退出码 {completed.returncode}",
            command=command,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            tool=tool,
            execution_mode=execution_mode,
        )

    if not out_raw.exists():
        return ScanReport(
            status=ERROR,
            state=STATE_MISSING_ARTIFACTS,
            error="VibeCheck-AI 未写出扫描产物",
            command=command,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            tool=tool,
            execution_mode=execution_mode,
        )

    try:
        payload = json.loads(out_raw.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ScanReport(
            status=ERROR,
            state=STATE_PARSE_FAILURE,
            error=f"扫描产物无法解析：{type(exc).__name__}",
            command=command,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            tool=tool,
            execution_mode=execution_mode,
        )

    findings_raw = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(findings_raw, list):
        return ScanReport(
            status=ERROR,
            state=STATE_PARSE_FAILURE,
            error="扫描产物缺少 findings 列表",
            command=command,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            tool=tool,
            execution_mode=execution_mode,
        )

    findings: list[dict[str, Any]] = []
    for item in findings_raw:
        normalized = _normalize_finding(item)
        if normalized is None:
            return ScanReport(
                status=ERROR,
                state=STATE_PARSE_FAILURE,
                error="扫描产物包含无法识别的发现项",
                command=command,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                tool=tool,
                execution_mode=execution_mode,
            )
        findings.append(normalized)

    files_scanned = payload.get("total_files_scanned")
    lines_scanned = payload.get("total_lines_scanned")
    if not isinstance(files_scanned, int) or not isinstance(lines_scanned, int):
        return ScanReport(
            status=ERROR,
            state=STATE_PARSE_FAILURE,
            error="扫描产物缺少文件或行统计",
            command=command,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            tool=tool,
            execution_mode=execution_mode,
        )
    if files_scanned <= 0:
        return ScanReport(
            status=ERROR,
            state=STATE_EMPTY_SCOPE,
            error="扫描范围为 0 个文件",
            command=command,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            tool=tool,
            execution_mode=execution_mode,
        )

    by_severity, by_dimension = _summarize_counts(findings)
    skipped = payload.get("skipped_files") or []
    errors = payload.get("scan_errors") or []
    notices = payload.get("notices") or []
    scope = {
        "source": source_label,
        "files_scanned": files_scanned,
        "lines_scanned": lines_scanned,
        "findings_count": len(findings),
        "notices_count": len(notices),
        "skipped_files_count": len(skipped),
        "scan_errors_count": len(errors),
        "by_severity": by_severity,
        "by_dimension": by_dimension,
    }
    return ScanReport(
        status=PASS,
        findings=findings,
        scope=scope,
        raw=payload,
        command=command,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        tool=tool,
        execution_mode=execution_mode,
    )
