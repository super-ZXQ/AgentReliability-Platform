"""只读 SQLite 执行端口：先记录调用，再执行 SQL，支持注入故障。"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .trace import TraceCollector


@dataclass
class SqlInvocation:
    sql: str
    status: str = "called"
    error: str | None = None
    row_count: int | None = None
    duration_ms: int = 0


@dataclass
class SqlitePort:
    fixture_sql: str
    trace: TraceCollector | None = None
    case_id: str = ""
    trial: int = 1
    round_no: int = 1
    failure_script: list[str] = field(default_factory=list)
    invocations: list[SqlInvocation] = field(default_factory=list)
    _connection: sqlite3.Connection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(self.fixture_sql)
        # 种子写入完成后再开启只读，防止被测 Agent 的越权写操作落地。
        connection.execute("PRAGMA query_only=ON")
        self._connection = connection

    @property
    def schema_text(self) -> str:
        rows = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        return ";\n".join(row["sql"] for row in rows if row["sql"]) + ";"

    def close(self) -> None:
        self._connection.close()

    def _open_event(self, sql: str) -> dict[str, Any] | None:
        if self.trace is None:
            return None
        return self.trace.open(
            case_id=self.case_id,
            trial=self.trial,
            round_no=self.round_no,
            source="sql_port",
            step="execute_sql",
            summary="进入 SQL 执行端口",
        )

    async def execute(self, sql: str) -> list[dict[str, Any]]:
        invocation = SqlInvocation(sql=sql)
        self.invocations.append(invocation)
        event = self._open_event(sql)
        started = time.perf_counter()
        failure = (
            self.failure_script[len(self.invocations) - 1]
            if len(self.invocations) - 1 < len(self.failure_script)
            else None
        )
        if failure:
            invocation.status = "error"
            invocation.error = failure
            invocation.duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            if self.trace is not None and event is not None:
                self.trace.close(
                    event,
                    "error",
                    f"SQL 执行失败：{failure}",
                    invocation.duration_ms,
                    sql=sql,
                    extra={"failure": failure},
                )
            if failure == "TimeoutError":
                raise TimeoutError("SQL 执行超时")
            if failure == "OperationalError":
                raise sqlite3.OperationalError("注入的执行错误")
            raise RuntimeError(f"注入的端口故障：{failure}")

        try:
            cursor = self._connection.execute(sql)
            rows = [{key: row[key] for key in row.keys()} for row in cursor.fetchall()]
            invocation.status = "success"
            invocation.row_count = len(rows)
            invocation.duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            if self.trace is not None and event is not None:
                self.trace.close(
                    event,
                    "success",
                    f"SQL 执行成功，返回 {len(rows)} 行",
                    invocation.duration_ms,
                    sql=sql,
                )
            return rows
        except Exception as exc:
            invocation.status = "error"
            invocation.error = type(exc).__name__
            invocation.duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            if self.trace is not None and event is not None:
                self.trace.close(
                    event,
                    "error",
                    f"SQL 执行失败：{type(exc).__name__}",
                    invocation.duration_ms,
                    sql=sql,
                )
            raise
