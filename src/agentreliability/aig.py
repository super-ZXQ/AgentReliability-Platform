"""腾讯 A.I.G MCP 源码扫描适配器：白名单样例、有界轮询与统一发现。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from .html_report import _CSS, _badge, _e, _json
from .oracle import ERROR, NOT_RUN, PASS
from .report import sha256_file

AIG_REPOSITORY = "https://github.com/Tencent/AI-Infra-Guard"
AIG_COMMIT = "76146bf479b2fedb34f7acb7f536aa37b1f8ba74"
DEFAULT_SAMPLE = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "aig_samples"
    / "synthetic_mcp_server"
)
ALLOWED_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".txt"}

STATE_INSTANCE_MISSING = "aig_instance_missing"
STATE_MODEL_CONFIG = "aig_model_config_invalid"
STATE_UNREACHABLE = "aig_unreachable"
STATE_TIMEOUT = "aig_poll_timeout"
STATE_TASK_FAILED = "aig_task_failed"
STATE_SERVICE_FAILURE = "aig_service_failure"
STATE_PARSE_FAILURE = "aig_parse_failure"
STATE_SAMPLE_INVALID = "aig_sample_invalid"

Transport = Callable[[str, str, bytes | None, dict[str, str], float], tuple[int, bytes, dict[str, str]]]


def _default_transport(
    method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: float
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read(), dict(response.headers)


class AigError(Exception):
    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


def build_source_zip(sample_dir: Path | str, target: Path) -> dict[str, Any]:
    sample_dir = Path(sample_dir)
    if not sample_dir.is_dir():
        raise AigError(STATE_SAMPLE_INVALID, f"白名单样例目录不存在：{sample_dir.name}")
    sample_root = sample_dir.resolve()
    included: list[str] = []
    skipped: list[str] = []
    rejected: list[dict[str, str]] = []
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(sample_dir.rglob("*")):
            if path.is_symlink():
                # 符号链接可能指向目录外文件，一律拒绝入包。
                relative = str(path.relative_to(sample_dir))
                rejected.append({"path": relative, "reason": "symlink"})
                skipped.append(relative)
                continue
            if not path.is_file():
                continue
            relative = path.relative_to(sample_dir)
            if any(part.startswith(".") for part in relative.parts) or path.suffix.lower() not in ALLOWED_SUFFIXES:
                skipped.append(str(relative))
                continue
            try:
                real = path.resolve(strict=True)
            except OSError:
                rejected.append({"path": str(relative), "reason": "unresolvable"})
                skipped.append(str(relative))
                continue
            if not real.is_relative_to(sample_root):
                rejected.append({"path": str(relative), "reason": "outside_root"})
                skipped.append(str(relative))
                continue
            archive.write(real, arcname=str(relative))
            included.append(str(relative))
    if not included:
        raise AigError(STATE_SAMPLE_INVALID, "白名单样例没有可上传的源码文件")
    return {
        "sample_dir": sample_dir.name,
        "archive": target.name,
        "sha256": sha256_file(target),
        "files": included,
        "skipped_files": skipped,
        "rejected_files": rejected,
        "allowed_suffixes": sorted(ALLOWED_SUFFIXES),
    }


def _redact_response(value: Any, token: str) -> Any:
    if isinstance(value, str):
        return value.replace(token, "[redacted]")
    if isinstance(value, list):
        return [_redact_response(item, token) for item in value]
    if isinstance(value, dict):
        return {
            key.replace(token, "[redacted]"): _redact_response(item, token)
            for key, item in value.items()
        }
    return value


class AigClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: Transport | None = None,
        poll_interval: float = 2.0,
        max_poll_seconds: float = 600.0,
        max_poll_attempts: int = 300,
        redact_token: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport or _default_transport
        self.poll_interval = poll_interval
        self.max_poll_seconds = max_poll_seconds
        self.max_poll_attempts = max_poll_attempts
        self.redact_token = redact_token

    def _call(self, method: str, path: str, body: bytes | None, headers: dict[str, str]) -> dict:
        try:
            status, payload, _headers = self.transport(
                method, self.base_url + path, body, headers, self.timeout
            )
        except urllib.error.HTTPError as exc:
            raise AigError(STATE_SERVICE_FAILURE, f"A.I.G 返回 HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise AigError(STATE_UNREACHABLE, f"无法连接 A.I.G：{exc.reason}") from exc
        except TimeoutError as exc:
            raise AigError(STATE_SERVICE_FAILURE, f"A.I.G 请求超时：{exc}") from exc
        except OSError as exc:
            raise AigError(STATE_UNREACHABLE, f"无法连接 A.I.G：{exc}") from exc
        if status >= 400:
            raise AigError(STATE_SERVICE_FAILURE, f"A.I.G 返回 HTTP {status}")
        try:
            envelope = json.loads(payload.decode("utf-8"))
            if self.redact_token:
                envelope = _redact_response(envelope, self.redact_token)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AigError(STATE_PARSE_FAILURE, f"A.I.G 响应无法解析：{exc}") from exc
        if not isinstance(envelope, dict) or "status" not in envelope:
            raise AigError(STATE_PARSE_FAILURE, "A.I.G 响应缺少统一状态字段")
        if envelope["status"] != 0:
            raise AigError(
                STATE_TASK_FAILED,
                f"A.I.G 任务失败：{envelope.get('message') or '未提供原因'}",
            )
        return envelope.get("data") or {}

    def upload(self, archive: Path) -> dict:
        boundary = "----agentreliability" + uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{archive.name}"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode() + archive.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        return self._call("POST", "/api/v1/app/taskapi/upload", body, headers)

    def create_mcp_scan(
        self,
        attachments: str,
        *,
        prompt: str,
        thread: int,
        language: str,
        model: dict[str, str] | None = None,
    ) -> dict:
        content: dict[str, Any] = {
            "prompt": prompt,
            "thread": thread,
            "language": language,
            "attachments": attachments,
        }
        if model:
            content["model"] = model
        body = json.dumps({"type": "mcp_scan", "content": content}).encode("utf-8")
        return self._call("POST", "/api/v1/app/taskapi/tasks", body, {"Content-Type": "application/json"})

    def poll(self, session_id: str, *, sleep=time.sleep) -> dict[str, Any]:
        started = time.monotonic()
        attempts = 0
        last: dict[str, Any] = {}
        while attempts < self.max_poll_attempts:
            attempts += 1
            last = self._call("GET", f"/api/v1/app/taskapi/status/{session_id}", None, {})
            state = last.get("status")
            if state in {"completed", "done"}:
                return {"status": state, "attempts": attempts, "last": last}
            if state in {"failed", "error", "terminated"}:
                raise AigError(
                    STATE_TASK_FAILED,
                    f"A.I.G 扫描任务失败：{last.get('log') or '未提供日志'}",
                )
            if state not in {"pending", "running", "todo", "doing"}:
                raise AigError(STATE_PARSE_FAILURE, f"未知任务状态：{state}")
            if time.monotonic() - started >= self.max_poll_seconds:
                break
            sleep(self.poll_interval)
        raise AigError(
            STATE_TIMEOUT,
            f"A.I.G 轮询超过上限（{attempts} 次 / {self.max_poll_seconds}s）",
        )

    def result(self, session_id: str) -> dict:
        return self._call("GET", f"/api/v1/app/taskapi/result/{session_id}", None, {})


_TITLE_KEYS = ("title", "name", "vulnerability", "risk", "issue", "summary")
_SEVERITY_KEYS = ("severity", "level", "risk_level", "riskLevel", "risk")
_DESCRIPTION_KEYS = ("description", "detail", "details", "message", "impact")
_SUGGESTION_KEYS = ("suggestion", "solution", "recommendation", "fix", "remediation")
_LOCATION_KEYS = ("location", "file", "path", "endpoint", "line")


def _first(node: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if node.get(key):
            return node[key]
    return default


def normalize_findings(data: Any) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    def add(node: dict) -> None:
        title = _first(node, _TITLE_KEYS)
        severity = _first(node, _SEVERITY_KEYS, "unknown")
        if not isinstance(title, str) or not title.strip():
            return
        item = {
            "title": title.strip()[:300],
            "severity": str(severity)[:40],
            "description": str(_first(node, _DESCRIPTION_KEYS, ""))[:800],
            "suggestion": str(_first(node, _SUGGESTION_KEYS, ""))[:800],
            "location": str(_first(node, _LOCATION_KEYS, ""))[:300],
        }
        key = (item["title"], item["severity"], item["location"])
        if key not in seen:
            seen.add(key)
            findings.append(item)

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            if _first(node, _TITLE_KEYS) and _first(node, _SEVERITY_KEYS):
                add(node)
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(data)
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return {
        "findings": findings,
        "total": len(findings),
        "severity_counts": counts,
        "normalization": "recognized" if findings else "unrecognized",
        "raw_keys": sorted(data.keys()) if isinstance(data, dict) else [],
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


def run_aig_scan(
    *,
    out: Path | str,
    base_url: str | None = None,
    command: str = "",
    sample_dir: Path | str | None = None,
    timeout: float = 30.0,
    poll_interval: float = 2.0,
    max_poll_seconds: float = 600.0,
    max_poll_attempts: int = 300,
    transport: Transport | None = None,
    environ: dict[str, str] | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
) -> tuple[dict[str, Any], Path]:
    environ = os.environ if environ is None else environ
    base_url = base_url or environ.get("AIG_BASE_URL")
    now = datetime.now(timezone.utc) if now is None else now
    run_id = uuid4().hex if run_id is None else run_id
    base = Path(out)
    base.mkdir(parents=True, exist_ok=True)
    run_dir = _run_dir(base, now, run_id)
    manifest: dict[str, Any] = {
        "format": "aig-mcp-scan/1.0",
        "run_id": run_id,
        "created_at": now.isoformat(),
        "command": command,
        "status": ERROR,
        "state": None,
        "error": None,
        "execution_mode": "test-double" if transport is not None else "real-service",
        "tool": {
            "name": "Tencent A.I.G",
            "repository": AIG_REPOSITORY,
            "commit": AIG_COMMIT,
            "base_url": base_url,
        },
        "sample": {},
        "task": {},
        "polling": {},
        "findings": {},
        "artifacts": [],
        "notes": [],
    }
    sample_dir = Path(DEFAULT_SAMPLE if sample_dir is None else sample_dir)
    archive = run_dir / "source.zip"
    try:
        sample = build_source_zip(sample_dir, archive)
    except AigError as exc:
        manifest["state"] = exc.state
        manifest["error"] = str(exc)
        return _finish(manifest, run_dir)
    manifest["sample"] = sample
    if not base_url:
        manifest["status"] = NOT_RUN
        manifest["state"] = STATE_INSTANCE_MISSING
        manifest["error"] = "缺少本地 A.I.G 实例地址（可用 AIG_BASE_URL 指定）"
        manifest["notes"].append("实例就绪后运行：AIG_BASE_URL=http://localhost:8088 uv run --locked agentreliability aig --out <dir>")
        return _finish(manifest, run_dir)

    model = {
        "model": environ.get("AIG_MODEL", "").strip(),
        "base_url": environ.get("AIG_MODEL_BASE_URL", "").strip(),
        "token": environ.get("AIG_MODEL_TOKEN", "").strip(),
    }
    if any(model.values()) and (
        not all(model.values())
        or urlsplit(base_url).hostname not in {"127.0.0.1", "::1", "localhost"}
    ):
        manifest["state"] = STATE_MODEL_CONFIG
        manifest["error"] = "显式模型需同时配置 AIG_MODEL、AIG_MODEL_BASE_URL、AIG_MODEL_TOKEN，并使用本地 A.I.G 实例"
        return _finish(manifest, run_dir)

    client = AigClient(
        base_url,
        timeout=timeout,
        transport=transport,
        poll_interval=poll_interval,
        max_poll_seconds=max_poll_seconds,
        max_poll_attempts=max_poll_attempts,
        redact_token=model["token"],
    )
    try:
        upload = client.upload(archive)
        manifest["task"]["upload"] = {
            key: upload.get(key) for key in ("fileUrl", "filename", "size")
        }
        (run_dir / "upload.json").write_text(
            json.dumps(upload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        file_url = upload.get("fileUrl")
        if not file_url:
            raise AigError(STATE_PARSE_FAILURE, "上传响应缺少 fileUrl")
        created = client.create_mcp_scan(
            str(file_url),
            prompt="扫描此 MCP 服务器源码中的安全风险",
            thread=4,
            language="zh",
            model=model if all(model.values()) else None,
        )
        session_id = created.get("session_id")
        if not session_id:
            raise AigError(STATE_PARSE_FAILURE, "创建任务响应缺少 session_id")
        manifest["task"]["session_id"] = session_id
        manifest["task"]["type"] = "mcp_scan"
        (run_dir / "task.json").write_text(
            json.dumps(created, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        polled = client.poll(session_id)
        manifest["polling"] = {
            "attempts": polled["attempts"],
            "final_status": polled["status"],
            "max_poll_seconds": max_poll_seconds,
            "interval_seconds": poll_interval,
        }
        (run_dir / "status.json").write_text(
            json.dumps(polled, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        raw = client.result(session_id)
        (run_dir / "result.raw.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        normalized = normalize_findings(raw)
        manifest["findings"] = normalized
        (run_dir / "findings.json").write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        manifest["status"] = PASS
    except AigError as exc:
        manifest["state"] = exc.state
        manifest["error"] = str(exc)
        manifest["status"] = NOT_RUN if exc.state == STATE_UNREACHABLE else ERROR
        if exc.state == STATE_UNREACHABLE:
            manifest["notes"].append("本地 A.I.G 实例不可达；未生成任何扫描结论")
    return _finish(manifest, run_dir)


def _finish(manifest: dict[str, Any], run_dir: Path) -> tuple[dict[str, Any], Path]:
    manifest_path = run_dir / "manifest.json"
    manifest["artifacts"] = [
        _artifact(run_dir, path)
        for path in sorted(run_dir.glob("*.json"))
        if path.name != "manifest.json"
    ]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    report_path = run_dir / "report.html"
    report_path.write_text(render_aig_html(manifest), encoding="utf-8")
    manifest["artifacts"] += [
        _artifact(run_dir, manifest_path),
        _artifact(run_dir, report_path),
    ]
    return manifest, run_dir


def aig_exit_code(status: str) -> int:
    return 0 if status == PASS else 2


def render_aig_html(manifest: dict[str, Any]) -> str:
    findings = manifest.get("findings") or {}
    rows = "".join(
        "<tr>"
        f"<td>{_e(item['title'])}</td>"
        f"<td>{_e(item['severity'])}</td>"
        f"<td>{_e(item['location'])}</td>"
        f"<td>{_e(item['description'])}</td>"
        f"<td>{_e(item['suggestion'])}</td>"
        "</tr>"
        for item in findings.get("findings", [])
    )
    cards = [
        ("状态", manifest.get("status")),
        ("发现", findings.get("total", 0)),
        ("轮询次数", (manifest.get("polling") or {}).get("attempts", 0)),
        ("任务状态", (manifest.get("polling") or {}).get("final_status")),
    ]
    card_html = "".join(
        f"<div class='card'><b>{_e(value)}</b><span>{_e(label)}</span></div>"
        for label, value in cards
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>A.I.G MCP 扫描 {_e(manifest['run_id'][:8])}</title><style>{_CSS}</style></head>
<body><main>
<h1>腾讯 A.I.G · MCP 源码扫描</h1>
<p class="muted">白名单合成样例 · 有界轮询 · 统一发现展示</p>
<div class="meta">
<div>状态：{_badge(manifest.get('status'))} {_e(manifest.get('state') or '')}</div>
<div>执行方式：{_e(manifest['execution_mode'])} ｜ 实例：{_e(manifest['tool'].get('base_url') or '未配置')}</div>
<div>会话：{_e(manifest['task'].get('session_id') or '无')}</div>
<div>样例：{_e(manifest['sample'].get('archive'))} ｜ 文件：{_e(_json(manifest['sample'].get('files')))}</div>
<div>跳过：{_e(_json(manifest['sample'].get('skipped_files')))}</div>
{_e(manifest.get('error') or '')}
</div>
<h2>概览</h2><div class="cards">{card_html}</div>
<h2>统一发现</h2>
<table><thead><tr><th>标题</th><th>等级</th><th>位置</th><th>描述</th><th>建议</th></tr></thead>
<tbody>{rows or '<tr><td colspan=5 class="muted">无发现或尚未扫描</td></tr>'}</tbody></table>
<h2>任务与轮询</h2><pre>{_json({'task': manifest.get('task'), 'polling': manifest.get('polling')})}</pre>
<h2>产物</h2><pre>{_json(manifest.get('artifacts'))}</pre>
</main></body></html>
"""
