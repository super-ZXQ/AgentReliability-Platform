#!/usr/bin/env python3
"""GitHub Actions agent-regression：基准锁与候选锁分别运行并对比。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agentreliability.target import TargetError, load_lock

LOCK_PATH = "target.lock.json"


class ConfigError(RuntimeError):
    """基准锁、候选锁或目标提交不满足 CI 配置要求。"""


def _run(
    command: list[str], cwd: Path, log_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command, cwd=str(cwd.resolve()), capture_output=True, text=True, check=False
    )
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"$ {' '.join(command)}\n\nSTDOUT:\n{completed.stdout}\n"
            f"STDERR:\n{completed.stderr}\n",
            encoding="utf-8",
        )
    return completed


def _parse_lock_text(text: str, source: str) -> dict[str, str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{source} 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"{source} 必须是对象")
    repository = payload.get("repository")
    commit = payload.get("commit")
    if not isinstance(repository, str) or not isinstance(commit, str):
        raise ConfigError(f"{source} 缺少 repository 或 commit")
    if len(commit) != 40:
        raise ConfigError(f"{source} 的 commit 不是完整 40 位 SHA")
    return {"repository": repository, "commit": commit}


def read_base_lock(repo_root: Path, base_sha: str) -> dict[str, str]:
    completed = _run(
        ["git", "-C", str(repo_root), "show", f"{base_sha}:{LOCK_PATH}"],
        repo_root,
    )
    if completed.returncode != 0:
        raise ConfigError(f"基准提交 {base_sha} 缺少 {LOCK_PATH}")
    return _parse_lock_text(completed.stdout, f"{base_sha}:{LOCK_PATH}")


def read_candidate_lock(repo_root: Path) -> dict[str, str]:
    path = repo_root / LOCK_PATH
    if not path.exists():
        raise ConfigError(f"候选工作树缺少 {LOCK_PATH}")
    try:
        return load_lock(path)
    except TargetError as exc:
        raise ConfigError(str(exc)) from exc


def validate_candidate_report(
    report: dict[str, Any], expected_ids: list[str]
) -> list[str]:
    errors: list[str] = []
    cases = report.get("cases")
    if not isinstance(cases, list):
        return ["候选报告缺少用例结果"]
    case_ids = [str(case.get("case_id")) for case in cases]
    if len(case_ids) != len(set(case_ids)):
        errors.append("候选报告存在重复用例 ID")
    missing = sorted(set(expected_ids) - set(case_ids))
    extra = sorted(set(case_ids) - set(expected_ids))
    if missing:
        errors.append(f"候选报告缺失用例：{', '.join(missing)}")
    if extra:
        errors.append(f"候选报告多出用例：{', '.join(extra)}")
    if len(expected_ids) != 20 or len(set(expected_ids)) != 20:
        errors.append("用例矩阵不是 20 个唯一 ID")
    if len(case_ids) != 20:
        errors.append(f"候选报告用例数为 {len(case_ids)}，不是 20")
    return errors


def _clone_upstream(repository: str, work_dir: Path, log_path: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    upstream = work_dir / "upstream"
    if upstream.exists():
        shutil.rmtree(upstream)
    completed = _run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            repository,
            str(upstream),
        ],
        work_dir,
        log_path,
    )
    if completed.returncode != 0:
        raise ConfigError(f"克隆目标仓库失败：{completed.stderr.strip()}")
    return upstream


def _add_worktree(upstream: Path, commit: str, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    exists = _run(["git", "-C", str(upstream), "cat-file", "-e", f"{commit}^{{commit}}"], upstream)
    if exists.returncode != 0:
        raise ConfigError(f"目标提交不存在：{commit}")
    ancestor = _run(
        ["git", "-C", str(upstream), "merge-base", "--is-ancestor", commit, "origin/main"],
        upstream,
    )
    if ancestor.returncode != 0:
        raise ConfigError(f"目标提交不在上游 main 历史中：{commit}")
    added = _run(
        ["git", "-C", str(upstream), "worktree", "add", "--detach", str(dest), commit],
        upstream,
    )
    if added.returncode != 0:
        raise ConfigError(f"检出目标提交失败：{added.stderr.strip()}")
    return dest


def prepare_targets(
    repository: str, locks: dict[str, dict[str, str]], work_dir: Path, log_path: Path
) -> dict[str, Path]:
    upstream = _clone_upstream(repository, work_dir, log_path)
    return {
        role: _add_worktree(upstream, lock["commit"], work_dir / role)
        for role, lock in locks.items()
    }


def _run_suite(
    repo_root: Path,
    target_dir: Path,
    lock_path: Path,
    out_dir: Path,
    cases_path: Path,
    fixture_path: Path,
    log_path: Path,
    demo_fault: str | None = None,
) -> int:
    command = [
        sys.executable,
        "-m",
        "agentreliability",
        "run",
        "--mode",
        "offline",
        "--target-dir",
        str(target_dir),
        "--target-lock",
        str(lock_path),
        "--out",
        str(out_dir),
        "--cases",
        str(cases_path),
        "--fixture",
        str(fixture_path),
    ]
    if demo_fault:
        command.extend(["--demo-fault", demo_fault])
    return _run(command, repo_root, log_path).returncode


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _config_error(out_dir: Path, args: argparse.Namespace, reason: str) -> int:
    _write_json(
        out_dir / "config-error.json",
        {
            "status": "configuration_error",
            "exit_code": 2,
            "base_sha": args.base_sha,
            "reason": reason,
        },
    )
    print(f"配置错误：{reason}", file=sys.stderr)
    return 2


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--out", default="artifacts/ci")
    parser.add_argument("--cases", default="benchmarks/commerce/cases.jsonl")
    parser.add_argument("--fixture", default="benchmarks/commerce/fixture.sql")
    parser.add_argument("--lock-path", default=LOCK_PATH)
    parser.add_argument(
        "--candidate-demo-fault",
        choices=["disable-sql-retry"],
        default=None,
        help="仅用于演示 PR：给候选运行注入演示变体",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        baseline_lock = read_base_lock(repo_root, args.base_sha)
        candidate_lock = read_candidate_lock(repo_root)
    except ConfigError as exc:
        return _config_error(out_dir, args, str(exc))
    if baseline_lock["repository"] != candidate_lock["repository"]:
        return _config_error(out_dir, args, "基准锁与候选锁的目标仓库不一致")

    cases_path = Path(args.cases).resolve()
    fixture_path = Path(args.fixture).resolve()
    try:
        from agentreliability.runner import load_cases

        expected_ids = [str(case["case_id"]) for case in load_cases(cases_path)]
    except (OSError, ValueError) as exc:
        return _config_error(out_dir, args, f"用例矩阵不可用：{exc}")

    baseline_lock_path = out_dir / "baseline-target.lock.json"
    candidate_lock_path = out_dir / "candidate-target.lock.json"
    _write_json(baseline_lock_path, baseline_lock)
    _write_json(candidate_lock_path, candidate_lock)

    try:
        targets = prepare_targets(
            baseline_lock["repository"],
            {"baseline": baseline_lock, "candidate": candidate_lock},
            out_dir / "targets",
            out_dir / "clone.log",
        )
    except ConfigError as exc:
        return _config_error(out_dir, args, str(exc))

    baseline_code = _run_suite(
        repo_root,
        targets["baseline"],
        baseline_lock_path,
        out_dir / "baseline",
        cases_path,
        fixture_path,
        out_dir / "baseline.log",
    )
    candidate_code = _run_suite(
        repo_root,
        targets["candidate"],
        candidate_lock_path,
        out_dir / "candidate",
        cases_path,
        fixture_path,
        out_dir / "candidate.log",
        demo_fault=args.candidate_demo_fault,
    )

    baseline_report_path = out_dir / "baseline" / "report.json"
    candidate_report_path = out_dir / "candidate" / "report.json"
    errors: list[str] = []
    candidate_report: dict[str, Any] | None = None
    try:
        candidate_report = json.loads(candidate_report_path.read_text(encoding="utf-8"))
        errors.extend(validate_candidate_report(candidate_report, expected_ids))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"候选报告不可用：{exc}")

    comparison_code = 2
    if baseline_report_path.exists() and candidate_report_path.exists():
        comparison_code = _run(
            [
                sys.executable,
                "-m",
                "agentreliability",
                "compare",
                str(baseline_report_path),
                str(candidate_report_path),
                "--out",
                str(out_dir / "comparison"),
            ],
            repo_root,
            out_dir / "compare.log",
        ).returncode
    else:
        errors.append("缺少基线或候选报告，无法比较")

    if errors or candidate_code != 0 or comparison_code != 0:
        status = "fail"
        exit_code = 1
    else:
        status = "pass"
        exit_code = 0

    summary = {
        "status": status,
        "exit_code": exit_code,
        "base_sha": args.base_sha,
        "baseline": {
            "lock": baseline_lock,
            "exit_code": baseline_code,
            "report": str(baseline_report_path),
        },
        "candidate": {
            "lock": candidate_lock,
            "exit_code": candidate_code,
            "report": str(candidate_report_path),
            "case_ids": (
                [case.get("case_id") for case in candidate_report.get("cases", [])]
                if candidate_report
                else []
            ),
        },
        "comparison": {
            "exit_code": comparison_code,
            "json": str(out_dir / "comparison" / "comparison.json"),
            "html": str(out_dir / "comparison" / "comparison.html"),
        },
        "errors": errors,
    }
    _write_json(out_dir / "ci-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
