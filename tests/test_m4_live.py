"""M4 验收：真实模型模式的配置门槛、18 次试验与提供方错误分类。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentreliability.cli import main
from agentreliability.html_report import render_run_html
from agentreliability.live import (
    LIVE_CASE_IDS,
    LiveConfig,
    build_live_report,
    run_live_cases_sync,
    select_live_cases,
)


@dataclass
class FakeResponse:
    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

CASES = Path("benchmarks/commerce/cases.jsonl")
FIXTURE = Path("benchmarks/commerce/fixture.sql")
CONFIG = LiveConfig(
    api_key="test-key",
    base_url="https://example.invalid/v1",
    model="fake-model",
    provider_host="example.invalid",
)


def _sql_for(question: str) -> str:
    if "各平台" in question:
        return (
            "SELECT platform, SUM(payment_amount) AS sales FROM orders "
            "WHERE order_date >= '2025-01-01' AND order_date < '2025-02-01' "
            "GROUP BY platform"
        )
    if "订单明细" in question:
        return (
            "SELECT order_id FROM orders "
            "WHERE order_date >= '2025-01-01' AND order_date < '2025-02-01' "
            "ORDER BY order_id"
        )
    if "金额最高" in question:
        return (
            "SELECT order_id, payment_amount FROM orders "
            "ORDER BY payment_amount DESC, order_id ASC LIMIT 2"
        )
    if "2 月" in question:
        return (
            "SELECT SUM(payment_amount) AS sales FROM orders "
            "WHERE order_date >= '2025-02-01' AND order_date < '2025-03-01'"
        )
    return (
        "SELECT SUM(payment_amount) AS sales FROM orders "
        "WHERE order_date >= '2025-01-01' AND order_date < '2025-02-01'"
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.closed = False

    async def generate_sql(self, query, schema, history, sources, previous_error):
        return FakeResponse(
            content=_sql_for(query), input_tokens=10, output_tokens=5, total_tokens=15
        )

    async def answer(self, query, intent, history, sources, sql, rows):
        if "1 月" in query and "2 月" not in query:
            content = "2025 年 1 月销售额为 42。"
        else:
            content = f"结果：{rows}"
        return FakeResponse(content=content, input_tokens=8, output_tokens=4, total_tokens=12)

    async def aclose(self) -> None:
        self.closed = True


class FailingAdapter:
    async def generate_sql(self, query, schema, history, sources, previous_error):
        raise ConnectionError("provider unreachable")

    async def answer(self, query, intent, history, sources, sql, rows):
        return FakeResponse(content="fallback")

    async def aclose(self) -> None:
        return None


def _run_args(tmp_path: Path, target, *, allow_network: bool) -> list[str]:
    args = [
        "run",
        "--mode",
        "live",
        "--target-dir",
        str(target.path),
        "--out",
        str(tmp_path / "live"),
        "--cases",
        str(CASES),
        "--fixture",
        str(FIXTURE),
        "--repeat",
        "3",
    ]
    if allow_network:
        args.append("--allow-network")
    return args


def test_live_requires_explicit_network_switch(tmp_path, target):
    assert main(_run_args(tmp_path, target, allow_network=False)) == 2
    report = __import__("json").loads(
        (tmp_path / "live" / "report.json").read_text(encoding="utf-8")
    )
    assert report["mode"] == "live"
    assert report["summary"]["total"] == 18
    assert report["summary"]["not_run"] == 18
    assert report["summary"]["verdict"] == "NOT_RUN"
    assert "--allow-network" in report["live"]["environment"]["reason"]


def test_live_missing_credentials_marks_all_tasks_not_run(
    tmp_path, target, monkeypatch
):
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)

    assert main(_run_args(tmp_path, target, allow_network=True)) == 2
    report = __import__("json").loads(
        (tmp_path / "live" / "report.json").read_text(encoding="utf-8")
    )
    assert report["summary"]["total"] == 18
    assert report["summary"]["not_run"] == 18
    assert report["summary"]["verdict"] == "NOT_RUN"
    assert report["live"]["environment"]["missing"] == [
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
    ]
    assert len(report["cases"]) == 18
    assert len(report["case_summaries"]) == 6
    assert all(item["not_run"] == 3 for item in report["case_summaries"])


def test_live_fake_provider_runs_18_tasks_and_summarizes(cases, target, fixture_sql):
    live_cases = select_live_cases(cases)
    assert [case["case_id"] for case in live_cases] == list(LIVE_CASE_IDS)

    results, trace = run_live_cases_sync(
        live_cases,
        target=target,
        fixture_sql=fixture_sql,
        run_id="test-live-fake",
        repeat=3,
        config=CONFIG,
        adapter_factory=lambda _target, _config: FakeAdapter(),
    )
    assert len(results) == 18
    assert all(result["verdict"] == "PASS" for result in results)
    assert {result["trial"] for result in results} == {1, 2, 3}
    assert any(event.get("source") == "model" for event in trace)

    report = build_live_report(
        run_id="test-live-fake",
        target=target,
        cases_path=CASES,
        fixture_path=FIXTURE,
        results=results,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        command="uv run --locked agentreliability run --mode live --allow-network --repeat 3",
        repeat=3,
        requested_cases=list(LIVE_CASE_IDS),
        config=CONFIG,
        missing=[],
    )
    assert report["summary"]["pass"] == 18
    assert len(report["case_summaries"]) == 6
    assert all(item["trials"] == 3 for item in report["case_summaries"])
    assert all(item["pass"] == 3 for item in report["case_summaries"])
    assert report["live"]["model"]["name"] == "fake-model"

    html = render_run_html(report, trace)
    assert "真实模型报告" in html
    assert "fake-model" in html
    assert "每条用例汇总" in html


def test_live_provider_error_is_classified_as_error(cases, target, fixture_sql):
    case = next(case for case in cases if case["case_id"] == "DATA-01")
    results, trace = run_live_cases_sync(
        [case],
        target=target,
        fixture_sql=fixture_sql,
        run_id="test-live-provider-error",
        repeat=1,
        config=CONFIG,
        adapter_factory=lambda _target, _config: FailingAdapter(),
    )
    result = results[0]
    assert result["verdict"] == "ERROR"
    assert result["actual"]["provider_errors"]
    assert result["actual"]["provider_errors"][0]["category"] == "connection"
    assert result["assertions"][0]["name"] == "provider_error"
    assert any(
        event.get("provider_error") for event in trace if event.get("source") == "model"
    )


def test_offline_rejects_live_only_flags(tmp_path, target):
    base = [
        "run",
        "--mode",
        "offline",
        "--target-dir",
        str(target.path),
        "--out",
        str(tmp_path / "offline"),
        "--cases",
        str(CASES),
        "--fixture",
        str(FIXTURE),
    ]
    assert main(base + ["--repeat", "2"]) == 2
    assert main(base + ["--allow-network"]) == 2
