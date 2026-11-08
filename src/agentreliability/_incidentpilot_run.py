"""在 IncidentPilot 环境中执行单一闭环场景。

脚本只使用标准库与 IncidentPilot 自身依赖，由 IncidentPilot 的虚拟环境
解释器启动。模型与工具均为脚本化替身（上游 fake/synthetic），
Docker 复测是真实容器执行；输出用 JSON 返回给平台适配器。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

SCENARIO_ID = "fault_002_slow_database_query"
FAULT_TYPE = "slow_database_query"
LATENCY_THRESHOLD_MS = 250.0


def _tail(text: str, limit: int = 2000) -> str:
    return (text or "")[-limit:]


def _causal_facts_coverage(
    ground_truth, root_cause: dict
) -> tuple[bool, list[dict]]:
    """逐条核验关键因果事实：至少一条事实的实质词覆盖达到一半。

    上游结构化评分对事实只做“任一关键词命中”的宽松判断，容易放过
    只重复 orders/latency 等泛词但机制错误的解释。判定器在此加严。
    """
    facts = [str(item).lower() for item in (ground_truth.get("causal_facts") or [])]
    if not facts:
        return False, []
    text = " ".join(
        [
            str(root_cause.get("summary") or ""),
            *[str(item) for item in root_cause.get("causal_facts") or []],
        ]
    ).lower()
    covered: list[dict] = []
    for fact in facts:
        tokens = {token for token in re.split(r"[^a-z0-9_]+", fact) if len(token) > 3}
        if not tokens:
            continue
        hits = sorted(token for token in tokens if token in text)
        ratio = len(hits) / len(tokens)
        covered.append(
            {
                "fact": fact,
                "tokens": sorted(tokens),
                "hits": hits,
                "ratio": round(ratio, 4),
            }
        )
    supported = any(item["ratio"] >= 0.5 for item in covered)
    return supported, covered


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def baseline_assertion(reference_dir: Path, fault_type: str, params: dict) -> dict:
    """对原版本注入故障并断言延迟超限；预期结论是 FAIL。"""
    sys.path.insert(0, str(reference_dir))
    from app import create_app
    from faults import reset_fault
    from fastapi.testclient import TestClient

    reset_fault()
    client = TestClient(create_app("sqlite:///:memory:"))
    healthy_started = datetime.now(timezone.utc)
    healthy_resp = client.get("/orders")
    healthy_ms = (
        datetime.now(timezone.utc) - healthy_started
    ).total_seconds() * 1000
    setup = client.post(
        "/admin/fault",
        json={"fault_type": fault_type, "params": params},
    )
    setup.raise_for_status()

    samples: list[float] = []
    bodies: list[object] = []
    for _ in range(3):
        started = datetime.now(timezone.utc)
        response = client.get("/orders")
        samples.append((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        bodies.append(response.json())
    reset_fault()
    healthy_body = healthy_resp.json()

    observed = sorted(samples)[len(samples) // 2]
    status_code = response.status_code
    payload_equal = all(body == healthy_body for body in bodies)
    failed = observed >= LATENCY_THRESHOLD_MS and status_code == 200 and payload_equal
    return {
        "name": "fault_active_latency",
        "scope": "original-source",
        "fault_type": fault_type,
        "params": params,
        "threshold_ms": LATENCY_THRESHOLD_MS,
        "healthy_latency_ms": round(healthy_ms, 2),
        "samples_ms": [round(item, 2) for item in samples],
        "observed_ms": round(observed, 2),
        "status_code": status_code,
        "payload_equal_to_healthy": payload_equal,
        "status": "FAIL" if failed else "PASS",
        "expected": "FAIL",
        "expected_matched": failed,
        "conclusion": (
            "原版本在故障条件下延迟超过阈值，故障已复现"
            if failed
            else "原版本未达到失败阈值，故障未复现"
        ),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


async def run_workflow(root: Path, reference_dir: Path, tmp: Path, case) -> dict:
    from incidentpilot.agent.graph import run_agent_workflow
    from incidentpilot.config import Settings
    from incidentpilot.models.schemas import IncidentCreate
    from incidentpilot.persistence.jobs import create_incident_and_run
    from incidentpilot.persistence.session import (
        dispose_engine,
        get_session_factory,
        init_db,
    )
    from incidentpilot.services.incidents import RunService
    from sqlalchemy import select

    from incidentpilot.models.db import (
        Evidence,
        Hypothesis,
        PatchArtifact,
        PatchAttempt,
        TestRun,
        ToolCall,
    )

    os.environ["GITHUB_INTEGRATION_ENABLED"] = "false"
    url = "sqlite+aiosqlite:///" + str(tmp / "incident.db")
    base = Settings(
        database_url=url,
        environment="test",
        llm_enabled=False,
        llm_api_key="",
        tool_backend="fake",
        sandbox_enabled=True,
        reference_repo_path=str(reference_dir),
        max_patch_attempts=1,
    )
    settings = base.model_copy(update={"sandbox_fault_type": case.fault_type})
    await init_db(url)
    factory = get_session_factory()

    async with factory() as session:
        incident, run = await create_incident_and_run(
            session,
            incident_data={
                **IncidentCreate.model_validate(case.incident).model_dump(),
                "owner_id": "platform-offline-investigation",
            },
            max_queued_runs=1000,
        )
        run_id = run.run_id
        incident_id = incident.id

    baseline = baseline_assertion(reference_dir, case.fault_type, case.params)
    baseline["linked_run_id"] = run_id
    baseline["linked_incident_id"] = incident_id

    state = await run_agent_workflow(
        incident_pk=incident_id, run_id=run_id, settings=settings
    )

    async with factory() as session:
        run_obj = await RunService(session).get(run_id)
        evidence = list(
            (
                await session.execute(select(Evidence).where(Evidence.run_pk == run_obj.id))
            ).scalars()
        )
        tool_calls = list(
            (
                await session.execute(select(ToolCall).where(ToolCall.run_pk == run_obj.id))
            ).scalars()
        )
        hypotheses = list(
            (
                await session.execute(select(Hypothesis).where(Hypothesis.run_pk == run_obj.id))
            ).scalars()
        )
        attempts = list(
            (
                await session.execute(
                    select(PatchAttempt).where(PatchAttempt.run_pk == run_obj.id)
                )
            ).scalars()
        )
        artifacts = list(
            (
                await session.execute(
                    select(PatchArtifact).where(PatchArtifact.run_pk == run_obj.id)
                )
            ).scalars()
        )
        test_runs = list(
            (
                await session.execute(select(TestRun).where(TestRun.run_pk == run_obj.id))
            ).scalars()
        )
        status = run_obj.status
        root_cause = run_obj.root_cause or {}
        result = run_obj.result or {}
        trace_id = run_obj.trace_id

    from incidentpilot.eval.scoring import score_structured_root_cause

    root_cause_summary = str(
        (root_cause or {}).get("summary") or state.get("root_cause", {}).get("summary") or ""
    )
    # 只采用根因自身声明的引用；缺引用时不得用本轮全部证据补齐。
    evidence_ids = list((root_cause or {}).get("evidence_ids") or [])
    expected_sources = set(str(item) for item in case.expected_evidence_sources)
    observed_sources = {
        item.tool_name for item in tool_calls if str(item.status).endswith("SUCCEEDED")
    }
    evidence_dicts = [
        {
            "evidence_id": item.evidence_id,
            "run_id": run_id,
            "tool_call_id": item.tool_call_id,
            "hash": item.content_hash,
            "result": item.result,
        }
        for item in evidence
    ]
    calls = [
        {"tool_call_id": item.tool_call_id, "status": item.status, "output": item.output}
        for item in tool_calls
    ]
    structured = score_structured_root_cause(
        case.ground_truth,
        root_cause or {},
        evidence=evidence_dicts,
        tool_calls=calls,
        run_id=run_id,
    )
    causal_supported, causal_coverage = _causal_facts_coverage(
        case.ground_truth, root_cause or {}
    )
    call_names = {item.tool_call_id: item.tool_name for item in tool_calls}
    cited = [item for item in evidence if item.evidence_id in set(evidence_ids)]
    cited_sources = sorted({call_names.get(item.tool_call_id, str(item.source)) for item in cited})
    verification = {
        "diagnosis_present": bool(root_cause_summary),
        "root_cause_verified": bool((root_cause or {}).get("verified")),
        "diagnosis_fault_category": (root_cause or {}).get("fault_category"),
        "expected_category_match": bool(structured["category_ok"]),
        "expected_component_match": bool(structured["component_ok"]),
        "causal_facts_match": causal_supported,
        "causal_facts_coverage": causal_coverage,
        "evidence_ids": evidence_ids,
        "evidence_bound_to_diagnosis": bool(structured["evidence_bound"]),
        "cited_evidence_sources": cited_sources,
        "evidence_supports_conclusion": bool(
            cited
            and len(cited_sources) >= 2
            and set(cited_sources) & expected_sources
        ),
        "expected_evidence_sources": sorted(expected_sources),
        "observed_evidence_sources": sorted(observed_sources),
        "expected_evidence_sources_covered": sorted(expected_sources & observed_sources),
    }

    artifact = artifacts[0] if artifacts else None
    patch_diff = artifact.patch_diff if artifact else ""
    patch_summary = ""
    if patch_diff:
        try:
            parsed = json.loads(patch_diff)
            patch_summary = parsed.get("reason") or parsed.get("type") or ""
        except (json.JSONDecodeError, AttributeError):
            patch_summary = patch_diff.splitlines()[0][:120] if patch_diff else ""

    payload = {
        "status": status,
        "workflow_state": state.get("workflow_state"),
        "state_status": state.get("status"),
        "run_id": run_id,
        "incident_id": incident_id,
        "trace_id": trace_id,
        "root_cause": {
            "summary": root_cause_summary,
            "fault_category": (root_cause or {}).get("fault_category"),
            "affected_component": (root_cause or {}).get("affected_component"),
            "causal_facts": (root_cause or {}).get("causal_facts") or [],
            "confidence": (root_cause or {}).get("confidence"),
            "verified": (root_cause or {}).get("verified"),
        },
        "hypotheses": [
            {
                "statement": item.statement,
                "evidence_ids": item.evidence_ids,
                "status": item.status,
                "verified": item.verified,
                "confidence": item.confidence,
            }
            for item in hypotheses
        ],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "source": item.source,
                "source_type": item.source_type,
                "tool_call_id": item.tool_call_id,
                "summary": item.summary,
                "content_hash": item.content_hash,
                "result": item.result,
                "content": item.content,
            }
            for item in evidence
        ],
        "tool_calls": [
            {
                "tool_call_id": item.tool_call_id,
                "tool_name": item.tool_name,
                "status": item.status,
                "latency_ms": round(item.latency_ms, 2) if item.latency_ms else None,
                "output": item.output,
                "error": item.error,
            }
            for item in tool_calls
        ],
        "patch_attempts": [
            {
                "attempt_number": item.attempt_number,
                "status": item.status,
                "patch_hash": item.patch_hash,
                "notes": item.notes,
            }
            for item in attempts
        ],
        "test_runs": [
            {
                "test_run_id": item.test_run_id,
                "status": item.status,
                "exit_code": item.exit_code,
                "duration_ms": item.duration_ms,
                "command": item.command,
                "mode": item.mode,
                "base_commit_sha": item.base_commit_sha,
                "stdout_tail": _tail(item.stdout),
                "stderr_tail": _tail(item.stderr),
            }
            for item in test_runs
        ],
        "patch_artifact": (
            {
                "patch_artifact_id": artifact.patch_artifact_id,
                "base_commit_sha": artifact.base_commit_sha,
                "patch_hash": artifact.patch_hash,
                "patch_diff": artifact.patch_diff,
                "patch_summary": patch_summary,
                "test_run_id": artifact.test_run_id,
            }
            if artifact
            else None
        ),
        "result": {
            "test_status": (result or {}).get("test_status"),
            "test_scope": (result or {}).get("test_scope"),
            "test_mode": (result or {}).get("test_mode"),
            "model": (result or {}).get("model"),
            "patch_hash": (result or {}).get("patch_hash"),
            "patch_artifact_id": (result or {}).get("patch_artifact_id"),
        },
        "verification": verification,
        "review": {
            "status": "PENDING_HUMAN_REVIEW" if status == "WAITING_APPROVAL" else "NOT_APPLICABLE",
            "human_approval": "NOT_RUN",
            "note": "上游流程停在 WAITING_APPROVAL，未模拟人工审批",
        },
    }

    await dispose_engine()
    return {"baseline": baseline, "investigation": payload}


async def run_sandbox_retest(root: Path, reference_dir: Path, tmp: Path, patch_diff: str, run_id: str) -> dict:
    from incidentpilot.config import Settings
    from incidentpilot.sandbox.manager import SandboxManager

    settings = Settings(
        environment="test",
        llm_enabled=False,
        llm_api_key="",
        tool_backend="fake",
        sandbox_enabled=True,
        sandbox_fault_type=FAULT_TYPE,
        reference_repo_path=str(reference_dir),
    )
    manager = SandboxManager(settings)
    retest_id = f"retest-{run_id[-8:]}"
    result: dict = {
        "status": "ERROR",
        "state": None,
        "baseline_sha": None,
        "patch_summary": "",
        "fault_active": None,
        "healthy_regression": None,
        "workspace": "isolated-temp",
    }
    try:
        manager.create_workspace(retest_id, reference_dir)
        result["baseline_sha"] = manager.base_commit_sha(retest_id)
        manager.apply_patch(retest_id, patch_diff)
        try:
            parsed = json.loads(patch_diff)
            result["patch_summary"] = parsed.get("reason") or parsed.get("type") or ""
        except (json.JSONDecodeError, AttributeError):
            result["patch_summary"] = "unified diff"

        fault = await manager.run_tests_async(retest_id, "pytest_fault", timeout=120)
        result["fault_active"] = {
            "profile": "pytest_fault",
            "scope": "fault-active",
            "status": "PASS" if fault.ok else "FAIL",
            "exit_code": fault.exit_code,
            "duration_ms": round(fault.duration_ms, 2),
            "mode": fault.mode,
            "command": " ".join(fault.command),
            "stdout_tail": _tail(fault.stdout),
            "stderr_tail": _tail(fault.stderr),
        }
        healthy = await manager.run_tests_async(retest_id, "pytest_regression", timeout=120)
        result["healthy_regression"] = {
            "profile": "pytest_regression",
            "scope": "healthy-path",
            "status": "PASS" if healthy.ok else "FAIL",
            "exit_code": healthy.exit_code,
            "duration_ms": round(healthy.duration_ms, 2),
            "mode": healthy.mode,
            "command": " ".join(healthy.command),
            "stdout_tail": _tail(healthy.stdout),
            "stderr_tail": _tail(healthy.stderr),
        }
        modes = {fault.mode, healthy.mode}
        if modes != {"docker"}:
            result["status"] = "ERROR"
            result["state"] = "sandbox_not_docker"
        elif fault.ok and healthy.ok:
            result["status"] = "PASS"
        else:
            result["status"] = "FAIL"
    except Exception as exc:  # noqa: BLE001 - 状态需显式返回
        result["status"] = "NOT_RUN"
        result["state"] = "sandbox_unavailable"
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        result["traceback_tail"] = _tail(traceback.format_exc(), 600)
    finally:
        manager.destroy_workspace(retest_id)
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: _incidentpilot_run.py <incidentpilot_root> <output.json>", file=sys.stderr)
        return 2
    root = Path(argv[0]).resolve()
    output = Path(argv[1]).resolve()
    reference_dir = root / "reference" / "orders_api"
    sys.path.insert(0, str(root / "src"))
    from incidentpilot.benchmarks.fault_cases import load_fault_case

    case = load_fault_case(root / "benchmarks" / "fault_cases" / f"{SCENARIO_ID}.yaml")
    payload: dict = {"status": "ERROR", "error": None}
    with tempfile.TemporaryDirectory(prefix="platform-quality-") as raw_tmp:
        tmp = Path(raw_tmp)
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                workflow = loop.run_until_complete(run_workflow(root, reference_dir, tmp, case))
            finally:
                loop.close()
            payload.update(workflow)
            patch = (payload.get("investigation") or {}).get("patch_artifact") or {}
            patch_diff = patch.get("patch_diff") or ""
            if patch_diff:
                retest = asyncio.run(
                    run_sandbox_retest(
                        root,
                        reference_dir,
                        tmp,
                        patch_diff,
                        payload["investigation"]["run_id"],
                    )
                )
                payload["sandbox_retest"] = retest
            else:
                payload["sandbox_retest"] = {
                    "status": "NOT_RUN",
                    "state": "missing_patch_artifact",
                    "error": "上游流程未产出补丁产物",
                }
            payload["status"] = "PASS"
            payload["execution"] = {
                "model": "scripted-fake",
                "tools": "synthetic-fake",
                "sandbox": "docker",
                "network": "none",
                "note": "模型与工具为上游脚本化替身；Docker 复测为真实容器",
            }
        except Exception as exc:  # noqa: BLE001
            payload["status"] = "ERROR"
            payload["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            payload["traceback_tail"] = _tail(traceback.format_exc(), 800)
        finally:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
