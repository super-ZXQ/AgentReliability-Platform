"""CLI 入口：离线运行、HTML 报告、基线与候选对比。"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .compare import NOT_COMPARABLE, compare_reports, load_report, write_comparison
from .html_report import render_run_html, write_html
from .live import (
    LIVE_CASE_IDS,
    build_live_report,
    not_run_live_results,
    resolve_live_config,
    run_live_cases_sync,
    select_live_cases,
)
from .oracle import ERROR, FAIL, NOT_RUN
from .report import build_report, write_artifacts
from .runner import load_cases, not_run_results, run_cases_sync
from .target import TargetError, verify_target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentreliability")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="运行离线或真实模型用例并生成报告")
    run_parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    run_parser.add_argument("--target-dir", required=True, help="固定提交的目标 checkout")
    run_parser.add_argument("--out", required=True, help="产物目录")
    run_parser.add_argument(
        "--target-lock", default="target.lock.json", help="目标版本锁文件"
    )
    run_parser.add_argument(
        "--cases", default="benchmarks/commerce/cases.jsonl", help="用例文件"
    )
    run_parser.add_argument(
        "--fixture", default="benchmarks/commerce/fixture.sql", help="合成数据种子"
    )
    run_parser.add_argument(
        "--case", dest="case_id", default=None, help="只运行指定用例，用于复现"
    )
    run_parser.add_argument(
        "--demo-fault",
        choices=["disable-sql-retry"],
        default=None,
        help="注入演示变体，仅用于离线门禁红绿演示",
    )
    run_parser.add_argument(
        "--allow-network",
        action="store_true",
        help="真实模型模式必须显式开启联网",
    )
    run_parser.add_argument(
        "--repeat", type=int, default=1, help="真实模型模式每条用例的重复次数"
    )

    compare_parser = subparsers.add_parser(
        "compare", help="比较基线与候选报告并生成对比产物"
    )
    compare_parser.add_argument("baseline", help="基线 report.json")
    compare_parser.add_argument("candidate", help="候选 report.json")
    compare_parser.add_argument("--out", required=True, help="对比产物目录")
    return parser


def _command(raw_argv: list[str]) -> str:
    return "uv run --locked agentreliability " + shlex.join(raw_argv)


def _exit_code(results: list[dict]) -> int:
    if any(result["verdict"] in (ERROR, NOT_RUN) for result in results):
        return 2
    if any(result["verdict"] == FAIL for result in results):
        return 1
    return 0


def _run(args: argparse.Namespace, raw_argv: list[str]) -> int:
    if args.repeat != 1:
        print("离线模式仅支持 --repeat 1", file=sys.stderr)
        return 2
    if args.allow_network:
        print("离线模式不接受 --allow-network", file=sys.stderr)
        return 2
    cases_path = Path(args.cases)
    fixture_path = Path(args.fixture)
    out_dir = Path(args.out)
    lock_path = Path(args.target_lock)
    command = _command(raw_argv)

    if not cases_path.exists():
        print(f"用例文件不存在：{cases_path}", file=sys.stderr)
        return 2
    if not fixture_path.exists():
        print(f"数据种子文件不存在：{fixture_path}", file=sys.stderr)
        return 2
    if not lock_path.exists():
        print(f"目标锁文件不存在：{lock_path}", file=sys.stderr)
        return 2

    try:
        cases = load_cases(cases_path)
    except (OSError, ValueError) as exc:
        print(f"用例加载失败：{exc}", file=sys.stderr)
        return 2
    if not cases:
        print("用例文件为空", file=sys.stderr)
        return 2

    selection = None
    if args.case_id:
        cases = [case for case in cases if case["case_id"] == args.case_id]
        if not cases:
            print(f"用例不存在：{args.case_id}", file=sys.stderr)
            return 2
        selection = [args.case_id]

    run_id = uuid4().hex
    started_at = datetime.now(timezone.utc)

    try:
        target = verify_target(Path(args.target_dir), lock_path)
    except TargetError as exc:
        results = not_run_results(cases, str(exc))
        report = build_report(
            run_id=run_id,
            mode=args.mode,
            target=None,
            cases_path=cases_path,
            fixture_path=fixture_path,
            results=results,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            command=command,
            variant=args.demo_fault,
            selection=selection,
            error=str(exc),
        )
        report_path, trace_path = write_artifacts(out_dir, report, [])
        html_path = write_html(out_dir / "report.html", render_run_html(report, []))
        print(f"目标 checkout 不可用：{exc}", file=sys.stderr)
        print(f"报告：{report_path}")
        print(f"轨迹：{trace_path}")
        print(f"HTML：{html_path}")
        return 2

    fixture_sql = fixture_path.read_text(encoding="utf-8")
    results, trace_events = run_cases_sync(
        cases,
        target=target,
        fixture_sql=fixture_sql,
        run_id=run_id,
        variant=args.demo_fault,
    )
    report = build_report(
        run_id=run_id,
        mode=args.mode,
        target={
            "repository": target.repository,
            "commit": target.commit,
            "path": str(target.path),
        },
        cases_path=cases_path,
        fixture_path=fixture_path,
        results=results,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        command=command,
        variant=args.demo_fault,
        selection=selection,
    )
    report_path, trace_path = write_artifacts(out_dir, report, trace_events)
    html_path = write_html(
        out_dir / "report.html", render_run_html(report, trace_events)
    )

    for result in results:
        print(f"{result['verdict']} {result['case_id']} ({result['duration_ms']} ms)")
    summary = report["summary"]
    print(
        f"{summary['verdict']} 总计 {summary['total']}，"
        f"通过 {summary['pass']}，失败 {summary['fail']}，"
        f"错误 {summary['error']}，未执行 {summary['not_run']}"
    )
    print(f"报告：{report_path}")
    print(f"轨迹：{trace_path}")
    print(f"HTML：{html_path}")
    return _exit_code(results)


def _emit_live_not_run(
    *,
    args: argparse.Namespace,
    raw_argv: list[str],
    cases: list[dict],
    requested: list[str],
    repeat: int,
    target,
    missing: list[str],
    reason: str,
) -> int:
    run_id = uuid4().hex
    started_at = datetime.now(timezone.utc)
    results = not_run_live_results(cases, repeat, reason)
    report = build_live_report(
        run_id=run_id,
        target=target,
        cases_path=Path(args.cases),
        fixture_path=Path(args.fixture),
        results=results,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        command=_command(raw_argv),
        repeat=repeat,
        requested_cases=requested,
        config=None,
        missing=missing,
        reason=reason,
    )
    report_path, trace_path = write_artifacts(Path(args.out), report, [])
    html_path = write_html(Path(args.out) / "report.html", render_run_html(report, []))
    print(f"NOT_RUN 总计 {len(results)}：{reason}", file=sys.stderr)
    print(f"报告：{report_path}")
    print(f"轨迹：{trace_path}")
    print(f"HTML：{html_path}")
    return 2


def _run_live(args: argparse.Namespace, raw_argv: list[str]) -> int:
    if args.repeat < 1:
        print("--repeat 必须大于等于 1", file=sys.stderr)
        return 2
    if args.demo_fault:
        print("演示变体仅用于离线模式", file=sys.stderr)
        return 2

    cases_path = Path(args.cases)
    fixture_path = Path(args.fixture)
    lock_path = Path(args.target_lock)
    out_dir = Path(args.out)

    if not cases_path.exists():
        print(f"用例文件不存在：{cases_path}", file=sys.stderr)
        return 2
    if not fixture_path.exists():
        print(f"数据种子文件不存在：{fixture_path}", file=sys.stderr)
        return 2
    if not lock_path.exists():
        print(f"目标锁文件不存在：{lock_path}", file=sys.stderr)
        return 2

    try:
        cases = load_cases(cases_path)
    except (OSError, ValueError) as exc:
        print(f"用例加载失败：{exc}", file=sys.stderr)
        return 2

    live_cases = select_live_cases(cases)
    missing_cases = [
        case_id
        for case_id in LIVE_CASE_IDS
        if case_id not in {case["case_id"] for case in live_cases}
    ]
    if missing_cases:
        print(f"真实模式缺少用例：{'、'.join(missing_cases)}", file=sys.stderr)
        return 2

    if args.case_id:
        if args.case_id not in LIVE_CASE_IDS:
            print(
                f"真实模式不支持用例：{args.case_id}；可选 {', '.join(LIVE_CASE_IDS)}",
                file=sys.stderr,
            )
            return 2
        live_cases = [case for case in live_cases if case["case_id"] == args.case_id]
        requested = [args.case_id]
    else:
        requested = list(LIVE_CASE_IDS)

    try:
        target = verify_target(Path(args.target_dir), lock_path)
    except TargetError as exc:
        return _emit_live_not_run(
            args=args,
            raw_argv=raw_argv,
            cases=live_cases,
            requested=requested,
            repeat=args.repeat,
            target=None,
            missing=[],
            reason=f"目标 checkout 不可用：{exc}",
        )

    if not args.allow_network:
        return _emit_live_not_run(
            args=args,
            raw_argv=raw_argv,
            cases=live_cases,
            requested=requested,
            repeat=args.repeat,
            target=target,
            missing=[],
            reason="未显式传入 --allow-network，已拒绝联网",
        )

    config, missing_env = resolve_live_config(os.environ)
    if config is None:
        return _emit_live_not_run(
            args=args,
            raw_argv=raw_argv,
            cases=live_cases,
            requested=requested,
            repeat=args.repeat,
            target=target,
            missing=missing_env,
            reason=f"缺少环境变量：{'、'.join(missing_env)}",
        )

    run_id = uuid4().hex
    started_at = datetime.now(timezone.utc)
    results, trace_events = run_live_cases_sync(
        live_cases,
        target=target,
        fixture_sql=fixture_path.read_text(encoding="utf-8"),
        run_id=run_id,
        repeat=args.repeat,
        config=config,
    )
    report = build_live_report(
        run_id=run_id,
        target=target,
        cases_path=cases_path,
        fixture_path=fixture_path,
        results=results,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        command=_command(raw_argv),
        repeat=args.repeat,
        requested_cases=requested,
        config=config,
        missing=[],
    )
    report_path, trace_path = write_artifacts(out_dir, report, trace_events)
    html_path = write_html(
        out_dir / "report.html", render_run_html(report, trace_events)
    )

    for result in results:
        print(
            f"{result['verdict']} {result['case_id']} trial={result['trial']} "
            f"({result['duration_ms']} ms)"
        )
    summary = report["summary"]
    print(
        f"{summary['verdict']} 总计 {summary['total']}，"
        f"通过 {summary['pass']}，失败 {summary['fail']}，"
        f"错误 {summary['error']}，未执行 {summary['not_run']}"
    )
    print(
        f"模型 {config.model}（{config.provider_host or '未知主机'}），"
        f"每条用例 {args.repeat} 次，共 {len(results)} 次任务"
    )
    print(f"报告：{report_path}")
    print(f"轨迹：{trace_path}")
    print(f"HTML：{html_path}")
    return _exit_code(results)


def _comparison_error(
    out_dir: Path, command: str, reasons: list[str]
) -> tuple[int, Path, Path]:
    comparison = {
        "format_version": "1.0",
        "comparison_version": "1.0",
        "command": command,
        "baseline": {},
        "candidate": {},
        "comparable": False,
        "reasons": reasons,
        "summary": {
            "total": 0,
            "new_failures": 0,
            "fixed": 0,
            "environment_errors": 0,
            "missing": 0,
            "unchanged_failures": 0,
            "unchanged_pass": 0,
            "verdict": NOT_COMPARABLE,
        },
        "cases": [],
    }
    json_path, html_path = write_comparison(out_dir, comparison)
    return 2, json_path, html_path


def _run_compare(args: argparse.Namespace, raw_argv: list[str]) -> int:
    out_dir = Path(args.out)
    command = _command(raw_argv)
    reasons: list[str] = []
    try:
        baseline = load_report(Path(args.baseline))
    except ValueError as exc:
        reasons.append(f"基线不可用：{exc}")
        baseline = None
    try:
        candidate = load_report(Path(args.candidate))
    except ValueError as exc:
        reasons.append(f"候选不可用：{exc}")
        candidate = None

    if reasons or baseline is None or candidate is None:
        code, json_path, html_path = _comparison_error(out_dir, command, reasons)
        print(f"无法比较：{'；'.join(reasons)}", file=sys.stderr)
        print(f"对比 JSON：{json_path}")
        print(f"对比 HTML：{html_path}")
        return code

    comparison = compare_reports(baseline, candidate, command=command)
    json_path, html_path = write_comparison(out_dir, comparison)
    summary = comparison["summary"]
    print(
        f"{summary['verdict']} 总计 {summary['total']}，"
        f"新增失败 {summary['new_failures']}，恢复 {summary['fixed']}，"
        f"环境错误 {summary['environment_errors']}，缺失 {summary['missing']}，"
        f"未变失败 {summary['unchanged_failures']}"
    )
    for case_id in comparison["new_failures"]:
        print(f"新增失败 {case_id}")
    for reason in comparison.get("reasons") or []:
        print(f"不可比原因：{reason}", file=sys.stderr)
    print(f"对比 JSON：{json_path}")
    print(f"对比 HTML：{html_path}")

    verdict = comparison["summary"]["verdict"]
    if verdict == NOT_COMPARABLE:
        return 2
    if verdict == FAIL:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    try:
        if args.command == "run":
            if args.mode == "live":
                return _run_live(args, raw_argv)
            return _run(args, raw_argv)
        if args.command == "compare":
            return _run_compare(args, raw_argv)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 必须返回稳定退出码
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
