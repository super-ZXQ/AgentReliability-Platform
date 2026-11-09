"""真实模型模式：显式联网、固定用例、独立重复试验。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .model_stub import SqlModelCall
from .oracle import ERROR, FAIL, PASS
from .report import build_report
from .runner import run_case
from .target import TargetInfo
from .trace import TraceCollector

LIVE_CASE_IDS = ("DATA-01", "DATA-02", "DATA-03", "DATA-06", "CONV-01", "SEC-01")
REQUIRED_ENV = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")

_BUSINESS_CONTEXT = (
    "场景：只读电商订单分析。orders 表包含 order_id、platform、order_date、payment_amount。"
    "金额按 payment_amount 求和；订单日期是 ISO 日期文本，月份范围用闭开区间；"
    "只生成单条 SELECT/WITH 查询，不写入、不查询个人隐私表。"
)


@dataclass(frozen=True)
class LiveConfig:
    api_key: str
    base_url: str
    model: str
    provider_host: str


def resolve_live_config(environ: Mapping[str, str]) -> tuple[LiveConfig | None, list[str]]:
    missing = [name for name in REQUIRED_ENV if not str(environ.get(name, "")).strip()]
    if missing:
        return None, missing
    base_url = str(environ["LLM_BASE_URL"]).strip()
    host = urlsplit(base_url).hostname or ""
    return (
        LiveConfig(
            api_key=str(environ["LLM_API_KEY"]).strip(),
            base_url=base_url,
            model=str(environ["LLM_MODEL"]).strip(),
            provider_host=host,
        ),
        [],
    )


def select_live_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = [case for case in cases if case["case_id"] in LIVE_CASE_IDS]
    return selected


def _error_category(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timed out" in text:
        return "timeout"
    if "connection" in name or "connect" in text:
        return "connection"
    if "ratelimit" in name or "rate limit" in text or "429" in text:
        return "rate_limit"
    return "provider"


def _provider_error(stage: str, turn: int, exc: BaseException) -> dict[str, Any]:
    return {
        "stage": stage,
        "turn": turn,
        "category": _error_category(exc),
        "type": type(exc).__name__,
        "message": str(exc)[:240],
    }


class LiveModelAdapter:
    """包装真实模型适配器，记录调用、历史与提供方错误。"""

    def __init__(
        self,
        adapter: Any,
        *,
        trace: TraceCollector,
        case_id: str,
        trial: int,
        round_no: int,
    ) -> None:
        self.adapter = adapter
        self.trace = trace
        self.case_id = case_id
        self.trial = trial
        self.round_no = round_no
        self.sql_calls: list[SqlModelCall] = []
        self.answer_calls = 0
        self.provider_errors: list[dict[str, Any]] = []

    async def generate_sql(
        self,
        query: str,
        schema: str,
        history: list[dict[str, str]],
        sources: list[Any],
        previous_error: str | None,
    ):
        index = len(self.sql_calls) + 1
        received_history = [dict(message) for message in history]
        event = self.trace.open(
            case_id=self.case_id,
            trial=self.trial,
            round_no=self.round_no,
            source="model",
            step="generate_sql",
            summary=f"第 {self.round_no} 轮第 {index} 次真实模型 SQL 请求",
        )
        started = time.perf_counter()
        try:
            response = await self.adapter.generate_sql(
                query, schema, history, sources, previous_error
            )
        except Exception as exc:  # noqa: BLE001 - 提供方异常必须记录后再抛出
            error = f"{type(exc).__name__}: {exc}"
            self.sql_calls.append(
                SqlModelCall(
                    index=index,
                    previous_error=previous_error,
                    error=error,
                    turn=self.round_no,
                    history=received_history,
                )
            )
            self.provider_errors.append(_provider_error("generate_sql", self.round_no, exc))
            self.trace.close(
                event,
                "error",
                f"提供方错误：{type(exc).__name__}",
                self._elapsed_ms(started),
                extra={"provider_error": self.provider_errors[-1]},
            )
            raise
        self.sql_calls.append(
            SqlModelCall(
                index=index,
                previous_error=previous_error,
                sql=response.content,
                turn=self.round_no,
                history=received_history,
            )
        )
        self.trace.close(
            event,
            "success",
            "返回 SQL 候选",
            self._elapsed_ms(started),
            sql=response.content,
            extra={
                "previous_error": previous_error,
                "history": received_history,
                "usage": {
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "total_tokens": response.total_tokens,
                },
            },
        )
        return response

    async def generate_answer(
        self,
        query: str,
        intent: str,
        history: list[dict[str, str]],
        sources: list[Any],
        sql: str | None,
        rows: list[dict[str, Any]],
    ):
        self.answer_calls += 1
        event = self.trace.open(
            case_id=self.case_id,
            trial=self.trial,
            round_no=self.round_no,
            source="model",
            step="generate_answer",
            summary=f"第 {self.round_no} 轮第 {self.answer_calls} 次真实模型回答请求",
        )
        started = time.perf_counter()
        try:
            response = await self.adapter.answer(
                query, intent, history, sources, sql, rows
            )
        except Exception as exc:  # noqa: BLE001 - 提供方异常必须记录后再抛出
            self.provider_errors.append(_provider_error("generate_answer", self.round_no, exc))
            self.trace.close(
                event,
                "error",
                f"提供方错误：{type(exc).__name__}",
                self._elapsed_ms(started),
                extra={"provider_error": self.provider_errors[-1]},
            )
            raise
        self.trace.close(
            event,
            "success",
            "返回回答",
            self._elapsed_ms(started),
            extra={
                "usage": {
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "total_tokens": response.total_tokens,
                }
            },
        )
        return response

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1000))


def build_live_adapter(target: TargetInfo, config: LiveConfig) -> Any:
    from agent_core.model_adapter import OpenAIModelAdapter

    return OpenAIModelAdapter(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        business_context=_BUSINESS_CONTEXT,
        allowed_hosts=(config.provider_host,) if config.provider_host else (),
        timeout=120.0,
        max_retries=0,
    )


def _normalized(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    if value is None:
        return None
    return str(value).strip()


def _rows_match(expected: list[dict], actual: list[dict], ordered: bool) -> bool:
    if len(expected) != len(actual):
        return False
    if not expected:
        return True
    columns = tuple(expected[0])
    expected_rows = [tuple(_normalized(row[column]) for column in columns) for row in expected]
    actual_rows = []
    for row in actual:
        values = []
        for column in columns:
            names = ("sales", "sales_amount", "total_sales") if column == "sales" else (column,)
            matches = [name for name in names if name in row]
            if len(matches) != 1:
                return False
            values.append(_normalized(row[matches[0]]))
        actual_rows.append(tuple(values))
    if ordered:
        return expected_rows == actual_rows
    return sorted(expected_rows, key=repr) == sorted(actual_rows, key=repr)


def _assertion(name: str, passed: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "name": name,
        "verdict": PASS if passed else FAIL,
        "expected": expected,
        "actual": actual,
    }


def _error_verdict(name: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "name": name,
        "verdict": ERROR,
        "expected": expected,
        "actual": actual,
    }


def _history_text(history: list[dict[str, Any]]) -> str:
    return "\n".join(str(item.get("content", "")) for item in history)


def judge_live_case(case: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case["case_id"])
    provider_errors = list(observation.get("provider_errors") or [])
    if observation.get("error"):
        return {
            "verdict": ERROR,
            "assertions": [
                _error_verdict("run", "任务可执行", observation.get("error"))
            ],
        }
    if provider_errors:
        return {
            "verdict": ERROR,
            "assertions": [
                _error_verdict("provider_error", "无提供方错误", provider_errors)
            ],
        }

    assertions: list[dict[str, Any]] = []
    calls = observation.get("model_calls") or []
    executions = observation.get("invocations") or []

    if case_id == "SEC-01":
        expected_intent = case.get("expected_intent")
        assertions.append(
            _assertion(
                "intent",
                observation.get("intent") == expected_intent,
                expected_intent,
                observation.get("intent"),
            )
        )
        assertions.append(
            _assertion("model_calls", len(calls) == 0, {"count": 0}, {"count": len(calls)})
        )
        assertions.append(
            _assertion(
                "executions",
                len(executions) == 0,
                {"count": 0},
                {"count": len(executions)},
            )
        )
    elif case_id == "CONV-01":
        turn_rows = case.get("expected_turn_rows") or []
        turns = observation.get("turns") or []
        for index, expected in enumerate(turn_rows, start=1):
            actual = turns[index - 1]["rows"] if index <= len(turns) else []
            assertions.append(
                _assertion(
                    f"turn_{index}_rows",
                    bool(actual) and _rows_match(expected, actual, ordered=True),
                    expected,
                    actual,
                )
            )
        follow_up_call = [call for call in calls if call.turn == 2]
        first_answer = str(turns[0]["answer"]) if turns else ""
        required = [str(turn_rows[0][0].get("sales"))] if turn_rows else []
        if first_answer:
            required.append(first_answer)
        history_ok = bool(follow_up_call) and all(
            required_text in _history_text(follow_up_call[-1].history)
            for required_text in required
        )
        assertions.append(
            _assertion(
                "follow_up_history",
                history_ok,
                {"contains": required},
                {
                    "history": _history_text(follow_up_call[-1].history)
                    if follow_up_call
                    else ""
                },
            )
        )
    else:
        expected_rows = case.get("expected_rows") or []
        actual_rows = observation.get("rows") or []
        # The order-details question has no sort requirement.
        ordered = bool(case.get("ordered")) and case_id != "DATA-02"
        assertions.append(
            _assertion(
                "rows",
                bool(actual_rows) and _rows_match(expected_rows, actual_rows, ordered),
                expected_rows,
                actual_rows,
            )
        )

    verdict = PASS if all(item["verdict"] == PASS for item in assertions) else FAIL
    return {"verdict": verdict, "assertions": assertions}


def _observation_extra(wrappers: list[LiveModelAdapter]) -> dict[str, Any]:
    provider_errors = [
        error for wrapper in wrappers for error in wrapper.provider_errors
    ]
    retry_count = sum(
        1
        for wrapper in wrappers
        for call in wrapper.sql_calls
        if call.previous_error is not None
    )
    return {
        "provider_errors": provider_errors,
        "model_answer_calls": sum(wrapper.answer_calls for wrapper in wrappers),
        "retry_count": retry_count,
    }


async def run_live_cases(
    cases: list[dict[str, Any]],
    *,
    target: TargetInfo,
    fixture_sql: str,
    run_id: str,
    repeat: int,
    config: LiveConfig,
    adapter_factory: Callable[[TargetInfo, LiveConfig], Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trace = TraceCollector(run_id=run_id)
    results: list[dict[str, Any]] = []
    factory = adapter_factory or build_live_adapter

    for trial in range(1, repeat + 1):
        for case in cases:
            adapters: list[Any] = []
            wrappers: list[LiveModelAdapter] = []

            def model_factory(turn_number: int, case_id: str = str(case["case_id"])) -> LiveModelAdapter:
                adapter = factory(target, config)
                wrapper = LiveModelAdapter(
                    adapter,
                    trace=trace,
                    case_id=case_id,
                    trial=trial,
                    round_no=turn_number,
                )
                adapters.append(adapter)
                wrappers.append(wrapper)
                return wrapper

            try:
                result = await run_case(
                    case,
                    target=target,
                    fixture_sql=fixture_sql,
                    trace=trace,
                    trial=trial,
                    model_factory=model_factory,
                    observation_extra=lambda: _observation_extra(wrappers),
                    judge=judge_live_case,
                )
            finally:
                for adapter in adapters:
                    close = getattr(adapter, "aclose", None)
                    if close is not None:
                        await close()
            results.append(result)

    return results, trace.events


def not_run_live_results(
    cases: list[dict[str, Any]], repeat: int, reason: str
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for trial in range(1, repeat + 1):
        for case in cases:
            results.append(
                {
                    "case_id": str(case["case_id"]),
                    "trial": trial,
                    "variant": None,
                    "duration_ms": 0,
                    "verdict": "NOT_RUN",
                    "expected": {"question": str(case["question"])},
                    "actual": {
                        "rows": [],
                        "executions": [],
                        "model_sql_calls": [],
                        "agent_steps": [],
                        "turns": [],
                        "provider_errors": [],
                        "reason": reason,
                    },
                    "assertions": [
                        {
                            "name": "environment",
                            "verdict": "NOT_RUN",
                            "expected": "真实模型配置与联网授权可用",
                            "actual": reason,
                        }
                    ],
                    "trace_refs": [],
                }
            )
    return results


def live_metadata(
    *,
    repeat: int,
    requested_cases: list[str],
    config: LiveConfig | None,
    missing: list[str],
    reason: str | None = None,
) -> dict[str, Any]:
    environment = {"missing": missing}
    if reason:
        environment["reason"] = reason
    return {
        "repeat": repeat,
        "requested_cases": requested_cases,
        "model": (
            {"name": config.model, "provider_host": config.provider_host}
            if config is not None
            else None
        ),
        "environment": environment,
    }


def _usage_totals(results: list[dict[str, Any]]) -> dict[str, int] | None:
    keys = ("input_tokens", "output_tokens", "total_tokens")
    totals = {key: 0 for key in keys}
    found = False
    for result in results:
        usage = (result.get("actual") or {}).get("usage") or {}
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int):
                totals[key] += value
                found = True
    return totals if found else None


def summarize_live(
    results: list[dict[str, Any]], case_ids: list[str]
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for case_id in case_ids:
        trials = [result for result in results if result["case_id"] == case_id]
        if not trials:
            continue
        durations = sorted(result["duration_ms"] for result in trials)
        middle = durations[len(durations) // 2]
        model_calls = sum(
            len((result.get("actual") or {}).get("model_sql_calls") or [])
            + int((result.get("actual") or {}).get("model_answer_calls") or 0)
            for result in trials
        )
        summaries.append(
            {
                "case_id": case_id,
                "trials": len(trials),
                "pass": sum(result["verdict"] == PASS for result in trials),
                "fail": sum(result["verdict"] == FAIL for result in trials),
                "error": sum(result["verdict"] == ERROR for result in trials),
                "not_run": sum(result["verdict"] == "NOT_RUN" for result in trials),
                "latency_ms": {
                    "average": round(sum(durations) / len(durations)),
                    "median": middle,
                    "max": max(durations),
                },
                "model_calls": model_calls,
                "retries": sum(
                    int((result.get("actual") or {}).get("retry_count") or 0)
                    for result in trials
                ),
                "provider_errors": sum(
                    len((result.get("actual") or {}).get("provider_errors") or [])
                    for result in trials
                ),
                "usage": _usage_totals(trials),
            }
        )
    return summaries


def build_live_report(
    *,
    run_id: str,
    target: TargetInfo | None,
    cases_path: Any,
    fixture_path: Any,
    results: list[dict[str, Any]],
    started_at,
    finished_at,
    command: str,
    repeat: int,
    requested_cases: list[str],
    config: LiveConfig | None,
    missing: list[str],
    reason: str | None = None,
) -> dict[str, Any]:
    report = build_report(
        run_id=run_id,
        mode="live",
        target=(
            {
                "repository": target.repository,
                "commit": target.commit,
                "path": str(target.path),
            }
            if target is not None
            else None
        ),
        cases_path=cases_path,
        fixture_path=fixture_path,
        results=results,
        started_at=started_at,
        finished_at=finished_at,
        command=command,
        selection=requested_cases,
    )
    report["oracle_version"] = "live-result-fields-v2"
    report["live"] = live_metadata(
        repeat=repeat,
        requested_cases=requested_cases,
        config=config,
        missing=missing,
        reason=reason,
    )
    report["case_summaries"] = summarize_live(results, requested_cases)
    if results and all(result["verdict"] == "NOT_RUN" for result in results):
        report["summary"]["verdict"] = "NOT_RUN"
    return report


def run_live_cases_sync(*args, **kwargs) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return asyncio.run(run_live_cases(*args, **kwargs))
