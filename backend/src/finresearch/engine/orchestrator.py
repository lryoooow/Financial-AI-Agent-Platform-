"""Research target preparation and persisted human decisions."""

import asyncio
from uuid import uuid4

from sqlalchemy import update

from ..db.models import HumanApproval
from ..db.session import session_scope
from ..tools.secrets import decrypt_config, encrypt_config
from .router import resolve_stock_target


async def prepare_research(code: str, template: str, name: str = "") -> tuple[str, str, list[dict]]:
    if template == "market_recap":
        return "market", "A 股收盘复盘", []
    if template == "industry":
        return code.strip(), name.strip() or code.strip(), []
    candidates = await asyncio.to_thread(resolve_stock_target, code)
    if len(candidates) == 1:
        return candidates[0]["code"], name.strip() or candidates[0]["name"], []
    return "", "", candidates


def new_approval(conversation_id: str, query: str, candidates: list[dict], config: dict | None) -> HumanApproval:
    return HumanApproval(
        id=uuid4().hex, conversation_id=conversation_id, status="pending",
        payload={"query": query, "candidates": candidates, "config": encrypt_config(config)},
    )


async def consume_approval(conversation_id: str, checkpoint_id: str, action: str, value: str, *, finalize: bool = True) -> dict:
    async with session_scope() as session:
        if session is None:
            raise LookupError("审批数据不可用。")
        row = await session.get(HumanApproval, checkpoint_id)
        if row is None or row.conversation_id != conversation_id:
            raise LookupError("确认请求不存在。")
        if row.status != "pending":
            raise RuntimeError("确认请求已处理。")
        candidates = row.payload["candidates"]
        symbol = value.strip().upper()
        if action == "edit":
            resolved = await asyncio.to_thread(resolve_stock_target, symbol)
            if len(resolved) != 1:
                raise ValueError("请填写可唯一确认的股票代码。")
            symbol = resolved[0]["code"]
        elif action == "approve" and symbol not in {candidate["code"] for candidate in candidates}:
            raise ValueError("请从候选标的中选择。")
        if finalize:
            result = await session.execute(
                update(HumanApproval)
                .where(HumanApproval.id == checkpoint_id, HumanApproval.status == "pending")
                .values(status="rejected" if action == "reject" else "approved")
            )
            if result.rowcount != 1:
                raise RuntimeError("确认请求已处理。")
            await session.commit()
        return {
            "symbol": symbol, "query": row.payload["query"],
            "llm": decrypt_config(row.payload.get("config")), "action": action,
        }
