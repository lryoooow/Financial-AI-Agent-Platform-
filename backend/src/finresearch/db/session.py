"""异步引擎与 session（懒加载 + 容错）。

psycopg 单驱动：与 LangGraph AsyncPostgresSaver 共用同一 DATABASE_URL。

关键设计：**不在导入时创建引擎**。本机 Python 3.14 可能缺 psycopg 的 pq 后端
（psycopg_binary/psycopg_c/libpq），若导入即 create_async_engine 会让
`import finresearch.main` 直接 ImportError。这里改为首次访问时才创建（PEP 562），
且全程 try/except：psycopg/PG 不可用 → engine/maker = None，DB 相关请求优雅跳过，
实时端点（akshare）与 LLM 端点照常工作。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from ..config import resolve_db_url

logger = logging.getLogger(__name__)

_engine = None  # AsyncEngine | None
_maker = None   # async_sessionmaker | None
_tried = False


def _ensure() -> None:
    """首次访问时创建引擎（幂等、容错）。失败则保持 None。

    支持 SQLite（aiosqlite，桌面默认）与 PostgreSQL（psycopg，开发用）。
    """
    global _engine, _maker, _tried
    if _tried:
        return
    _tried = True
    try:
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )

        url = resolve_db_url()
        kwargs: dict = {"future": True}
        is_sqlite = url.startswith("sqlite")
        if is_sqlite:
            # SQLite：跨线程 + 单文件。timeout=30 是 aiosqlite→sqlite3 的 busy_timeout（秒）：
            # 主库引擎与 LangGraph 检查点（AsyncSqliteSaver）访问同一个 finresearch.db，
            # 对话每轮既写聊天记录（本引擎 commit）又写 agent 线程状态（检查点），两者会撞
            # SQLite 写锁。默认 busy_timeout 太短时，聊几轮后偶发 `database is locked` 直接
            # 抛在 send_chat_message 的 commit 上 → HTTP 500（正是"有时报错、特别是聊几轮后"）。
            # 30s 让写操作等锁释放（本机锁通常毫秒级即散），而不是立即失败。
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs["pool_pre_ping"] = True
            kwargs["pool_size"] = 5
            kwargs["max_overflow"] = 5

        _engine = create_async_engine(url, **kwargs)

        if is_sqlite:
            # 每个新连接设 WAL（读写并发：读不再被写阻塞）+ busy_timeout（双保险）+
            # synchronous=NORMAL（WAL 下安全且更快）。WAL 是库级持久设置，但显式声明更稳。
            from sqlalchemy import event

            @event.listens_for(_engine.sync_engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _rec):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA busy_timeout=30000")
                    cur.execute("PRAGMA synchronous=NORMAL")
                finally:
                    cur.close()

        _maker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
        logger.info("DB 引擎已就绪：%s", url)
    except Exception as e:  # noqa: BLE001
        logger.warning("DB 引擎创建失败，DB 功能降级为内存：%s", e)
        _engine = None
        _maker = None


# PEP 562：仅在外部访问 engine / AsyncSessionLocal 时才触发创建；导入时不连库
def __getattr__(name: str):
    if name == "engine":
        _ensure()
        return _engine
    if name == "AsyncSessionLocal":
        _ensure()
        return _maker
    raise AttributeError(name)


async def _add_column_if_missing(conn, table: str, column: str, ddl_type: str) -> None:
    """加列迁移（SQLite/PG 都支持 ALTER TABLE ADD COLUMN；幂等）。表不存在时跳过（由 create_all 负责）。"""
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import NoSuchTableError, OperationalError

    def _has(sync_conn) -> bool:
        try:
            insp = inspect(sync_conn)
            return column in {c["name"] for c in insp.get_columns(table)}
        except (NoSuchTableError, OperationalError):
            return False  # 表尚未创建，create_all 会处理

    try:
        exists = await conn.run_sync(_has)
        if not exists:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
            logger.info("迁移：已为 %s 添加列 %s。", table, column)
    except (NoSuchTableError, OperationalError) as e:  # noqa: BLE001
        logger.debug("跳过迁移 %s.%s（表可能尚未创建）：%s", table, column, e)


async def init_db() -> None:
    """lifespan 启动时建表（幂等、容错）+ 加列迁移。"""
    _ensure()
    if _engine is None:
        return
    try:
        from sqlalchemy import text

        from .models import Base  # 延迟导入避免循环

        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # 加列迁移：research_task.llm_config（前端所选模型，SSE GET 无法带 body，落库持久化）
            await _add_column_if_missing(conn, "research_task", "llm_config", "JSON")
            await _add_column_if_missing(conn, "chat_conversation", "archived_at", "DATETIME")
            # 1.2.0：研究任务可恢复字段（阶段 / 阶段事件流 / 各阶段耗时 / 关联报告 id）
            await _add_column_if_missing(conn, "research_task", "stage", "VARCHAR(32)")
            await _add_column_if_missing(conn, "research_task", "progress_events", "JSON")
            await _add_column_if_missing(conn, "research_task", "timings", "JSON")
            await _add_column_if_missing(conn, "research_task", "report_id", "VARCHAR(64)")
            # 公共任务成功态统一为 succeeded；SSE 终止事件仍保留 type=done。
            await conn.execute(text("UPDATE research_task SET status='succeeded' WHERE status='done'"))
            await conn.execute(text("UPDATE research_task SET stage='succeeded' WHERE stage='done'"))
        logger.info("DB 表已就绪（create_all + 迁移）。")
    except Exception as e:  # noqa: BLE001
        logger.warning("init_db 失败（PG 可能未起）：%s", e)


async def db_ping() -> bool:
    """健康检查：能否连上 PG。"""
    _ensure()
    if _engine is None:
        return False
    try:
        from sqlalchemy import text

        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


@asynccontextmanager
async def session_scope() -> AsyncIterator[Optional[AsyncSession_t]]:
    """yield 一个 AsyncSession；DB 不可用时 yield None（调用方需判断）。"""
    _ensure()
    if _maker is None:
        yield None
        return
    async with _maker() as s:
        yield s


# 仅用于类型注解（避免导入期依赖 sqlalchemy 的公开符号时循环）
try:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession as AsyncSession_t  # type: ignore
except Exception:  # pragma: no cover  # noqa: BLE001
    AsyncSession_t = object  # type: ignore
