"""v1.8.0 端点：知识库检索 / 学习画像 / 行情搜索记录 / 应用偏好。

路由层只做参数校验与转发，业务逻辑在 finresearch.knowledge.service（agent 工具复用同一套服务）。
"""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..app_settings import load_app_settings
from ..knowledge import profile as profile_store
from ..knowledge import service

router = APIRouter(prefix="/api")

_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


# ── 知识库 ───────────────────────────────────────────────
@router.get("/knowledge/search")
async def knowledge_search(q: str, limit: int = 8, ticker: str = "", kind: str = "") -> dict:
    query = (q or "").strip()
    if not query:
        return {"items": []}
    if kind and kind not in {"report", "conversation"}:
        raise HTTPException(400, "kind 仅支持 report / conversation")
    return {"items": await service.search(query[:200], limit=limit, ticker=ticker, kind=kind)}


@router.get("/knowledge/stats")
async def knowledge_stats() -> dict:
    return await service.stats()


@router.post("/knowledge/reindex")
async def knowledge_reindex() -> dict:
    n = await service.rebuild_reports()
    return {"ok": True, "indexed": n, **(await service.stats())}


# ── 行情搜索记录 ─────────────────────────────────────────
class StockViewRequest(BaseModel):
    code: str
    name: Optional[str] = ""


@router.get("/market/history")
async def market_history(limit: int = 8) -> dict:
    return await service.stock_history(limit)


@router.post("/market/history")
async def market_history_add(req: StockViewRequest) -> dict:
    code = (req.code or "").strip().upper()
    if not _CODE_RE.match(code):
        raise HTTPException(400, "code 需为 600519.SH 形式")
    await service.record_stock_view(code, (req.name or "").strip())
    return {"ok": True}


@router.delete("/market/history")
async def market_history_clear() -> dict:
    import asyncio

    # stock_history 聚合 kind IN ('stock_view', 'research')，清除记录须同步清理两类事件
    n = await asyncio.to_thread(profile_store.clear_events, ["stock_view", "research"])
    return {"ok": True, "deleted": n}


# ── 学习画像 ─────────────────────────────────────────────
class NoteRequest(BaseModel):
    text: str


@router.get("/profile/learned")
async def learned_profile() -> dict:
    import asyncio

    prefs = await load_app_settings()
    data = await asyncio.to_thread(profile_store.learned_profile)
    return {"enabled": bool(prefs.get("learn_profile", True)), **data}


@router.delete("/profile/learned")
async def learned_profile_clear() -> dict:
    import asyncio

    events = await asyncio.to_thread(profile_store.clear_events, None)
    notes = await asyncio.to_thread(profile_store.clear_notes)
    return {"ok": True, "events": events, "notes": notes}


@router.post("/profile/notes")
async def profile_note_add(req: NoteRequest) -> dict:
    note = await service.remember_note(req.text, source="user")
    if note is None:
        raise HTTPException(400, "偏好内容过短")
    return note


@router.delete("/profile/notes/{note_id}")
async def profile_note_delete(note_id: int) -> dict:
    import asyncio

    return {"ok": await asyncio.to_thread(profile_store.delete_note, note_id)}


# ── 应用偏好（读取带默认值回填；写入走 /local-settings/app-settings，落库前校验） ──
@router.get("/app-settings")
async def app_settings() -> dict:
    return await load_app_settings()
