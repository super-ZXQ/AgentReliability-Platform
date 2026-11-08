"""CLI 入口：离线运行真实 Agent 工作流并写出报告。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .oracle import ERROR, FAIL, NOT_RUN, PASS
from .report import build_report, write_artifacts
from .runner import load_cases, not_run_results, run_cases_sync
from .target import TargetError, verify_target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentreliability")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="运行离线用例并生成报告")
    run_parser.add_argument("--mode", choices=["offline"], default="offline")
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
    return parser


def _exit_code(results: list[dict]) -> int:
    if any(result["verdict"] in (ERROR, NOT_RUN) for result in results):
        return 2
    if any(result["verdict"] == FAIL for result in results):
        return 1
    return 0


def _run(args: argparse.Namespace) -> int:
    cases_path = Path(args.cases)
    fixture_path = Path(args.fixture)
    out_dir = Path(args.out)
    lock_path = Path(args.target_lock)

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
            error=str(exc),
        )
        report_path, trace_path = write_artifacts(out_dir, report, [])
        print(f"目标 checkout 不可用：{exc}", file=sys.stderr)
        print(f"报告：{report_path}")
        print(f"轨迹：{trace_path}")
        return 2

    fixture_sql = fixture_path.read_text(encoding="utf-8")
    results, trace_events = run_cases_sync(
        cases,
        target=target,
        fixture_sql=fixture_sql,
        run_id=run_id,
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
    )
    report_path, trace_path = write_artifacts(out_dir, report, trace_events)

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
    return _exit_code(results)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 - CLI 必须返回稳定退出码
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
