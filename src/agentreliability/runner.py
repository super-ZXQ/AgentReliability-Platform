"""用例加载、多轮试验执行与轨迹采集。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .adapter import build_runtime, new_conversations
from .db import SqlitePort
from .model_stub import ScriptedModel
from .oracle import judge_case, NOT_RUN
from .target import TargetInfo
from .trace import TraceCollector

_EXPECTATION_KEYS = (
    "expected_rows",
    "ordered",
    "expected_executions",
    "expected_model_calls",
    "expect_retry_context",
    "history_contains",
    "history_excludes",
    "expected_sql_error",
    "expected_answer_contains",
    "expected_intent",
    "expected_validation_failures",
    "forbidden_marker",
    "executed_sql_forbidden",
    "sql_port_failures",
)


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                case = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"用例文件第 {line_number} 行不是合法 JSON") from exc
            if not isinstance(case, dict):
                raise ValueError(f"用例文件第 {line_number} 行必须是对象")
            if "case_id" not in case:
                raise ValueError(f"用例文件第 {line_number} 行缺少 case_id")
            if not case.get("turns") and "question" not in case:
                raise ValueError(f"用例文件第 {line_number} 行缺少 question 或 turns")
            cases.append(case)
    case_ids = [case.get("case_id") for case in cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("用例 ID 重复")
    return cases


def _turns(case: dict[str, Any]) -> list[dict[str, Any]]:
    turns = case.get("turns")
    if turns:
        return [dict(turn) for turn in turns]
    return [
        {
            "question": case["question"],
            "sql_script": case.get("sql_script", []),
            "answer": case.get("answer", ""),
        }
    ]


def _expected_payload(case: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "questions": [str(turn["question"]) for turn in _turns(case)],
        "rows": case.get("expected_rows"),
        "ordered": case.get("ordered", True),
        "executions": case.get("expected_executions"),
    }
    for key in _EXPECTATION_KEYS:
        if key in ("expected_rows", "ordered", "expected_executions"):
            continue
        if key in case:
            payload[key] = case[key]
    return payload


def not_run_results(cases: list[dict[str, Any]], reason: str) -> list[dict[str, Any]]:
    return [
        {
            "case_id": case["case_id"],
            "trial": 1,
            "variant": None,
            "duration_ms": 0,
            "verdict": NOT_RUN,
            "expected": _expected_payload(case),
            "actual": {"reason": reason},
            "assertions": [
                {
                    "name": "environment",
                    "verdict": NOT_RUN,
                    "expected": "目标 checkout 可用",
                    "actual": reason,
                }
            ],
            "trace_refs": [],
        }
        for case in cases
    ]


async def run_case(
    case: dict[str, Any],
    *,
    target: TargetInfo,
    fixture_sql: str,
    trace: TraceCollector,
    trial: int = 1,
    variant: str | None = None,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    turns = _turns(case)
    start_seq = trace.next_seq
    started = time.perf_counter()
    all_model_calls: list[Any] = []
    all_steps: list[dict[str, Any]] = []
    turn_summaries: list[dict[str, Any]] = []
    sql_port: SqlitePort | None = None
    observation: dict[str, Any] = {
        "rows": [],
        "invocations": [],
        "model_calls": [],
        "agent_steps": [],
        "turns": [],
    }

    try:
        sql_port = SqlitePort(
            fixture_sql,
            trace=trace,
            case_id=case_id,
            trial=trial,
            failure_script=list(case.get("sql_port_failures", [])),
        )
        conversations = new_conversations(target)
        final_result = None

        for turn_number, turn in enumerate(turns, start=1):
            sql_port.round_no = turn_number
            model = ScriptedModel(
                sql_script=list(turn.get("sql_script", [])),
                answer=str(turn.get("answer", "")),
                trace=trace,
                case_id=case_id,
                trial=trial,
                round_no=turn_number,
            )
            runtime = build_runtime(
                target=target,
                sql_port=sql_port,
                model=model,
                conversations=conversations,
                demo_fault=variant,
            )

            def on_step(step: dict[str, Any], round_no: int = turn_number) -> None:
                event = trace.open(
                    case_id=case_id,
                    trial=trial,
                    round_no=round_no,
                    source="agent",
                    step=step["name"],
                    summary=step["summary"],
                )
                trace.close(event, step["status"], step["summary"], step["duration_ms"])
                all_steps.append({"round": round_no, **step})

            owner = str(turn.get("owner") or case.get("owner") or "benchmark")
            thread_id = str(
                turn.get("thread_id")
                or case.get("thread_id")
                or f"{case_id}-trial-{trial}"
            )
            state = await runtime.astream(
                str(turn["question"]),
                owner=owner,
                thread_id=thread_id,
                on_step=on_step,
            )
            result = state["result"]
            all_model_calls.extend(model.sql_calls)
            final_result = result
            turn_summaries.append(
                {
                    "round": turn_number,
                    "question": str(turn["question"]),
                    "owner": owner,
                    "thread_id": thread_id,
                    "rows": result.rows,
                    "sql": result.sql,
                    "sql_error": result.sql_error,
                    "answer": result.answer,
                }
            )

        observation = {
            "rows": final_result.rows if final_result is not None else [],
            "invocations": sql_port.invocations,
            "model_calls": all_model_calls,
            "agent_steps": all_steps,
            "turns": turn_summaries,
            "answer": final_result.answer if final_result is not None else "",
            "sql": final_result.sql if final_result is not None else None,
            "sql_error": final_result.sql_error if final_result is not None else None,
            "intent": final_result.intent if final_result is not None else None,
            "usage": (
                asdict(final_result.usage)
                if final_result is not None and final_result.usage is not None
                else None
            ),
        }
    except Exception as exc:
        observation = {
            "error": f"{type(exc).__name__}: {exc}",
            "rows": [],
            "invocations": sql_port.invocations if sql_port is not None else [],
            "model_calls": all_model_calls,
            "agent_steps": all_steps,
            "turns": turn_summaries,
        }
    finally:
        if sql_port is not None:
            sql_port.close()

    judged = judge_case(case, observation)
    duration_ms = max(0, round((time.perf_counter() - started) * 1000))
    actual: dict[str, Any] = {
        "rows": observation.get("rows", []),
        "executions": [
            {
                "sql": invocation.sql,
                "status": invocation.status,
                "error": invocation.error,
                "row_count": invocation.row_count,
                "duration_ms": invocation.duration_ms,
            }
            for invocation in observation.get("invocations", [])
        ],
        "model_sql_calls": [
            {
                "turn": call.turn,
                "index": call.index,
                "previous_error": call.previous_error,
                "sql": call.sql,
                "error": call.error,
                "history": call.history,
            }
            for call in observation.get("model_calls", [])
        ],
        "agent_steps": [
            {
                "round": step.get("round"),
                "name": step.get("name"),
                "status": step.get("status"),
                "summary": step.get("summary"),
            }
            for step in observation.get("agent_steps", [])
        ],
        "turns": observation.get("turns", []),
    }
    for key in ("answer", "sql", "sql_error", "intent", "usage", "error"):
        if key in observation:
            actual[key] = observation[key]

    return {
        "case_id": case_id,
        "trial": trial,
        "variant": variant or case.get("variant"),
        "duration_ms": duration_ms,
        "verdict": judged["verdict"],
        "expected": _expected_payload(case),
        "actual": actual,
        "assertions": judged["assertions"],
        "trace_refs": trace.refs_since(start_seq, case_id, trial),
    }


async def run_cases(
    cases: list[dict[str, Any]],
    *,
    target: TargetInfo,
    fixture_sql: str,
    run_id: str,
    variant: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trace = TraceCollector(run_id=run_id)
    results = []
    for case in cases:
        results.append(
            await run_case(
                case,
                target=target,
                fixture_sql=fixture_sql,
                trace=trace,
                trial=1,
                variant=variant,
            )
        )
    return results, trace.events


def run_cases_sync(
    cases: list[dict[str, Any]],
    *,
    target: TargetInfo,
    fixture_sql: str,
    run_id: str,
    variant: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return asyncio.run(
        run_cases(
            cases,
            target=target,
            fixture_sql=fixture_sql,
            run_id=run_id,
            variant=variant,
        )
    )
