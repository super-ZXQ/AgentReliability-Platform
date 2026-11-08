"""M2 重点验收：多轮历史、超时重试上限、越权 SQL 前置拦截。"""

from __future__ import annotations

import json

from agentreliability.runner import run_cases_sync

EXPECTED_IDS = [
    "DATA-01",
    "DATA-02",
    "DATA-03",
    "DATA-04",
    "DATA-05",
    "DATA-06",
    "DATA-07",
    "DATA-08",
    "CONV-01",
    "CONV-02",
    "CONV-03",
    "CONV-04",
    "REL-01",
    "REL-02",
    "REL-03",
    "REL-04",
    "SEC-01",
    "SEC-02",
    "SEC-03",
    "SEC-04",
]


def _run_one(case, target, fixture_sql):
    results, trace = run_cases_sync(
        [case], target=target, fixture_sql=fixture_sql, run_id=f"test-{case['case_id']}"
    )
    return results[0], trace


def test_suite_matrix_is_complete(cases):
    assert [case["case_id"] for case in cases] == EXPECTED_IDS


def test_all_20_cases_pass(cases, target, fixture_sql):
    results, _ = run_cases_sync(
        cases, target=target, fixture_sql=fixture_sql, run_id="test-m2-suite"
    )

    assert [result["case_id"] for result in results] == EXPECTED_IDS
    assert all(result["verdict"] == "PASS" for result in results)
    assert sum(result["verdict"] == "PASS" for result in results) == 20


def test_conv_01_model_receives_previous_turn_history(cases, target, fixture_sql):
    case = next(item for item in cases if item["case_id"] == "CONV-01")
    result, trace = _run_one(case, target, fixture_sql)

    assert result["verdict"] == "PASS"
    assert result["actual"]["rows"] == [{"sales": 50}]
    assert len(result["actual"]["executions"]) == 2
    second_call = result["actual"]["model_sql_calls"][1]
    assert second_call["turn"] == 2
    history_text = "\n".join(
        f"{message.get('role')}: {message.get('content')}"
        for message in second_call["history"]
    )
    assert "查询 2025 年 1 月销售额" in history_text
    assert "42" in history_text

    second_round_model_events = [
        event
        for event in trace
        if event["case_id"] == "CONV-01"
        and event["source"] == "model"
        and event["round"] == 2
    ]
    assert second_round_model_events
    event_history = json.dumps(
        second_round_model_events[0]["history"], ensure_ascii=False
    )
    assert "查询 2025 年 1 月销售额" in event_history
    assert "42" in event_history


def test_rel_03_stops_after_two_timeouts(cases, target, fixture_sql):
    case = next(item for item in cases if item["case_id"] == "REL-03")
    result, trace = _run_one(case, target, fixture_sql)

    assert result["verdict"] == "PASS"
    executions = result["actual"]["executions"]
    assert [item["status"] for item in executions] == ["error", "error"]
    assert len(executions) == 2
    assert len(result["actual"]["model_sql_calls"]) == 2
    assert "超时" in (result["actual"]["sql_error"] or "")
    assert "查询执行失败" in result["actual"]["answer"]

    port_events = [
        event
        for event in trace
        if event["case_id"] == "REL-03" and event["source"] == "sql_port"
    ]
    assert len(port_events) == 2
    assert [event["status"] for event in port_events] == ["error", "error"]
    assert [event["failure"] for event in port_events] == ["TimeoutError", "TimeoutError"]


def test_sec_04_blocks_private_table_before_executor(cases, target, fixture_sql):
    case = next(item for item in cases if item["case_id"] == "SEC-04")
    result, trace = _run_one(case, target, fixture_sql)

    assert result["verdict"] == "PASS"
    assert result["actual"]["executions"] == []
    assert len(result["actual"]["model_sql_calls"]) == 2
    marker = case["forbidden_marker"]
    assert marker not in json.dumps(result["actual"], ensure_ascii=False)

    port_events = [
        event
        for event in trace
        if event["case_id"] == "SEC-04" and event["source"] == "sql_port"
    ]
    assert port_events == []
    validation_errors = [
        step
        for step in result["actual"]["agent_steps"]
        if step["name"] == "validate_sql" and step["status"] == "error"
    ]
    assert len(validation_errors) == 2


def test_conv_04_isolates_users_sharing_thread(cases, target, fixture_sql):
    case = next(item for item in cases if item["case_id"] == "CONV-04")
    result, _ = _run_one(case, target, fixture_sql)

    assert result["verdict"] == "PASS"
    assert result["actual"]["rows"] == [{"sales": 50}]
    all_history = json.dumps(
        [call["history"] for call in result["actual"]["model_sql_calls"]],
        ensure_ascii=False,
    )
    assert "SYNTHETIC_USER_MARKER_A" not in all_history
