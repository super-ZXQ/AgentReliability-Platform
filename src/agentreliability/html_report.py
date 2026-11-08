"""零依赖静态 HTML：单次运行报告与基线候选对比报告。"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .oracle import ERROR, FAIL, NOT_RUN, PASS

_CSS = """
:root { --bg:#f6f7f9; --card:#fff; --ink:#1f2937; --muted:#6b7280;
  --line:#e5e7eb; --pass:#0f766e; --fail:#b91c1c; --error:#b45309; --notrun:#6b7280; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif; }
main { max-width:1180px; margin:0 auto; padding:32px 24px 64px; }
h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:18px; margin:32px 0 12px; }
h3 { font-size:15px; margin:20px 0 8px; }
.muted { color:var(--muted); } .meta { display:grid; gap:4px; margin:16px 0;
  padding:16px; background:var(--card); border:1px solid var(--line); border-radius:10px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; }
.card b { display:block; font-size:20px; } .card span { color:var(--muted); font-size:12px; }
table { width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid var(--line); border-radius:10px; overflow:hidden; }
th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { background:#f3f4f6; font-weight:600; } tr:last-child td { border-bottom:0; }
.badge { display:inline-block; padding:1px 8px; border-radius:999px; color:#fff; font-size:12px; }
.pass { background:var(--pass); } .fail { background:var(--fail); }
.error { background:var(--error); } .not_run { background:var(--notrun); }
pre { margin:0; padding:12px; background:#f9fafb; border:1px solid var(--line);
  border-radius:8px; overflow:auto; white-space:pre-wrap; word-break:break-word; }
details { margin:10px 0; background:var(--card); border:1px solid var(--line); border-radius:10px; }
details > summary { cursor:pointer; padding:10px 12px; font-weight:600; }
details > div { padding:0 12px 12px; }
.repro { margin-top:8px; } code { background:#f3f4f6; padding:1px 4px; border-radius:4px; }
.warn { background:#fffbeb; border:1px solid #fcd34d; }
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _json(value: Any) -> str:
    return _e(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _badge(verdict: str) -> str:
    css = {
        PASS: "pass",
        FAIL: "fail",
        ERROR: "error",
        NOT_RUN: "not_run",
        "NOT_COMPARABLE": "error",
    }.get(verdict, "not_run")
    return f'<span class="badge {css}">{_e(verdict)}</span>'


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{_e(title)}</title><style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>\n"
    )


def _cards(summary: dict[str, Any]) -> str:
    items = [
        ("总计", summary.get("total", 0)),
        ("通过", summary.get("pass", 0)),
        ("失败", summary.get("fail", 0)),
        ("错误", summary.get("error", 0)),
        ("未执行", summary.get("not_run", 0)),
    ]
    cards = "".join(
        f'<div class="card"><b>{_e(value)}</b><span>{_e(label)}</span></div>'
        for label, value in items
    )
    verdict = str(summary.get("verdict", NOT_RUN))
    return f'<div class="cards">{cards}<div class="card"><b>{_badge(verdict)}</b><span>结论</span></div></div>'


def _meta_rows(report: dict[str, Any]) -> str:
    target = report.get("target") or {}
    inputs = report.get("inputs") or {}
    live = report.get("live") or {}
    rows = [
        ("run_id", report.get("run_id")),
        ("运行模式", report.get("mode")),
        ("变体", report.get("variant") or "默认（无注入）"),
        ("用例选择", report.get("selection") or "完整套件"),
    ]
    if live:
        model = live.get("model") or {}
        environment = live.get("environment") or {}
        rows.extend(
            [
                ("模型", model.get("name") or "未配置"),
                ("提供方主机", model.get("provider_host") or "未配置"),
                ("重复次数", live.get("repeat")),
                ("环境缺失", "、".join(environment.get("missing") or []) or "无"),
                ("未执行原因", environment.get("reason") or "无"),
            ]
        )
    rows.extend(
        [
            ("目标仓库", target.get("repository")),
            ("目标提交", target.get("commit")),
            ("判定器版本", report.get("oracle_version")),
            ("用例 SHA-256", inputs.get("cases_sha256")),
            ("数据 SHA-256", inputs.get("fixture_sha256")),
            ("开始时间", report.get("started_at")),
            ("结束时间", report.get("finished_at")),
        ]
    )
    return "".join(
        f"<div><span class=\"muted\">{_e(label)}：</span><code>{_e(value)}</code></div>"
        for label, value in rows
    )


def _case_summary_table(summaries: list[dict[str, Any]]) -> str:
    if not summaries:
        return ""
    rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('case_id'))}</td>"
        f"<td>{_e(item.get('trials'))}</td>"
        f"<td>{_e(item.get('pass'))}</td>"
        f"<td>{_e(item.get('fail'))}</td>"
        f"<td>{_e(item.get('error'))}</td>"
        f"<td>{_e(item.get('not_run'))}</td>"
        f"<td>{_e((item.get('latency_ms') or {}).get('average'))}</td>"
        f"<td>{_e((item.get('latency_ms') or {}).get('max'))}</td>"
        f"<td>{_e(item.get('model_calls'))}</td>"
        f"<td>{_e(item.get('retries'))}</td>"
        f"<td>{_e(item.get('provider_errors'))}</td>"
        f"<td>{_e((item.get('usage') or {}).get('total_tokens'))}</td>"
        "</tr>"
        for item in summaries
    )
    return (
        "<table><thead><tr><th>用例</th><th>试验</th><th>通过</th><th>失败</th>"
        "<th>错误</th><th>未执行</th><th>平均耗时 ms</th><th>最大耗时 ms</th>"
        "<th>模型调用</th><th>重试</th><th>提供方错误</th><th>总 token</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _assertion_table(assertions: list[dict[str, Any]]) -> str:
    if not assertions:
        return '<p class="muted">无断言记录。</p>'
    rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('name'))}</td>"
        f"<td>{_badge(str(item.get('verdict')))}</td>"
        f"<td><pre>{_json(item.get('expected'))}</pre></td>"
        f"<td><pre>{_json(item.get('actual'))}</pre></td>"
        "</tr>"
        for item in assertions
    )
    return (
        "<table><thead><tr><th>断言</th><th>判定</th><th>预期</th><th>实际</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _trace_table(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<p class="muted">无关联轨迹事件。</p>'
    rows = "".join(
        "<tr>"
        f"<td>{_e(event.get('seq'))}</td>"
        f"<td>{_e(event.get('round'))}</td>"
        f"<td>{_e(event.get('source'))}</td>"
        f"<td>{_e(event.get('step'))}</td>"
        f"<td>{_e(event.get('status'))}</td>"
        f"<td>{_e(event.get('duration_ms'))}</td>"
        f"<td>{_e(event.get('summary'))}</td>"
        "</tr>"
        for event in events
    )
    table = (
        "<table><thead><tr><th>事件</th><th>轮次</th><th>来源</th><th>步骤</th>"
        "<th>状态</th><th>耗时 ms</th><th>摘要</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    payloads = []
    for event in events:
        payload = {
            key: event[key]
            for key in (
                "sql",
                "failure",
                "previous_error",
                "history",
                "usage",
                "provider_error",
            )
            if key in event
        }
        if payload:
            payloads.append(
                f"<details><summary>事件 {_e(event.get('seq'))} 证据载荷</summary>"
                f"<div><pre>{_json(payload)}</pre></div></details>"
            )
    return table + "".join(payloads)


def render_run_html(
    report: dict[str, Any], trace_events: list[dict[str, Any]]
) -> str:
    summary = report.get("summary") or {}
    trace_by_seq = {event.get("seq"): event for event in trace_events}
    command = str(report.get("command") or "")
    title = (
        "AgentReliability 真实模型报告"
        if report.get("mode") == "live"
        else "AgentReliability 离线报告"
    )
    summaries = report.get("case_summaries") or []
    summary_section = (
        f"<h2>每条用例汇总</h2>{_case_summary_table(summaries)}"
        if summaries
        else ""
    )
    blocks: list[str] = []
    for case in report.get("cases", []):
        case_id = str(case.get("case_id"))
        refs = list(case.get("trace_refs") or [])
        events = [trace_by_seq[ref] for ref in refs if ref in trace_by_seq]
        case_command = command
        if case_command and "--case" not in case_command:
            case_command = f"{case_command} --case {case_id}"
        repro = (
            f'<div class="repro"><span class="muted">单例复现：</span>'
            f"<code>{_e(case_command)}</code></div>"
            if case_command
            else ""
        )
        blocks.append(
            "<details open>" if case.get("verdict") != PASS else "<details>"
            f"<summary>{_badge(str(case.get('verdict')))} {_e(case_id)}"
            f"　<span class=\"muted\">{_e(case.get('duration_ms'))} ms</span></summary>"
            "<div>"
            f"{repro}"
            "<h3>断言</h3>"
            f"{_assertion_table(case.get('assertions') or [])}"
            "<h3>预期与实际</h3>"
            f"<details><summary>预期</summary><div><pre>{_json(case.get('expected'))}</pre></div></details>"
            f"<details><summary>实际</summary><div><pre>{_json(case.get('actual'))}</pre></div></details>"
            "<h3>关联轨迹</h3>"
            f"{_trace_table(events)}"
            "</div></details>"
        )

    body = (
        f"<h1>{_e(title)}</h1>"
        f"<p class=\"muted\">同一份用例与数据上的真实 AgentRuntime 执行结果。</p>"
        f"<div class=\"meta\">{_meta_rows(report)}</div>"
        f"<h2>摘要</h2>{_cards(summary)}"
        f"{summary_section}"
        f"<h2>复现命令</h2><pre>{_e(command)}</pre>"
        f"<h2>用例</h2>{''.join(blocks)}"
    )
    return _page(title, body)


def render_compare_html(comparison: dict[str, Any]) -> str:
    baseline = comparison.get("baseline") or {}
    candidate = comparison.get("candidate") or {}
    summary = comparison.get("summary") or {}
    reasons = comparison.get("reasons") or []
    comparable = comparison.get("comparable")
    reason_html = ""
    if reasons:
        items = "".join(f"<li>{_e(reason)}</li>" for reason in reasons)
        reason_html = f'<div class="warn"><h2>不可比原因</h2><ul>{items}</ul></div>'
    counts = [
        ("新增失败", summary.get("new_failures", 0)),
        ("恢复", summary.get("fixed", 0)),
        ("环境错误", summary.get("environment_errors", 0)),
        ("缺失用例", summary.get("missing", 0)),
        ("未变失败", summary.get("unchanged_failures", 0)),
        ("保持通过", summary.get("unchanged_pass", 0)),
    ]
    cards = "".join(
        f'<div class="card"><b>{_e(value)}</b><span>{_e(label)}</span></div>'
        for label, value in counts
    )
    verdict = str(summary.get("verdict", "NOT_COMPARABLE"))
    cards += f'<div class="card"><b>{_badge(verdict)}</b><span>结论</span></div>'

    meta = (
        "<div class=\"meta\">"
        f"<div>基线 run_id：<code>{_e(baseline.get('run_id'))}</code>，"
        f"提交 <code>{_e((baseline.get('target') or {}).get('commit'))}</code>，"
        f"变体 <code>{_e(baseline.get('variant') or '默认（无注入）')}</code></div>"
        f"<div>候选 run_id：<code>{_e(candidate.get('run_id'))}</code>，"
        f"提交 <code>{_e((candidate.get('target') or {}).get('commit'))}</code>，"
        f"变体 <code>{_e(candidate.get('variant') or '默认（无注入）')}</code></div>"
        f"<div>可比：<code>{_e(comparable)}</code>　复现命令：<code>{_e(comparison.get('command'))}</code></div>"
        "</div>"
    )

    rows = []
    for item in comparison.get("cases", []):
        change = str(item.get("change"))
        baseline_exec = item.get("baseline_actual_executions")
        candidate_exec = item.get("candidate_actual_executions")
        candidate_assertions = item.get("candidate_assertions") or []
        failing = [
            assertion
            for assertion in candidate_assertions
            if assertion.get("verdict") != PASS
        ]
        details = (
            "<details><summary>差异证据</summary><div>"
            f"<h3>基线执行</h3><pre>{_json(baseline_exec)}</pre>"
            f"<h3>候选执行</h3><pre>{_json(candidate_exec)}</pre>"
            f"<h3>候选失败断言</h3>{_assertion_table(failing)}"
            "</div></details>"
        )
        rows.append(
            "<tr>"
            f"<td>{_e(item.get('case_id'))}</td>"
            f"<td>{_badge(str(item.get('baseline_verdict')))}</td>"
            f"<td>{_badge(str(item.get('candidate_verdict')))}</td>"
            f"<td>{_e(change)}</td>"
            "<td>"
            f"{details}"
            "</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>用例</th><th>基线</th><th>候选</th><th>变化</th>"
        "<th>证据</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    body = (
        "<h1>AgentReliability 对比报告</h1>"
        f"{meta}{reason_html}<h2>摘要</h2><div class=\"cards\">{cards}</div>"
        f"<h2>逐用例差异</h2>{table}"
    )
    return _page("AgentReliability 对比报告", body)


def write_html(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path

