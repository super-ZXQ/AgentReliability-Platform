"""按调用顺序注入 SQL 的离线模型桩。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .trace import TraceCollector


@dataclass
class SqlModelCall:
    index: int
    previous_error: str | None
    sql: str | None = None
    error: str | None = None
    turn: int = 1
    history: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ScriptedModel:
    """只接收执行所需输入；按脚本顺序返回 SQL 或抛出可控异常。"""

    sql_script: list[Any]
    answer: str
    trace: TraceCollector | None = None
    case_id: str = ""
    trial: int = 1
    round_no: int = 1
    sql_calls: list[SqlModelCall] = field(default_factory=list)

    async def generate_sql(
        self,
        query: str,
        schema: str,
        history: list[dict[str, str]],
        sources: list[Any],
        previous_error: str | None,
    ):
        from agent_core.models import ModelResponse

        index = len(self.sql_calls) + 1
        received_history = [dict(message) for message in history]
        event = None
        if self.trace is not None:
            event = self.trace.open(
                case_id=self.case_id,
                trial=self.trial,
                round_no=self.round_no,
                source="model",
                step="generate_sql",
                summary=f"第 {self.round_no} 轮第 {index} 次 SQL 生成请求",
            )
        try:
            entry = self.sql_script[index - 1]
        except IndexError as exc:
            self.sql_calls.append(
                SqlModelCall(
                    index=index,
                    previous_error=previous_error,
                    error="ScriptExhausted",
                    turn=self.round_no,
                    history=received_history,
                )
            )
            if self.trace is not None and event is not None:
                self.trace.close(event, "error", "模型桩脚本已耗尽")
            raise RuntimeError("模型桩脚本已耗尽") from exc

        if isinstance(entry, dict) and "raise" in entry:
            error_name = str(entry["raise"])
            self.sql_calls.append(
                SqlModelCall(
                    index=index,
                    previous_error=previous_error,
                    error=error_name,
                    turn=self.round_no,
                    history=received_history,
                )
            )
            if self.trace is not None and event is not None:
                self.trace.close(event, "error", f"模型桩注入异常：{error_name}")
            raise RuntimeError(f"模型桩注入异常：{error_name}")

        sql = str(entry)
        self.sql_calls.append(
            SqlModelCall(
                index=index,
                previous_error=previous_error,
                sql=sql,
                turn=self.round_no,
                history=received_history,
            )
        )
        if self.trace is not None and event is not None:
            self.trace.close(
                event,
                "success",
                "返回 SQL 候选",
                sql=sql,
                extra={
                    "previous_error": previous_error,
                    "history": received_history,
                },
            )
        return ModelResponse(content=sql)

    async def generate_answer(
        self,
        query: str,
        intent: str,
        history: list[dict[str, str]],
        sources: list[Any],
        sql: str | None,
        rows: list[dict[str, Any]],
    ):
        from agent_core.models import ModelResponse

        return ModelResponse(content=self.answer)
