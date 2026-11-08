"""M1 测试公共夹具。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentreliability.target import TargetInfo, verify_target


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def target() -> TargetInfo:
    checkout = Path(
        os.environ.get("AGENTRELIABILITY_TARGET_DIR", REPO_ROOT / ".targets" / "commerce")
    )
    if not (checkout / ".git").exists():
        pytest.skip("目标 checkout 未准备，请按 README 创建 .targets/commerce")
    return verify_target(checkout, REPO_ROOT / "target.lock.json")


@pytest.fixture(scope="session")
def fixture_sql(repo_root: Path) -> str:
    return (repo_root / "benchmarks" / "commerce" / "fixture.sql").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def cases(repo_root: Path) -> list[dict]:
    from agentreliability.runner import load_cases

    return load_cases(repo_root / "benchmarks" / "commerce" / "cases.jsonl")
