"""SQLite-backed watchlists and confirmed imports."""

import asyncio
import math
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from ..agents._utils import _timed_call
from ..core.watchlist_import import import_preview, verify_symbol
from ..db.models import WatchlistGroup, WatchlistItem
from ..db.session import session_scope
from ..tools.batch_radar import batch_radar
from ..tools.market import get_realtime_quote

router = APIRouter(prefix="/api/watchlist")
_WRITE = asyncio.Lock()
_QUOTES = asyncio.Semaphore(6)


class WatchlistChange(BaseModel):
    kind: Literal["group", "item", "order"] = "item"
    id: str = Field(default="", max_length=64)
    group_id: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=64)
    symbol: str = Field(default="", max_length=16)
    symbols: list[str] = Field(default_factory=list, max_length=50)
    ids: list[str] = Field(default_factory=list, max_length=500)
    note: str | None = Field(default=None, max_length=200)
    sort_order: int | None = Field(default=None, ge=0, le=10_000)


class PreviewRequest(BaseModel):
    mode: Literal["text", "table", "image"]
    payload: str = Field(max_length=17_000_000)
    filename: str = Field(default="", max_length=255)
    llm: dict | None = None


class RadarRequest(BaseModel):
    group_id: str = Field(default="", max_length=64)
    symbols: list[str] = Field(default_factory=list, max_length=50)


def _database(session):
    if session is None:
        raise HTTPException(503, "本地数据库不可用。")
    return session


async def _groups(session) -> list[WatchlistGroup]:
    return list((await session.scalars(select(WatchlistGroup).order_by(WatchlistGroup.sort_order, WatchlistGroup.id))).all())


async def _items(session, group_id: str = "") -> list[WatchlistItem]:
    stmt = select(WatchlistItem).order_by(WatchlistItem.sort_order, WatchlistItem.id)
    if group_id:
        stmt = stmt.where(WatchlistItem.group_id == group_id)
    return list((await session.scalars(stmt)).all())


async def ensure_default_watchlist() -> None:
    async with _WRITE, session_scope() as session:
        if session is not None and not await _groups(session):
            session.add(WatchlistGroup(name="核心自选"))
            await session.commit()


def _item(row: WatchlistItem) -> dict:
    return {key: getattr(row, key) for key in (
        "id", "group_id", "symbol", "name", "market", "added_price", "note", "sort_order", "created_at",
    )}


async def group_symbols(group_id: str = "") -> list[str]:
    async with session_scope() as session:
        rows = await _items(_database(session), group_id)
        return list(dict.fromkeys(row.symbol for row in rows))


async def _quote(symbol: str) -> dict:
    async with _QUOTES:
        quote, _, _ = await _timed_call(get_realtime_quote, symbol, timeout=7)
        return quote or {}


def checked_symbols(values: list[str]) -> dict[str, str]:
    checked = {}
    for raw in values:
        try:
            entry = verify_symbol(raw.strip())
        except ValueError as exc:
            raise HTTPException(422, f"{raw[:16]}：{exc}") from exc
        checked[entry["code"]] = entry["name"]
    return checked


@router.get("")
async def get_watchlist() -> dict:
    await ensure_default_watchlist()
    async with session_scope() as session:
        groups = await _groups(_database(session))
        items = await _items(session)
        return {"groups": [{
            "id": group.id, "name": group.name, "sort_order": group.sort_order, "created_at": group.created_at,
            "items": [_item(item) for item in items if item.group_id == group.id],
        } for group in groups]}


@router.get("/quotes")
async def watchlist_quotes(group_id: str = "") -> dict:
    from ..cache import quote_cache

    symbols = (await group_symbols(group_id))[:50]
    cache_key = "watchlist:" + ",".join(symbols)
    if (cached := quote_cache.get(cache_key)) is not None:
        return cached
    data = await asyncio.gather(*(_quote(code) for code in symbols))
    result = {"quotes": dict(zip(symbols, data))}
    quote_cache.set(cache_key, result, ttl=25)
    return result


@router.post("")
async def create_watchlist_entry(req: WatchlistChange) -> dict:
    if req.kind == "group":
        name = req.name.strip()
        if not name:
            raise HTTPException(422, "请输入分组名称。")
        async with _WRITE, session_scope() as session:
            groups = await _groups(_database(session))
            if any(g.name == name for g in groups):
                raise HTTPException(409, "已存在同名自选分组。")
            row = WatchlistGroup(name=name, sort_order=max((item.sort_order for item in groups), default=-1) + 1)
            session.add(row)
            await session.commit()
            return {"id": row.id}
    if req.kind != "item" or not req.group_id or not (req.symbols or req.symbol):
        raise HTTPException(422, "请选择分组及至少一只股票。")
    entries = checked_symbols(req.symbols or [req.symbol])
    async with session_scope() as session:
        if not await _database(session).get(WatchlistGroup, req.group_id):
            raise HTTPException(404, "分组不存在。")
        existing = {row.symbol for row in await _items(session, req.group_id)}
    candidates = [code for code in entries if code not in existing]
    prices = dict(zip(candidates, await asyncio.gather(*(_quote(code) for code in candidates))))
    async with _WRITE, session_scope() as session:
        if not await _database(session).get(WatchlistGroup, req.group_id):
            raise HTTPException(404, "分组不存在。")
        rows = await _items(session, req.group_id)
        existing = {row.symbol for row in rows}
        added = []
        for index, code in enumerate(candidates, start=max((item.sort_order for item in rows), default=-1) + 1):
            if code in existing:
                continue
            raw = prices[code].get("price")
            price = float(raw) if isinstance(raw, (int, float)) and math.isfinite(raw) and raw > 0 else None
            row = WatchlistItem(group_id=req.group_id, symbol=code, name=entries[code], market=code[-2:],
                                added_price=price, note=req.note or "", sort_order=index)
            session.add(row)
            added.append(row)
        await session.commit()
        return {"items": [_item(row) for row in added], "duplicates": len(entries) - len(added)}


@router.patch("")
async def update_watchlist_entry(req: WatchlistChange) -> dict:
    async with _WRITE, session_scope() as session:
        _database(session)
        if req.kind == "order":
            rows = await _items(session, req.group_id) if req.group_id else await _groups(session)
            by_id = {row.id: row for row in rows}
            if len(req.ids) != len(by_id) or set(req.ids) != by_id.keys():
                raise HTTPException(409, "排序列表已变动，请刷新后重试。")
            for index, row_id in enumerate(req.ids):
                by_id[row_id].sort_order = index
        else:
            row = await session.get(WatchlistGroup if req.kind == "group" else WatchlistItem, req.id)
            if row is None:
                raise HTTPException(404, "自选条目不存在。")
            if req.kind == "group" and req.name.strip():
                new_name = req.name.strip()
                groups = await _groups(session)
                if any(g.name == new_name and g.id != row.id for g in groups):
                    raise HTTPException(409, "已存在同名自选分组。")
                row.name = new_name
            if req.kind == "item" and req.note is not None:
                row.note = req.note
            if req.sort_order is not None:
                row.sort_order = req.sort_order
        await session.commit()
    return {"ok": True}


@router.delete("")
async def delete_watchlist_entry(id: str, kind: Literal["group", "item"] = "item") -> dict:
    async with _WRITE, session_scope() as session:
        row = await _database(session).get(WatchlistGroup if kind == "group" else WatchlistItem, id)
        if row is None:
            raise HTTPException(404, "自选条目不存在。")
        if kind == "group":
            if len(await _groups(session)) <= 1:
                raise HTTPException(409, "请保留至少一个自选分组。")
            await session.execute(delete(WatchlistItem).where(WatchlistItem.group_id == id))
        await session.delete(row)
        await session.commit()
    return {"ok": True}


@router.post("/import-preview")
async def watchlist_import_preview(req: PreviewRequest) -> dict:
    from .routes import _active_llm_cfg, _overlay_current_key

    config = None
    if req.mode == "image":
        config = await (_overlay_current_key(req.llm) if req.llm else _active_llm_cfg())
    try:
        return await import_preview(req.mode, req.payload, req.filename, config)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/radar")
async def watchlist_radar(req: RadarRequest) -> dict:
    codes = list(checked_symbols(req.symbols)) if req.symbols else await group_symbols(req.group_id)
    try:
        return await batch_radar(codes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
