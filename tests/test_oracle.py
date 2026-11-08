"""判定器负例与 M2 覆盖：等价 SQL、重复行、有序性、错误分类。"""

from __future__ import annotations

import asyncio

from agentreliability.db import SqlInvocation, SqlitePort
from agentreliability.model_stub import SqlModelCall
from agentreliability.oracle import ERROR, FAIL, PASS, judge_case, rows_match


def _case(**overrides):
    case = {
        "case_id": "NEG-01",
        "expected_rows": [{"sales": 42}],
        "ordered": True,
        "expected_executions": {"count": 1, "statuses": ["success"]},
    }
    case.update(overrides)
    return case


def _observation(rows, statuses):
    return {
        "rows": rows,
        "invocations": [SqlInvocation(sql="SELECT ...", status=status) for status in statuses],
        "model_calls": [SqlModelCall(index=1, previous_error=None, sql="SELECT ...")],
    }


def test_negative_business_result_fails_with_evidence():
    judged = judge_case(_case(), _observation([{"sales": 92}], ["success"]))

    assert judged["verdict"] == FAIL
    rows_assertion = judged["assertions"][0]
    assert rows_assertion["verdict"] == FAIL
    assert rows_assertion["expected"]["rows"] == [{"sales": 42}]
    assert rows_assertion["actual"]["rows"] == [{"sales": 92}]


def test_execution_status_sequence_must_match():
    case = _case(expected_executions={"count": 2, "statuses": ["error", "success"]})
    judged = judge_case(case, _observation([{"sales": 42}], ["success", "success"]))

    assert judged["verdict"] == FAIL
    assertion = judged["assertions"][1]
    assert assertion["expected"]["statuses"] == ["error", "success"]
    assert assertion["actual"]["statuses"] == ["success", "success"]


def test_retry_context_must_carry_previous_error():
    case = _case(
        expected_executions={"count": 2, "statuses": ["error", "success"]},
        expect_retry_context=True,
    )
    judged = judge_case(
        case,
        {
            "rows": [{"sales": 42}],
            "invocations": [
                SqlInvocation(sql="bad", status="error"),
                SqlInvocation(sql="good", status="success"),
            ],
            "model_calls": [
                SqlModelCall(index=1, previous_error=None, sql="bad"),
                SqlModelCall(index=2, previous_error=None, sql="good"),
            ],
        },
    )

    assert judged["verdict"] == FAIL
    assert judged["assertions"][2]["name"] == "retry_context"
    assert judged["assertions"][2]["verdict"] == FAIL


def test_retry_context_passes_when_error_is_forwarded():
    case = _case(
        expected_executions={"count": 2, "statuses": ["error", "success"]},
        expect_retry_context=True,
    )
    judged = judge_case(
        case,
        {
            "rows": [{"sales": 42}],
            "invocations": [
                SqlInvocation(sql="bad", status="error"),
                SqlInvocation(sql="good", status="success"),
            ],
            "model_calls": [
                SqlModelCall(index=1, previous_error=None, sql="bad"),
                SqlModelCall(
                    index=2,
                    previous_error="OperationalError：执行失败",
                    sql="good",
                ),
            ],
        },
    )

    assert judged["verdict"] == PASS


def test_equivalent_sql_returns_same_business_rows(fixture_sql: str):
    port = SqlitePort(fixture_sql)
    lower_bound = asyncio.run(
        port.execute(
            "SELECT SUM(payment_amount) AS sales FROM orders "
            "WHERE order_date >= '2025-01-01' AND order_date < '2025-02-01'"
        )
    )
    between = asyncio.run(
        port.execute(
            "SELECT SUM(payment_amount) AS sales FROM orders "
            "WHERE order_date BETWEEN '2025-01-01' AND '2025-01-31'"
        )
    )
    port.close()

    assert lower_bound == [{"sales": 42}]
    assert between == [{"sales": 42}]
    assert rows_match(lower_bound, between, ordered=True)


def test_duplicate_rows_are_preserved():
    duplicated = [{"payment_amount": 12}, {"payment_amount": 12}]
    deduplicated = [{"payment_amount": 12}]

    assert rows_match(duplicated, list(duplicated), ordered=True)
    assert not rows_match(duplicated, deduplicated, ordered=True)


def test_ordered_and_unordered_comparison_are_declared_per_case():
    expected = [{"sales": 12}, {"sales": 30}]
    reordered = [{"sales": 30}, {"sales": 12}]

    assert not rows_match(expected, reordered, ordered=True)
    assert rows_match(expected, reordered, ordered=False)


def test_execution_error_is_classified_as_error():
    judged = judge_case(
        _case(),
        {"error": "RuntimeError: 模型桩脚本已耗尽"},
    )

    assert judged["verdict"] == ERROR
    assert judged["assertions"][0]["name"] == "run"
