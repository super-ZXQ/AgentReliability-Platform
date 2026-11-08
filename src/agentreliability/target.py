"""目标版本锁定、checkout 核验与真实 Agent 导入。"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TargetError(RuntimeError):
    """目标锁、checkout 或导入位置不满足运行条件。"""


@dataclass(frozen=True)
class TargetInfo:
    path: Path
    repository: str
    commit: str


def _git(target_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(target_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise TargetError(f"git {' '.join(args)} 失败：{completed.stderr.strip()}")
    return completed.stdout.strip()


def _normalize_repository(url: str) -> str:
    return url.strip().rstrip("/").removesuffix(".git")


def load_lock(path: Path) -> dict[str, str]:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TargetError(f"目标锁文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise TargetError(f"目标锁文件不是合法 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise TargetError(f"目标锁文件格式错误：{path}")
    repository = payload.get("repository")
    commit = payload.get("commit")
    if not isinstance(repository, str) or not isinstance(commit, str):
        raise TargetError("目标锁文件必须包含字符串字段 repository 与 commit")
    if len(commit) != 40:
        raise TargetError("目标锁文件的 commit 必须是完整 40 位 SHA")
    return {"repository": repository, "commit": commit}


def verify_target(target_dir: Path, lock_path: Path) -> TargetInfo:
    lock = load_lock(lock_path)
    path = Path(target_dir).resolve()
    if not (path / ".git").exists():
        raise TargetError(f"目标目录不是 git checkout：{path}")

    commit = _git(path, "rev-parse", "HEAD")
    if commit != lock["commit"]:
        raise TargetError(f"目标提交不匹配：期望 {lock['commit']}，实际 {commit}")

    remote = _git(path, "config", "--get", "remote.origin.url")
    if _normalize_repository(remote) != _normalize_repository(lock["repository"]):
        raise TargetError(f"目标仓库来源不匹配：期望 {lock['repository']}，实际 {remote}")

    dirty = _git(path, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise TargetError(f"目标工作树存在未提交改动：{dirty.splitlines()[0]}")

    return TargetInfo(path=path, repository=lock["repository"], commit=commit)


def import_agent_runtime(target: TargetInfo):
    """从已核验的 checkout 导入 agent_core.runtime，并确认模块来源。"""
    target_str = str(target.path)
    if target_str not in sys.path:
        sys.path.insert(0, target_str)
    module = importlib.import_module("agent_core.runtime")
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise TargetError("无法确认 agent_core.runtime 的导入来源")
    resolved = Path(module_file).resolve()
    if target.path not in resolved.parents:
        raise TargetError(f"agent_core 未从目标 checkout 导入：{resolved}")
    return module
