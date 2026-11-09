"""在 IncidentPilot 独立环境中运行 2×2 交叉实验。

脚本只使用标准库与 IncidentPilot 自身依赖。离线模式使用上游脚本化
模型与合成工具（tier=workflow_fake），真实模型配置不在本脚本内伪造。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import traceback
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from http.client import HTTPConnection
from urllib.parse import urlsplit
from pathlib import Path
from unittest.mock import patch


SLOW_QUERY_SCENARIO = "fault_002_slow_database_query"
SLOW_QUERY_ORACLE = "orders-app-delay/v1"
SLOW_QUERY_SOURCE_MARKERS = [
    'observe_db("select_orders", time.perf_counter() - db_started)',
    'if ftype in {"slow_database_query", "dependency_timeout", "connection_pool_exhaustion"}:',
    'time.sleep(float(params.get("delay_seconds", 0.5)))',
]


def calibrate_case(case, *, mode: str):
    """Match the scoring label to the behavior of the pinned reference app."""
    if mode != "model" or case.id != SLOW_QUERY_SCENARIO:
        return case, None
    truth = case.ground_truth.model_copy(update={
        "fault_category": "application_bug",
        "root_cause": "The orders handler sleeps after the SQL query has already completed.",
        "causal_facts": [
            "application handler sleeps after SQL execution",
            "the delay increases orders HTTP request latency",
        ],
        "injection_evidence": {"path": "app.py", "markers": SLOW_QUERY_SOURCE_MARKERS},
    })
    return case.model_copy(update={"ground_truth": truth}), SLOW_QUERY_ORACLE


def _trial_error(record: dict, budget_exceeded: dict, *, real: bool) -> str | None:
    if record.get("error"):
        return record["error"]
    if not record.get("run_id"):
        return "缺少调查运行编号"
    if real and any(budget_exceeded.values()):
        return "调查超出试验预算"
    if real and record.get("final_status") != "INVESTIGATION_COMPLETE":
        return f"调查未完成：{record.get('final_status') or '缺少终态'}"
    return None


def _tail(text: str, limit: int = 800) -> str:
    return (text or "")[-limit:]


def _model_prefix(spec: dict, model: dict) -> str:
    requirement = (spec.get("real_model_requirements") or {}).get(model["id"]) or []
    if requirement:
        first = str(requirement[0])
        for suffix in ("_API_KEY", "_MODEL", "_BASE_URL"):
            if first.endswith(suffix):
                return first[: -len(suffix)]
        return first
    return "PAWBENCH_MODEL_" + model["id"].split("-")[-1].upper()


def _slot_config(spec: dict, model: dict, environ: dict) -> dict:
    prefix = _model_prefix(spec, model)
    return {
        "prefix": prefix,
        "api_key": environ.get(f"{prefix}_API_KEY") or "",
        "model": environ.get(f"{prefix}_MODEL") or "",
        "base_url": environ.get(f"{prefix}_BASE_URL") or "",
    }


def _reference_api(environ: dict) -> str:
    return (
        environ.get("PAWBENCH_REFERENCE_ORDERS_API_URL")
        or environ.get("REFERENCE_ORDERS_API_URL")
        or "http://127.0.0.1:8001"
    )


class ReferenceResetError(RuntimeError):
    pass


def local_reference_endpoint(url: str):
    endpoint = urlsplit(url)
    if (
        endpoint.scheme != "http"
        or endpoint.hostname not in {"127.0.0.1", "::1"}
        or endpoint.username is not None
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("故障注入仅支持专用的本地 reference API（HTTP 回环地址）")
    return endpoint


def reset_reference(url: str) -> None:
    endpoint = local_reference_endpoint(url)
    connection = HTTPConnection(endpoint.hostname, endpoint.port, timeout=5)
    try:
        connection.request("POST", "/admin/fault/reset")
        response = connection.getresponse()
        if response.status != 200 or json.loads(response.read(4096)) != {"status": "reset"}:
            raise ValueError(f"reference API 未确认重置（HTTP {response.status}）")
    except Exception as exc:
        raise ReferenceResetError(f"无法确认 reference 故障已清除：{exc}") from exc
    finally:
        connection.close()


@contextmanager
def clean_reference(url: str):
    reset_reference(url)
    try:
        yield
    finally:
        reset_reference(url)


def reproduction_observation(
    events: list[dict], *, baseline_samples: int, faulted_samples: int
) -> dict | None:
    """Expose measured requests around the intervention without the injection payload."""
    baseline, observed = [], []
    samples = baseline
    intervention_seen = False
    for event in events:
        path, method = event.get("path"), event.get("method")
        if path == "/admin/fault" and method == "POST" and event.get("status") == 200:
            if intervention_seen:
                return None
            intervention_seen = True
            samples = observed
        elif path == "/orders" and method == "GET":
            samples.append({"status": event["status"], "latency_ms": event["latency_ms"]})
    if len(baseline) != baseline_samples or len(observed) != faulted_samples:
        return None
    return {
        "event": "controlled_reproduction",
        "source": "orders-api HTTP request telemetry",
        "path": "/orders",
        "baseline_requests": baseline,
        "intervention": {"method": "POST", "path": "/admin/fault", "status": 200},
        "post_intervention_requests": observed,
    }


def fixed_source_reader(call_tool, *, limit: int, audit: dict):
    """Expand the fixed source read while counting every MCP request."""
    async def call(adapter, name: str, arguments: dict) -> dict:
        async def request(args: dict) -> dict:
            if audit["mcp_call_count"] >= limit:
                raise RuntimeError("固定策略已用尽 MCP 调用预算")
            audit["mcp_call_count"] += 1
            return await call_tool(adapter, name, args)

        first = await request(arguments)
        if name != "read_source_code":
            return first
        pages = [first]
        while pages[-1]["has_more"] and len(pages) < 3 and audit["mcp_call_count"] < limit:
            previous = pages[-1]
            start = previous["end_line"] + 1
            page = await request({**arguments, "start_line": start})
            if (
                page["path"] != first["path"]
                or page["total_lines"] != first["total_lines"]
                or page["start_line"] != start
                or page["end_line"] < start
            ):
                raise ValueError("MCP 源码分页返回了不连续的内容")
            pages.append(page)
        ranges = [[page["start_line"], page["end_line"]] for page in pages]
        audit["source_reads"].append({"path": arguments["path"], "ranges": ranges})
        return {
            **first,
            "content": "".join(page["content"] for page in pages),
            "end_line": pages[-1]["end_line"],
            "has_more": pages[-1]["has_more"],
            "page_ranges": ranges,
        }

    return call


def trial_log_events(log_path: Path) -> list[dict]:
    events = []
    reset_seen = False
    with log_path.open("rb") as log:
        log.seek(max(0, log_path.stat().st_size - 128_000))
        for line in log:
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                if (
                    event.get("path") == "/admin/fault/reset"
                    and event.get("method") == "POST"
                    and event.get("status") == 200
                ):
                    reset_seen = True
                    events.clear()
                elif reset_seen:
                    events.append(event)
    return events


def source_citation_audit(calls: list[dict], evidence: list[dict], cited_ids: list[str], injection: dict) -> dict:
    markers = injection["markers"]
    expected_path = injection["path"]
    reads = []
    for call in calls:
        if call["tool_name"] != "read_source_code":
            continue
        output = call["output"]
        path = str(output.get("path", "")).replace("\\", "/")
        covers_injection = (
            call["status"] == "SUCCEEDED"
            and (path == expected_path or path.endswith("/" + expected_path))
            and all(marker in output.get("content", "") for marker in markers)
        )
        evidence_ids = [
            item["evidence_id"] for item in evidence if item["tool_call_id"] == call["tool_call_id"]
        ]
        reads.append({
            "tool_call_id": call["tool_call_id"],
            "status": call["status"],
            "path": Path(path).name or None,
            "requested_start_line": call["input"].get("start_line", 1),
            "start_line": output.get("start_line"),
            "end_line": output.get("end_line"),
            "evidence_ids": evidence_ids,
            "cited_evidence_ids": [item for item in evidence_ids if item in cited_ids],
            "covers_injection": covers_injection,
        })
    coverage = [read for read in reads if read["covers_injection"]]
    if any(read["cited_evidence_ids"] for read in coverage):
        verdict = "source_cited"
    elif coverage:
        verdict = "source_read_without_citation"
    elif any(read["status"] == "SUCCEEDED" for read in reads):
        verdict = "source_window_missed"
    elif reads:
        verdict = "source_read_failed"
    else:
        verdict = "source_not_read"
    return {"verdict": verdict, "reads": reads}


async def _load_diagnosis(run_id: str | None, injection: dict | None) -> tuple[dict, dict | None]:
    if not run_id:
        return {}, None
    from sqlalchemy import select

    from incidentpilot.agent.evidence import valid_evidence
    from incidentpilot.models.db import AgentRun, Evidence, ToolCall
    from incidentpilot.persistence.session import get_session_factory

    async with get_session_factory()() as session:
        run = (await session.execute(select(AgentRun).where(AgentRun.run_id == run_id))).scalars().first()
        if not run:
            return {}, None
        root_cause = dict(run.root_cause or {})
        if not injection:
            return root_cause, None
        call_rows = (await session.scalars(
            select(ToolCall).where(ToolCall.run_pk == run.id).order_by(ToolCall.created_at, ToolCall.id)
        )).all()
        evidence_rows = (await session.scalars(select(Evidence).where(Evidence.run_pk == run.id))).all()
        calls = [
            {"tool_call_id": row.tool_call_id, "tool_name": row.tool_name, "status": row.status,
             "input": row.input or {}, "output": row.output or {}}
            for row in call_rows
        ]
        evidence = [
            {"evidence_id": row.evidence_id, "run_id": run_id, "tool_call_id": row.tool_call_id,
             "hash": row.content_hash, "result": row.result}
            for row in evidence_rows
        ]
    cited_ids = root_cause.get("evidence_ids") or root_cause.get("supporting_evidence_ids") or []
    return root_cause, source_citation_audit(calls, valid_evidence(evidence, calls, run_id), cited_ids, injection)


async def run_trial(
    root: Path,
    spec: dict,
    model: dict,
    strategy: str,
    case,
    trial: int,
    *,
    mode: str,
    environ: dict,
) -> dict:
    from incidentpilot.config import Settings
    from incidentpilot.eval.harness import BASELINE_SAMPLES, FAULTED_SAMPLES, EvaluationHarness
    from incidentpilot.eval.scoring import CATEGORY_ALIASES
    from incidentpilot.persistence.session import dispose_engine, init_db
    from incidentpilot.tools.mcp_adapter import MCPReadonlyAdapter

    case, oracle_version = calibrate_case(case, mode=mode)
    slot = _slot_config(spec, model, environ)
    with tempfile.TemporaryDirectory(prefix="platform-matrix-") as raw_tmp:
        url = "sqlite+aiosqlite:///" + str(Path(raw_tmp) / "matrix.db")
        common = {
            "database_url": url,
            "environment": "test",
            "sandbox_enabled": False,
            "max_tool_calls": int((spec.get("budget") or {}).get("max_tool_calls", 40)),
            "reference_repo_path": str(root / "reference" / "orders_api"),
        }
        log_dir = environ.get("PAWBENCH_REFERENCE_LOGS_PATH")
        log_path = Path(log_dir) / "orders.jsonl" if log_dir else None
        if mode == "model":
            tier = "real_llm_investigation"
            settings = Settings(
                **common,
                llm_enabled=True,
                llm_provider="openai-compatible",
                llm_api_key=slot["api_key"],
                llm_model=slot["model"],
                llm_base_url=slot["base_url"],
                tool_backend="mcp",
                reference_orders_api_url=_reference_api(environ),
                reference_logs_path=(
                    raw_tmp if log_path else str(root / "reference" / "orders_api" / "var" / "logs")
                ),
                reference_db_url="",
            )
        else:
            tier = "workflow_fake"
            settings = Settings(
                **common,
                llm_enabled=False,
                llm_api_key="",
                tool_backend="fake",
            )
        observation = None

        class ObservedHarness(EvaluationHarness):
            async def _setup_and_reproduce(self, case):
                nonlocal observation
                await super()._setup_and_reproduce(case)
                if log_path is None:
                    return
                events = trial_log_events(log_path) if log_path.exists() else []
                observation = reproduction_observation(
                    events, baseline_samples=BASELINE_SAMPLES, faulted_samples=FAULTED_SAMPLES,
                )
                if observation is None:
                    raise RuntimeError("专用日志未捕获完整的当轮复现样本")
                snapshot = Path(self.settings.reference_logs_path) / "orders.jsonl"
                snapshot.write_text(
                    "".join(json.dumps(event) + "\n" for event in [*events, observation]),
                    encoding="utf-8",
                )

        await init_db(url)
        harness = ObservedHarness(
            root / "benchmarks" / "fault_cases",
            settings=settings,
            tier=tier,
            strategy=strategy,
        )
        audit = None
        source_reader = nullcontext()
        if mode == "model" and strategy == "fixed":
            audit = {"mcp_call_count": 0, "source_reads": []}
            source_reader = patch.object(
                MCPReadonlyAdapter,
                "call_tool",
                fixed_source_reader(MCPReadonlyAdapter.call_tool, limit=settings.max_tool_calls, audit=audit),
            )
        try:
            reference = clean_reference(_reference_api(environ)) if mode == "model" else nullcontext()
            with reference, source_reader:
                outcome = await harness.run_case(case)
                predicted_root_cause, citation_audit = await _load_diagnosis(
                    outcome.run_id, case.ground_truth.injection_evidence if oracle_version else None,
                )
        finally:
            await dispose_engine()
        record = asdict(outcome)
        ground_truth = case.ground_truth or {}
        expected_category = str(
            ground_truth.get("fault_category")
            or ground_truth.get("expected_fault_category")
            or ""
        )
        expected_component = str(ground_truth.get("affected_component") or "")
        predicted_category = str(predicted_root_cause.get("fault_category") or "")
        predicted_component = str(predicted_root_cause.get("affected_component") or "")
        aliases = sorted(CATEGORY_ALIASES.get(expected_category.lower(), set()))
        alias_covered = bool(
            expected_category
            and (
                predicted_category.lower() == expected_category.lower()
                or predicted_category.lower() in aliases
            )
        )
        budget = spec.get("budget") or {}
        details = record.get("details") or {}
        provider_model = details.get("model")
        provider_model = provider_model if isinstance(provider_model, dict) else None
        real = mode == "model"
        budget_exceeded = {
            "tool_calls": (record.get("tool_call_count") or 0) > budget.get("max_tool_calls", 1 << 30),
            "seconds": (record.get("duration_seconds") or 0.0) > budget.get("max_seconds_per_trial", 1 << 30),
        }
        error = _trial_error(record, budget_exceeded, real=real)
        return {
            "model_id": model["id"],
            "model_label": (
                (slot["model"] or model["id"])
                if real
                else (model.get("offline_label") or model.get("name") or model["id"])
            ),
            "model_identifier": slot["model"] if real else "scripted-fake",
            "model_backend": "real-llm" if real else "scripted-fake",
            "provider_model": provider_model,
            "reproduction_observation": observation,
            "oracle_version": oracle_version,
            "fixed_source_audit": audit,
            "source_citation_audit": citation_audit,
            "execution": {
                "mode": mode,
                "tier": tier,
                "tool_backend": settings.tool_backend,
                "llm_enabled": settings.llm_enabled,
                "llm_model": slot["model"] or None,
            },
            "strategy": strategy,
            "scenario": case.id,
            "trial": trial,
            "status": "ERROR" if error else "PASS",
            "final_status": record.get("final_status"),
            "run_id": record.get("run_id"),
            "duration_seconds": round(record.get("duration_seconds") or 0.0, 3),
            "usage": {"tokens": record.get("details", {}).get("token_usage"), "source": "provider-metadata"},
            "result": {
                "root_cause_correct": (
                    bool(record["root_cause_correct"] and record["evidence_hits_injection"])
                    if oracle_version
                    else record.get("root_cause_correct")
                ),
                "structured_root_cause_correct": record.get("root_cause_correct"),
                "fault_category_correct": record.get("fault_category_correct"),
                "affected_component_correct": record.get("affected_component_correct"),
                "evidence_supported": record.get("evidence_supported"),
                "evidence_hits_injection": record.get("evidence_hits_injection"),
                "tool_selection_score": round(record.get("tool_selection_score") or 0.0, 4),
                "tool_call_count": record.get("tool_call_count"),
                "patch_test_pass": record.get("patch_test_pass"),
                "unsafe_action": record.get("unsafe_action"),
                "expected_category": expected_category,
                "predicted_category": predicted_category,
                "expected_component": expected_component,
                "predicted_component": predicted_component,
                "category_alias_covered": alias_covered,
                "root_cause_summary": record.get("root_cause_summary"),
            },
            "budget_exceeded": budget_exceeded,
            "error": error,
        }


def _save_checkpoint(output: Path, payload: dict) -> None:
    staged = output.with_suffix(".tmp")
    staged.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    staged.replace(output)


async def main_async(root: Path, spec: dict, mode: str, environ: dict, output: Path, payload: dict) -> None:
    from incidentpilot.benchmarks.fault_cases import load_fault_cases

    cases = {case.id: case for case in load_fault_cases(root / "benchmarks" / "fault_cases")}
    trials, errors = payload["trials"], payload["errors"]
    real = mode == "model"
    payload["execution"] = {
        "mode": mode,
        "tier": "real_llm_investigation" if real else "workflow_fake",
        "model": "per-slot-env" if real else "scripted-fake",
        "tools": "mcp" if real else "synthetic-fake",
        "sandbox": "disabled",
        "network": "required" if real else "none",
    }
    trial_timeout = (spec.get("budget") or {}).get("max_seconds_per_trial", 600)
    for model in spec["models"]:
        for strategy in spec["strategies"]:
            for scenario in spec["scenarios"]:
                case = cases.get(scenario)
                if case is None:
                    errors.append({"scenario": scenario, "error": "故障场景不存在"})
                    continue
                for trial in range(1, int(spec["repetitions"]) + 1):
                    try:
                        result = await asyncio.wait_for(
                            run_trial(root, spec, model, strategy, case, trial, mode=mode, environ=environ),
                            timeout=trial_timeout,
                        )
                        trials.append(result)
                    except ReferenceResetError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        errors.append({
                            "model_id": model["id"],
                            "strategy": strategy,
                            "scenario": scenario,
                            "trial": trial,
                            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                            "traceback_tail": _tail(traceback.format_exc(), 600),
                        })
                    _save_checkpoint(output, payload)
    if trials and not errors and all(item["status"] == "PASS" for item in trials):
        payload["status"] = "PASS"


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: _incidentpilot_matrix.py <incidentpilot_root> <spec.json> "
            "<output.json> <offline|model>",
            file=sys.stderr,
        )
        return 2
    root = Path(argv[0]).resolve()
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    output = Path(argv[2]).resolve()
    mode = argv[3]
    if mode not in {"offline", "model"}:
        print(f"unknown matrix mode: {mode}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(root / "src"))
    payload: dict = {"status": "ERROR", "trials": [], "errors": [], "execution": {"mode": mode}}
    try:
        asyncio.run(main_async(root, spec, mode, dict(os.environ), output, payload))
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        payload["traceback_tail"] = _tail(traceback.format_exc(), 800)
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        _save_checkpoint(output, payload)
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
