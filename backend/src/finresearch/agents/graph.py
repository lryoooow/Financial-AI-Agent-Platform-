"""检查点（checkpointer）的构建与生命周期。

原先这里还有固定管线的 StateGraph 组装（START → entity → research → market → summary →
report → END）。管线已由 deepagents 取代（agents/deep/research_agent.py），
那五个 worker 没有作废——它们是报告质量的骨架，现在由 research_tools 的
prefetch_basics / compose_report 直接调用，取数与撰写逻辑一行未改，
换掉的只是"谁来决定取什么数"。

本模块现在只负责检查点，对话链路与研究链路共用：
长连 AsyncConnection + AsyncPostgresSaver(conn=...)，thread_id=会话/任务 id。
关键：不能写成 `AsyncPostgresSaver.from_conn_string(...).setup()`——
`from_conn_string` 返回的是 async context manager，不能那样用。
PG/psycopg 不可用 → 降级 MemorySaver（家用单进程足够，重启丢中间状态）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from langgraph.checkpoint.memory import MemorySaver

from ..config import settings, resolve_db_url

logger = logging.getLogger(__name__)

# 全局检查点器（lifespan 初始化 / 关闭）
_pg_conn: Any = None
_checkpointer: Any = None
_checkpointer_kind: str = "memory"


async def init_checkpointer() -> Tuple[Any, str]:
    """按 DB 方言建检查点：SQLite→AsyncSqliteSaver / PG→AsyncPostgresSaver；
    任一不可用降级 MemorySaver。返回 (checkpointer, kind)。"""
    global _pg_conn, _checkpointer, _checkpointer_kind
    url = resolve_db_url()
    try:
        if url.startswith("sqlite"):
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            path = url.split(":///", 1)[-1] if ":///" in url else url.split("://", 1)[-1]
            # timeout=30：检查点与主库引擎共用 finresearch.db，对话每轮两边都写、会撞 SQLite
            # 写锁。给足 busy_timeout 让写操作等锁散去，而不是立即抛 `database is locked`
            # （那会冒泡成对话 500，尤其聊几轮后写频繁时）。
            _pg_conn = await aiosqlite.connect(path, timeout=30)
            # WAL（读写并发）+ busy_timeout（毫秒）+ synchronous=NORMAL（WAL 下安全更快）
            await _pg_conn.execute("PRAGMA journal_mode=WAL")
            await _pg_conn.execute("PRAGMA busy_timeout=30000")
            await _pg_conn.execute("PRAGMA synchronous=NORMAL")
            saver = AsyncSqliteSaver(conn=_pg_conn)
            await saver.setup()
            _checkpointer = saver
            _checkpointer_kind = "sqlite"
        elif url.startswith("postgres"):
            from psycopg import AsyncConnection
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            conninfo = (
                url.replace("postgresql+psycopg_async://", "postgresql://")
                .replace("postgresql+psycopg://", "postgresql://")
            )
            _pg_conn = await AsyncConnection.connect(
                conninfo=conninfo, autocommit=True, prepare_threshold=0
            )
            saver = AsyncPostgresSaver(conn=_pg_conn)
            await saver.setup()
            _checkpointer = saver
            _checkpointer_kind = "postgres"
        else:
            raise ValueError(f"不支持的检查点 DB: {url}")
        logger.info("检查点就绪：%s", _checkpointer_kind)
    except Exception as e:  # noqa: BLE001
        logger.warning("检查点不可用，降级 MemorySaver：%s", e)
        await _close_conn()
        _checkpointer = MemorySaver()
        _checkpointer_kind = "memory"
    return _checkpointer, _checkpointer_kind


async def _close_conn() -> None:
    global _pg_conn
    if _pg_conn is not None:
        try:
            await _pg_conn.close()
        except Exception:  # noqa: BLE001
            pass
        _pg_conn = None


async def close_checkpointer() -> None:
    """lifespan 关闭时调用。"""
    await _close_conn()


def get_checkpointer() -> Tuple[Any, str]:
    return _checkpointer, _checkpointer_kind




