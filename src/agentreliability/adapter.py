"""从固定 checkout 装配真实 AgentRuntime，并注入离线依赖。"""

from __future__ import annotations

from typing import Any

from .db import SqlitePort
from .model_stub import ScriptedModel
from .target import TargetInfo, import_agent_runtime


def new_conversations(target: TargetInfo):
    """在目标 checkout 可导入后创建独立会话存储。"""
    import_agent_runtime(target)
    from agent_core.session import MemoryConversationStore

    return MemoryConversationStore()


def build_runtime(
    *,
    target: TargetInfo,
    sql_port: SqlitePort,
    model: ScriptedModel,
    conversations: Any | None = None,
    demo_fault: str | None = None,
):
    runtime_module = import_agent_runtime(target)
    if conversations is None:
        from agent_core.session import MemoryConversationStore

        conversations = MemoryConversationStore()
    runtime_class = runtime_module.AgentRuntime
    if demo_fault == "disable-sql-retry":
        # 仅在演示变体中覆盖执行后的分支，失败直接进入结果合成。
        class DemoFaultRuntime(runtime_class):
            @staticmethod
            def _after_execution(state):
                return "answer"

        runtime_class = DemoFaultRuntime
    elif demo_fault:
        raise ValueError(f"未知演示变体：{demo_fault}")
    AgentRuntime = runtime_class

    async def retriever(query: str) -> list[Any]:
        return []

    async def schema_loader() -> str:
        return sql_port.schema_text

    async def sql_executor(sql: str) -> list[dict[str, Any]]:
        return await sql_port.execute(sql)

    async def answer_generator(
        query: str,
        intent: str,
        history: list[dict[str, str]],
        sources: list[Any],
        sql: str | None,
        rows: list[dict[str, Any]],
    ):
        return await model.generate_answer(query, intent, history, sources, sql, rows)

    return AgentRuntime(
        retriever=retriever,
        schema_loader=schema_loader,
        sql_generator=model.generate_sql,
        sql_executor=sql_executor,
        answer_generator=answer_generator,
        conversations=conversations,
        sql_timeout_seconds=10,
        sql_dialect="sqlite",
    )
