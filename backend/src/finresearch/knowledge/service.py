"""知识库 / 画像的异步门面（路由与 agent 工具共用同一套业务服务）。

底层是同步 sqlite3，统一经 asyncio.to_thread 调用，不阻塞事件循环；
所有入口吞掉异常只记日志 —— 知识库是增强能力，失败绝不拖垮研究或对话主流程。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, Optional

from . import profile, store
from ..core.rag import embeddings, memory_judge

logger = logging.getLogger(__name__)

# 本轮用户原话（对话路由在流式开始前设置；remember_user_preference 据此校验笔记出自用户本人）
CURRENT_USER_TEXT: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "kb_current_user_text", default=None
)
_REBUILD_LOCK = asyncio.Lock()
_MEMORY_TASKS: set[asyncio.Task] = set()
_DELETED_REPORTS: set[str] = set()


def learn_from_user_text(text: str) -> None:
    async def save():
        try:
            await memory_judge.judge(text)
        except Exception:
            logger.debug("长期记忆写入失败")

    task = asyncio.create_task(save())
    _MEMORY_TASKS.add(task)
    task.add_done_callback(_MEMORY_TASKS.discard)


async def init_and_backfill() -> int:
    async with _REBUILD_LOCK:
        return await _init_and_backfill()


async def _init_and_backfill() -> int:
    """启动时建库并把存量报告补入知识库（已入库的跳过，已删除的清理）。返回新入库数。"""
    ok = await asyncio.to_thread(store.init)
    if not ok:
        return 0
    try:
        from sqlalchemy import select

        from ..db.models import ChatConversation, Report
        from ..db.session import session_scope

        done = await asyncio.to_thread(store.indexed_refs, "report")
        pending: list[tuple[str, dict]] = []
        valid_reports: set[str] = set()
        valid_convs: set[str] = set()
        async with session_scope() as s:
            if s is None:
                return 0
            rows = (await s.execute(select(Report.id, Report.payload))).all()
            for rid, payload in rows:
                valid_reports.add(rid)
                if rid not in done and isinstance(payload, dict):
                    pending.append((rid, payload))
            conv_rows = (await s.execute(select(ChatConversation.id))).all()
            valid_convs = {cid for (cid,) in conv_rows if cid}
        await asyncio.to_thread(store.prune_missing_refs, "report", valid_reports)
        await asyncio.to_thread(store.prune_missing_refs, "conversation", valid_convs)
        n = 0
        for rid, payload in pending:
            if await asyncio.to_thread(store.index_report, rid, payload):
                n += 1
        if n:
            logger.info("知识库回填：新增 %d 份历史报告。", n)
        return n
    except Exception as e:  # noqa: BLE001
        logger.warning("知识库回填失败：%s", e)
        return 0


async def rebuild_reports() -> int:
    """设置页「重建索引」：加锁串行清空报告文档后从业务库全量重建。"""
    async with _REBUILD_LOCK:
        await asyncio.to_thread(store.clear, "report")
        return await _init_and_backfill()


async def on_report_saved(report_id: str, doc: dict, *, code: str, template: str) -> None:
    """报告落库后的统一钩子：入知识库 +（开启学习画像时）记研究行为。"""
    async with _REBUILD_LOCK:
        if report_id in _DELETED_REPORTS:
            return
        await asyncio.to_thread(store.index_report, report_id, doc)
    try:
        from ..app_settings import load_app_settings

        prefs = await load_app_settings()
        if not prefs.get("learn_profile", True):
            return
    except Exception:  # noqa: BLE001
        pass
    name = str((doc or {}).get("name") or "")
    ticker = str((doc or {}).get("ticker") or code or "")
    await asyncio.to_thread(profile.record_event, "research", ticker, name)
    await asyncio.to_thread(profile.record_event, "template", template, store.TEMPLATE_LABELS.get(template, template))


async def on_report_deleted(report_id: str) -> None:
    async with _REBUILD_LOCK:
        _DELETED_REPORTS.add(report_id)
        if len(_DELETED_REPORTS) > 512:
            _DELETED_REPORTS.pop()
        await asyncio.to_thread(store.delete_by_ref, "report", report_id)


async def search(query: str, *, limit: int = 6, ticker: str = "", kind: str = "") -> list[dict[str, Any]]:
    try:
        limit = min(20, max(1, int(limit)))
        depth = min(20, limit * 3) if embeddings.enabled() else limit
        hits = await asyncio.to_thread(store.search, query, limit=depth, ticker=ticker, kind=kind)
        return (await embeddings.rerank(query, hits))[:limit]
    except Exception as e:  # noqa: BLE001
        logger.warning("知识库检索失败：%s", e)
        return []


async def stats() -> dict[str, Any]:
    try:
        return await asyncio.to_thread(store.stats)
    except Exception as e:  # noqa: BLE001
        logger.warning("知识库统计失败：%s", e)
        return {"reports": 0, "conversations": 0, "chunks": 0, "updated_at": ""}


async def record_stock_view(code: str, name: str = "") -> None:
    from ..app_settings import load_app_settings

    if (await load_app_settings()).get("learn_profile", True):
        await asyncio.to_thread(profile.record_event, "stock_view", code, name)


async def record_chat_topics(text: str) -> list[str]:
    try:
        return await asyncio.to_thread(profile.record_chat_topics, text)
    except Exception:  # noqa: BLE001
        return []


async def stock_history(limit: int = 8) -> dict[str, list[dict[str, Any]]]:
    return await asyncio.to_thread(profile.stock_history, limit)


async def learned_block(enabled: bool = True) -> str:
    if not enabled:
        return ""
    try:
        learned, memories = await asyncio.gather(
            asyncio.to_thread(profile.render_learned_block), asyncio.to_thread(memory_judge.render_memories),
        )
        return "\n".join(part for part in (learned, memories) if part)
    except Exception as e:  # noqa: BLE001
        logger.warning("渲染学习画像失败：%s", e)
        return ""


async def remember_note(text: str, source: str = "model") -> Optional[dict[str, Any]]:
    """记偏好笔记。source=model 时必须通过防注入校验（出自用户本轮原话、无指令式措辞）。"""
    if source != "user":
        from ..core.rag.memory_judge import classify

        current = CURRENT_USER_TEXT.get() or ""
        decision = classify(current)
        if decision is None or not profile.note_is_acceptable(text, decision.content):
            logger.info("拒绝未由用户直接表达的偏好笔记")
            return None
    return await asyncio.to_thread(profile.add_note, text, source)


async def index_conversation_summary(conversation_id: str, title: str, summary: str, generation: int) -> None:
    await asyncio.to_thread(store.index_conversation_summary, conversation_id, title, summary, generation)


async def on_conversation_deleted(conversation_id: str) -> None:
    await asyncio.to_thread(store.delete_by_ref, "conversation", conversation_id)
