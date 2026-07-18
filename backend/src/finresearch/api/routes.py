"""所有 API 端点。

逐字段对齐前端契约。所有返回带 provenance。
- 行情端点（quote/intraday/kline/book/indicators）：async 路由通过 asyncio.to_thread 调用
  同步的 akshare/直连源，**不再阻塞 asyncio 事件循环**（根因修复：之前同步 akshare 把整个 loop 卡死）。
  即使 LLM 未配置、akshare 缺失，也返回结构化结果（带 provenance.error），绝不抛 5xx。
- /intraday、/kline 返回 {data, provenance, trading_date, attempts, elapsed_ms}（含来源/失败原因/耗时）。
- /market/diagnostics 默认零网络快速返回（缓冲聚合），?refresh=1 才现场实时探测。
- /evidence/status 独立的联网证据状态（零网络，国内优先），不复用诊断。
- /research 接受可选 llm {base_url,api_key,model}（前端所选模型），落库 + 内存持久化；
  SSE 流按 task_id 取出真正使用（根因修复 #5：前后端模型配置打通）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from ..agents.deep.research_agent import build_research_agent
from ..agents.deep.research_tools import clear_task as research_clear_task, get_stored as research_get_stored
from ..agents.deep.stream import stream_agent_sse
from ..agents.graph import get_checkpointer
from ..agents.prompts import CHART_SYSTEM
from ..tools import export, now_iso
from ..tools.secrets import decrypt_config, encrypt_config
from ..tools.context import build_chart_context, recent_diagnostics as context_diagnostics
from ..tools.context_store import delete_conversation_data, media_path, offload_message_media
from ..config import settings
from ..db.models import ChatContextSummary, ChatConversation, ChatDelta, LocalSetting, Report, ResearchTask
from ..db.session import db_ping, session_scope
from ..llm import build_chart_llm, build_llm, llm_available, resolved_chart_model
from ..tools.entity import resolve_entity
from ..tools.market import (
    get_bid_ask,
    get_indicators,
    get_intraday,
    get_kline,
    get_realtime_quote,
)
from ..tools.report import DISCLAIMER
from . import sse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

VALID_TEMPLATES = {"equity_deep", "industry", "earnings", "risk"}
# 对话事实注入的取数上界（research 节点对同样的数据源用 6s；对话更不能让首 token 挂在无界网络调用上）
_MARKET_SEMAPHORE = asyncio.Semaphore(4)


async def _market_call(fn, *args):
    async with _MARKET_SEMAPHORE:
        return await asyncio.to_thread(fn, *args)

# SSE 是 GET（EventSource 不能带 body），前端所选模型配置在此按 task_id 暂存，供 stream 取用。
# 进程内单例（家用单进程）；DB 可用时也同步落 ResearchTask.llm_config（跨重启）。
_TASK_LLM: dict[str, Optional[dict]] = {}
_TASK_LLM_CAP = 500


@dataclass
class _ResearchRuntime:
    """研究计算与任意一个 SSE 客户端解耦的进程内事件总线。"""

    task_id: str
    history: list[str] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: Optional[asyncio.Task] = None
    terminal: bool = False


_RESEARCH_RUNS: dict[str, _ResearchRuntime] = {}


async def _mark_research_interrupted(task_id: str, message: str) -> None:
    async with session_scope() as s:
        if s is None:
            return
        row = await s.get(ResearchTask, task_id)
        if row and row.status in {"pending", "running"}:
            row.status = "error"
            row.stage = "error"
            row.error = message
            await s.commit()


def _start_research_runtime(
    task_id: str, factory: Callable[[], AsyncIterator[str]]
) -> _ResearchRuntime:
    current = _RESEARCH_RUNS.get(task_id)
    if current and current.task and not current.task.done():
        return current
    runtime = _ResearchRuntime(task_id=task_id)
    _RESEARCH_RUNS[task_id] = runtime

    async def pump() -> None:
        try:
            async for frame in factory():
                runtime.history.append(frame)
                if len(runtime.history) > 600:
                    del runtime.history[: len(runtime.history) - 600]
                for queue in tuple(runtime.subscribers):
                    queue.put_nowait(frame)
        except asyncio.CancelledError:
            await _mark_research_interrupted(task_id, "研究任务因应用关闭而中断，请重新提交。")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("research runtime failed: %s", task_id)
            await _mark_research_interrupted(task_id, str(exc))
            frame = sse.ev_error(str(exc))
            runtime.history.append(frame)
            for queue in tuple(runtime.subscribers):
                queue.put_nowait(frame)
        finally:
            runtime.terminal = True
            for queue in tuple(runtime.subscribers):
                queue.put_nowait(None)

    runtime.task = asyncio.create_task(pump(), name=f"research:{task_id}")
    return runtime


def _remember_task_llm(task_id: str, llm: Optional[dict]) -> None:
    _TASK_LLM[task_id] = llm
    if len(_TASK_LLM) > _TASK_LLM_CAP:
        # 丢弃最早的（dict 保序，Python 3.7+）
        for k in list(_TASK_LLM)[: len(_TASK_LLM) - _TASK_LLM_CAP]:
            _TASK_LLM.pop(k, None)


# ────────────────────────────────────────────────────────────
# health
# ────────────────────────────────────────────────────────────
@router.get("/health")
async def health() -> dict:
    db_ok = await db_ping()
    return {
        "status": "ok",
        "db": "ok" if db_ok else "down",
        "llm": "ok" if settings.llm_configured else "unconfigured",
    }


# ────────────────────────────────────────────────────────────
# resolve（搜索联想）
# ────────────────────────────────────────────────────────────
@router.get("/resolve")
async def resolve(q: str, limit: int = 8) -> list:
    res = await asyncio.to_thread(resolve_entity, q, max(1, min(int(limit or 8), 30)))
    return res.get("results", [])


# ────────────────────────────────────────────────────────────
# 行情族（直连优先；asyncio.to_thread 不阻塞事件循环）
# ────────────────────────────────────────────────────────────
@router.get("/quote/{code}")
async def quote(code: str) -> dict:
    return await _market_call(get_realtime_quote, code)


@router.get("/quote/{code}/intraday")
async def intraday(code: str) -> dict:
    """分时：返回 {data, provenance{...,attempts[]}, trading_date, elapsed_ms}（含来源/失败原因/耗时）。"""
    return await _market_call(get_intraday, code)


@router.get("/quote/{code}/kline")
async def kline(code: str, n: int = 60, adjust: str = "qfq") -> dict:
    """日K：返回 {data, provenance{...,attempts[]}, trading_date, elapsed_ms}（含来源/失败原因/耗时）。"""
    return await _market_call(get_kline, code, n, adjust)


@router.get("/quote/{code}/book")
async def book(code: str) -> dict:
    return await _market_call(get_bid_ask, code)


@router.get("/quote/{code}/indicators")
async def indicators(code: str) -> list:
    return (await _market_call(get_indicators, code)).get("data", [])


# ────────────────────────────────────────────────────────────
# 诊断（默认零网络快速；?refresh=1 才现场探测）
# ────────────────────────────────────────────────────────────
@router.get("/market/diagnostics")
async def market_diagnostics(refresh: bool = False) -> dict:
    from ..tools.diagnostics import get_market_diagnostics, get_market_diagnostics_fast

    if refresh:
        return await _market_call(get_market_diagnostics)
    return get_market_diagnostics_fast()


# ────────────────────────────────────────────────────────────
# 联网证据状态（独立、零网络、国内优先；不复用诊断）
# ────────────────────────────────────────────────────────────
@router.get("/evidence/status")
async def evidence_status() -> dict:
    from ..tools.evidence import get_evidence_status

    return get_evidence_status()


@router.get("/context/diagnostics/{kind}/{id}")
async def context_diagnostics_route(kind: str, id: str = "") -> dict:
    """上下文压缩诊断（对话/研究）：压缩前后长度、模块边界、消息数、是否压缩、耗时。不展示密钥。"""
    if kind not in ("chat", "research"):
        raise HTTPException(400, "kind 仅支持 chat/research")
    current = context_diagnostics(limit=20, kind=kind, obj_id=(id or None))
    if current or not id:
        return {"diagnostics": current}
    async with session_scope() as s:
        row = await s.get(LocalSetting, f"context-diag:{kind}:{id}") if s is not None else None
        return {"diagnostics": list((row.payload or {}).get("items") or []) if row else []}


async def _persist_context_diagnostics(kind: str, obj_id: str, items: list[dict]) -> None:
    if not obj_id or not items:
        return
    async with session_scope() as s:
        if s is None:
            return
        key = f"context-diag:{kind}:{obj_id}"
        row = await s.get(LocalSetting, key)
        if row is None:
            row = LocalSetting(key=key)
            s.add(row)
        row.payload = {"items": items[-20:]}
        await s.commit()


# ────────────────────────────────────────────────────────────
# research（异步任务 + SSE 流；可选 llm 配置打通前后端）
# ────────────────────────────────────────────────────────────
class LocalSettingRequest(BaseModel):
    payload: Any = None


@router.get("/local-settings/{key}")
async def get_local_setting(key: str) -> dict:
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        row = await s.get(LocalSetting, key)
        return {"key": key, "payload": row.payload if row else None}


@router.put("/local-settings/{key}")
async def put_local_setting(key: str, req: LocalSettingRequest) -> dict:
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        row = await s.get(LocalSetting, key)
        if row:
            row.payload = req.payload
        else:
            s.add(LocalSetting(key=key, payload=req.payload))
        await s.commit()
    return {"ok": True, "key": key}


class ChatCreateRequest(BaseModel):
    title: Optional[str] = None
    llm: Optional[dict] = None
    snapshot_interval: int = 8


class ChatSendRequest(BaseModel):
    message: str
    llm: Optional[dict] = None
    images: list[str] = []  # base64 data URL（多模态图片，持久化到 delta payload）


class ChatArchiveRequest(BaseModel):
    archived: bool = True


async def _probe_vision(llm, images: list) -> bool:
    """含图时，在 agent 跑之前先探一次模型支不支持视觉。

    为什么必须"先探"而不是"先跑、失败再去图重试"（legacy 就是后者）：
    legacy 没有检查点，重试是免费的；agent 带 checkpointer，失败那轮的用户消息**已经写进
    thread 状态**（实测：模型调用抛错后 aget_state 里已有 1 条 HumanMessage），此时再发一条
    去图的，状态里就有两条一模一样的提问，模型会看到用户把同一个问题问了两遍。
    宁可多花一次探测调用——含图消息本就少见——也不能污染会话状态。
    """
    from langchain_core.messages import HumanMessage

    probe = HumanMessage(
        content=[
            {"type": "text", "text": "."},
            {"type": "image_url", "image_url": {"url": images[0]}},
        ]
    )
    try:
        await llm.ainvoke([probe])
        return True
    except Exception as e:  # noqa: BLE001
        if _is_vision_error(e):
            logger.info("模型不支持视觉，本轮去图降级：%s", e)
            return False
        # 非视觉错误（网络/认证）不在这里吞掉：交给正式调用去报，错误信息更完整
        logger.debug("视觉探测遇到非视觉错误，按支持处理，交由正式调用暴露：%s", e)
        return True


async def _deepagent_chat_stream(
    *,
    conv_id: str,
    text: str,
    history: list,
    profile: dict,
    llm_cfg: Optional[dict],
    images: Optional[list] = None,
    user_msg: Optional[dict] = None,
):
    """deepagents 对话链路：产出 (sse_line, text_piece)。

    取数决定权在模型手里：不再每轮预取行情塞 prompt，模型自己决定调什么工具、调几次。
    """
    from ..agents.deep.chat_agent import build_chat_agent
    from ..tools.context import _to_lc_content

    checkpointer, _kind = get_checkpointer()
    agent = build_chat_agent(llm_config=llm_cfg, profile=profile, checkpointer=checkpointer)
    if agent is None:
        return

    config = {
        "configurable": {"thread_id": conv_id},
        "recursion_limit": int(settings.agent_recursion_limit),
    }

    # 多模态：含图时先探视觉支持，不支持就去图 + 明确告知（与 legacy 的降级体验一致）。
    # 用模块级 build_llm（不要在函数里再 import 一次遮蔽它）：那样既多余，又让调用方无法替换
    # 模型来源，测试也打不了补丁。
    note = ""
    if images:
        probe_llm = build_llm(streaming=False, override=llm_cfg)
        if probe_llm is not None and not await _probe_vision(probe_llm, images):
            note = f"（当前模型不支持图片识别，已忽略 {len(images)} 张图片，仅根据文字回复。）\n\n"
            images = None

    def _content(msg: dict):
        """带图消息 → 多模态 content blocks；无图 → 纯字符串。复用既有转换，不另造。"""
        return _to_lc_content(msg) if msg.get("images") else (msg.get("content") or "")

    # agent 的历史在 checkpointer 里，UI 的历史在 DB 里，两套状态各自独立。
    # 迁移前建的老对话在 checkpointer 里没有线程状态：此时只发新消息的话，agent 会对前文
    # 一无所知——而界面上明明白白显示着历史，用户会觉得"它失忆了"。
    # 所以线程为空时用 DB 历史播种一次，之后每轮只发增量（history 末条就是本轮提问，不会重复）。
    this_turn = dict(user_msg or {"role": "user", "content": text})
    if images:
        this_turn["images"] = images
    else:
        this_turn.pop("images", None)

    try:
        state = await agent.aget_state(config)
        seeded = bool((state.values or {}).get("messages"))
    except Exception as e:  # noqa: BLE001
        logger.warning("aget_state 失败，按未播种处理：%s", e)
        seeded = False

    if seeded:
        msgs = [{"role": "user", "content": _content(this_turn)}]
    else:
        msgs = [
            {"role": m.get("role") or "user", "content": _content(m)}
            for m in (history or [])
            if (m.get("content") or "").strip() or m.get("images")
        ] or [{"role": "user", "content": _content(this_turn)}]

    if note:
        yield sse.ev_token(note), note

    async for line, piece in stream_agent_sse(agent, {"messages": msgs}, config):
        yield line, piece


def _chat_msg(role: str, content: str, images: Optional[list[str]] = None) -> dict:
    msg = {
        "id": uuid.uuid4().hex,
        "role": role,
        "content": content,
        "created_at": now_iso(),
    }
    if images:
        msg["images"] = images
    return msg


# ── 多模态消息构造与视觉降级（1.3.0） ──────────────────────────────────






def _is_vision_error(e: Exception) -> bool:
    """含图消息首个 chunk 失败时，判断是否为视觉不支持（可降级去图重试）。

    保守判定：含图上下文下，视觉关键词或 400 状态码均视为可降级。
    非视觉错误（网络/认证）降级重试也会失败、错误正常上报，不会静默吞掉。
    """
    msg = str(e).lower()
    # "图片太大"类体积问题不可通过去图降级解决（应上报，而非静默切纯文本）
    if any(kw in msg for kw in ("too large", "too long", "too big", "exceed")):
        return False
    if any(kw in msg for kw in ("image", "vision", "multimodal", "图片", "图像", "视觉",
                                 "not support", "unsupported", "不支持")):
        return True
    # OpenAI 兼容端点对不支持的输入常返回 400
    if "400" in msg or "bad request" in msg:
        return True
    return False




async def _chat_history(s, conversation_id: str) -> list[dict]:
    stmt = (
        select(ChatDelta)
        .where(ChatDelta.conversation_id == conversation_id)
        .order_by(ChatDelta.seq.asc(), ChatDelta.id.asc())
    )
    rows = list((await s.execute(stmt)).scalars().all())
    messages: list[dict] = []
    last_snapshot_seq = 0
    for row in rows:
        if row.kind == "snapshot":
            messages = list((row.payload or {}).get("messages") or [])
            last_snapshot_seq = int(row.seq or 0)
    for row in rows:
        if int(row.seq or 0) <= last_snapshot_seq:
            continue
        if row.kind == "delta":
            msg = (row.payload or {}).get("message")
            if isinstance(msg, dict):
                messages.append(msg)
    return messages


async def _chat_next_seq(s, conversation_id: str) -> int:
    stmt = (
        select(ChatDelta)
        .where(ChatDelta.conversation_id == conversation_id)
        .order_by(ChatDelta.seq.desc())
        .limit(1)
    )
    row = (await s.execute(stmt)).scalars().first()
    return int(row.seq) + 1 if row else 1


async def _chat_append(s, conv: ChatConversation, message: dict, current: list[dict]) -> list[dict]:
    seq = await _chat_next_seq(s, conv.id)
    stored_message = await asyncio.to_thread(offload_message_media, message, conv.id)
    messages = [*current, stored_message]
    # 新写入一律是真增量，避免每 N 条把完整历史再存一遍造成 O(N²) 与 base64 复制。
    # _chat_history 继续兼容读取旧 snapshot 行，已有用户数据无需迁移重写。
    kind = "delta"
    payload = {"message": stored_message}
    role = stored_message.get("role", "user")
    s.add(
        ChatDelta(
            conversation_id=conv.id,
            seq=seq,
            kind=kind,
            role=role,
            payload=payload,
        )
    )
    return messages


@router.get("/context/media/{conversation_id}/{filename}")
async def context_media(conversation_id: str, filename: str):
    """同源展示已卸载媒体；路径经过白名单与目录边界校验。"""
    try:
        path = media_path(conversation_id, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, "media not found")
    return FileResponse(path)


def _chat_meta(conv: ChatConversation, messages: Optional[list[dict]] = None) -> dict:
    return {
        "id": conv.id,
        "title": conv.title,
        "snapshot_interval": conv.snapshot_interval,
        "model_config": conv.model_config,
        "archived_at": conv.archived_at.isoformat() if conv.archived_at else None,
        "created_at": conv.created_at.isoformat() if conv.created_at else "",
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else "",
        "message_count": len(messages) if messages is not None else None,
    }


@router.get("/chat/conversations")
async def chat_conversations(archived: bool = False) -> dict:
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        stmt = select(ChatConversation)
        if archived:
            stmt = stmt.where(ChatConversation.archived_at.is_not(None))
        else:
            stmt = stmt.where(ChatConversation.archived_at.is_(None))
        stmt = stmt.order_by(ChatConversation.updated_at.desc()).limit(50)
        rows = list((await s.execute(stmt)).scalars().all())
        return {"conversations": [_chat_meta(c) for c in rows], "channel": "deltachannel"}


@router.post("/chat/conversations")
async def create_chat_conversation(req: ChatCreateRequest) -> dict:
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        conv = ChatConversation(
            title=(req.title or "新对话")[:160],
            snapshot_interval=max(2, min(int(req.snapshot_interval or 8), 50)),
            model_config=encrypt_config(req.llm),
        )
        s.add(conv)
        await s.commit()
        await s.refresh(conv)
        return {"conversation": _chat_meta(conv, []), "messages": [], "channel": "deltachannel"}


@router.put("/chat/conversations/{conversation_id}/archive")
async def archive_chat_conversation(conversation_id: str, req: ChatArchiveRequest) -> dict:
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        conv = await s.get(ChatConversation, conversation_id)
        if not conv:
            raise HTTPException(404, "conversation not found")
        conv.archived_at = dt.datetime.now(dt.UTC) if req.archived else None
        conv.updated_at = dt.datetime.now(dt.UTC)
        await s.commit()
        await s.refresh(conv)
        return {"conversation": _chat_meta(conv), "channel": "deltachannel"}


@router.delete("/chat/conversations/{conversation_id}")
async def delete_chat_conversation(conversation_id: str) -> dict:
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        conv = await s.get(ChatConversation, conversation_id)
        if not conv:
            raise HTTPException(404, "conversation not found")
        await s.execute(delete(ChatDelta).where(ChatDelta.conversation_id == conversation_id))
        await s.execute(delete(ChatContextSummary).where(ChatContextSummary.conversation_id == conversation_id))
        await s.delete(conv)
        await s.commit()
        await asyncio.to_thread(delete_conversation_data, conversation_id)
        return {"ok": True, "channel": "deltachannel"}


@router.get("/chat/conversations/{conversation_id}")
async def get_chat_conversation(conversation_id: str) -> dict:
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        conv = await s.get(ChatConversation, conversation_id)
        if not conv:
            raise HTTPException(404, "conversation not found")
        messages = await _chat_history(s, conversation_id)
        return {"conversation": _chat_meta(conv, messages), "messages": messages, "channel": "deltachannel"}


# Markdown 清洗：只删「处在语法位置」的符号，不做无差别字符删除。
#
# 旧实现最后三行是 text.replace("*","").replace("`","").replace("#","")——
# 它把 3*5 变成 35、把「排名 #1」变成「排名 1」。之所以敢这么写，是因为它被**逐 token**
# 调用：`**` 可能被切成两个 token，成对的正则永远匹配不上，只好按字符删。
#
# 正确的做法是不要逐 token 清洗：流式原样透传，前端 StructuredText 每次渲染都对
# 「已累积的完整文本」做一次防御性清洗，成对标记因此总是完整的；后端只在落库前清洗一次。
_MD_FENCE = re.compile(r"```[A-Za-z0-9_+-]*\n?")          # 代码块围栏（含语言标注）
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")              # `code` → code
_MD_BOLD = re.compile(r"\*\*(?=\S)([\s\S]+?)(?<=\S)\*\*")  # **粗** → 粗
_MD_BOLD_U = re.compile(r"__(?=\S)([\s\S]+?)(?<=\S)__")
# 斜体：两侧不能紧邻单词字符，避免吃掉 3*5 与 snake_case
_MD_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_MD_ITALIC_U = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
# 标题：行首 #，后接空格或非数字（保住「#1 排名」）
_MD_HEADING = re.compile(r"(?m)^[ \t]{0,3}#{1,6}(?:[ \t]+|(?=[^\s\d#]))")
_MD_BULLET = re.compile(r"(?m)^[ \t]*[-*+][ \t]+")        # 行首 - / * / + 列表
# 流式尾部的半截标记（连同它前面的空白一起裁掉）。不含 #，否则会吃掉结尾的「C#」
_MD_TRAILING = re.compile(r"[ \t]*(?:\*{1,2}|_{1,2}|`{1,3})[ \t]*$")


def _strip_md(text: str) -> str:
    """把 Markdown 语法转成纯文本（"杜绝 .md"），但不误伤正文里的 * # _ ` 字符。

    只在**完整文本**上调用（落库前）。逐 token 调用会让成对标记匹配不上。
    """
    if not text:
        return text
    text = _MD_FENCE.sub("", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_BOLD_U.sub(r"\1", text)
    text = _MD_ITALIC.sub(r"\1", text)
    text = _MD_ITALIC_U.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("· ", text)
    return _MD_TRAILING.sub("", text)


@router.post("/chat/conversations/{conversation_id}/messages")
async def send_chat_message(conversation_id: str, req: ChatSendRequest):
    """流式对话：POST 返回 text/event-stream。

    事件序列：stage(understanding/thinking/composing) → 多个 token → done(含 conversation/messages/reply)。
    用户消息在流式开始前持久化（独立 session 提交）；助手回复在流式结束后持久化。
    前端用 fetch + ReadableStream 解析（EventSource 仅支持 GET，无法带 llm body）。
    """
    text = (req.message or "").strip()
    if not text:
        raise HTTPException(400, "message is empty")

    # 1) 持久化用户消息（独立 session，提交后才进入流式，确保即使流式中断也保留提问）
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        conv = await s.get(ChatConversation, conversation_id)
        if not conv:
            raise HTTPException(404, "conversation not found")
        history = await _chat_history(s, conversation_id)
        user_msg = _chat_msg("user", text, images=req.images)
        history = await _chat_append(s, conv, user_msg, history)
        if req.llm is not None:
            conv.model_config = encrypt_config(req.llm)
        if conv.title == "新对话":
            conv.title = text[:36]
        conv.updated_at = dt.datetime.now(dt.UTC)
        await s.commit()
        conv_id = conv.id
        llm_cfg = req.llm or decrypt_config(conv.model_config)
        history_snapshot = list(history)
        # 数据库存引用，当前这一轮仍把原始 data URL 交给视觉模型。
        if req.images and history_snapshot:
            history_snapshot[-1] = user_msg
        # 加载本地用户画像（默认稳健研究型），供上下文治理注入 system prompt
        # or {}（而非 or None）：缺行或空 payload 时交给 render_profile_block 逐字段回填 DEFAULT_PROFILE
        prof_row = await s.get(LocalSetting, "user-profile")
        user_profile = (prof_row.payload if prof_row else None) or {}
        summary_row = await s.get(ChatContextSummary, conv_id)
        summary_snapshot = ({
            "cutoff_count": summary_row.cutoff_count,
            "summary": summary_row.summary,
            "history_path": summary_row.history_path,
        } if summary_row else None)

    # 只判"有没有配模型"，不在这里构造模型：真正要用的那个由 build_chat_agent 内部建，
    # 这里再建一个只为判空是白白多造一套 httpx 客户端。
    has_model = llm_available(llm_cfg)

    async def gen():
        nonlocal summary_snapshot
        # 统一闭环：无论是否有模型/是否出错，都最终持久化助手回复并发 done。
        reply_text = ""

        if not has_model:
            # 无模型：不是 transient error，给出可操作提示并正常闭环（发 token + 持久化 + done）
            reply_text = (
                "⚠️ 当前还没有可用模型。\n\n"
                "请在「模型配置」中保存 API Key 与模型，再回到对话继续提问。"
            )
            yield sse.ev_chat_stage("composing", "正在组织回复")
            await asyncio.sleep(0.1)
            yield sse.ev_token(reply_text)
        else:
            # deepagents 是唯一实现。原先这里还有一条 legacy 分支（detect_ticker 预取行情 +
            # build_chat_context 手写上下文 + 裸 llm.astream），已整体移除——它的每一项能力
            # 都在 agent 链路里有对应实现，且更强：
            #   detect_ticker 单标的预取  → agent 按需自己调 resolve_entity/get_realtime_quote，
            #                               且能一轮里查多个标的（"茅台和五粮液比一下"）
            #   build_chat_context 手写摘记 → SummarizationMiddleware（含历史卸载到文件系统）
            #   offload/retrieve 原文取回   → FilesystemMiddleware，agent 按需自己 read_file
            #   含图 try-with-fallback      → _probe_vision（先探再跑，见其 docstring）
            try:
                async for _line, _piece in _deepagent_chat_stream(
                    conv_id=conv_id,
                    text=text,
                    history=history_snapshot,
                    profile=user_profile,
                    llm_cfg=llm_cfg,
                    images=req.images,
                    user_msg=history_snapshot[-1] if history_snapshot else None,
                ):
                    yield _line
                    reply_text += _piece
            except Exception as e:  # noqa: BLE001
                logger.exception("对话链路失败")
                # 已流式输出的部分保留；补失败说明后照常持久化 + done（闭环）
                note = f"\n\n⚠️ 模型调用失败：{e}"
                reply_text = (reply_text + note).strip() or note.strip()
                yield sse.ev_error(str(e))
            if not reply_text:
                reply_text = "⚠️ 模型返回为空。可以换一个模型或把问题拆得更具体。"
                yield sse.ev_token(reply_text)

        # 落库前对完整文本再清洗一次（状态安全网：兜住逐 token 清洗无法处理的跨 token 拼接，如 ** 重组）
        reply_text = _strip_md(reply_text)

        # 2) 持久化助手回复（独立 session）——统一闭环
        assistant_msg = _chat_msg("assistant", reply_text)
        final_meta = None
        final_messages: list = []
        async with session_scope() as s:
            if s is not None:
                conv2 = await s.get(ChatConversation, conv_id)
                if conv2 is not None:
                    history2 = await _chat_history(s, conv_id)
                    history2 = await _chat_append(s, conv2, assistant_msg, history2)
                    await s.commit()
                    await s.refresh(conv2)
                    final_meta = _chat_meta(conv2, history2)
                    final_messages = history2
        yield sse.sse({
            "type": "done",
            "conversation": final_meta,
            "messages": final_messages,
            "reply": assistant_msg,
            "channel": "deltachannel",
        })

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ResearchRequest(BaseModel):
    code: str
    template: str = "equity_deep"
    query: Optional[str] = None
    llm: Optional[dict] = None  # {base_url, api_key, model}：前端所选模型


def _research_task_meta(t: ResearchTask) -> dict:
    return {
        "id": t.id,
        "code": t.code,
        "template": t.template,
        "query": t.query,
        "status": t.status,        # pending/running/done/error
        "stage": t.stage,          # pending/entity/research/market/report/done/error
        "report_id": t.report_id,
        "error": t.error,
        "created_at": t.created_at.isoformat() if t.created_at else "",
        "updated_at": t.updated_at.isoformat() if t.updated_at else "",
    }


# /pipeline/stages 已移除：它返回的是固定管线的五个节点名，供前端画那条写死的进度条。
# 现在研究由 agent 编排，阶段是运行时真实发生的工具调用（tool_call 事件），
# 不再有可以提前枚举的"阶段表"。前端已改为按 tool_call 实时渲染，不再调用本端点。


@router.get("/research/tasks")
async def list_research_tasks(limit: int = 50) -> dict:
    """研究任务列表（用于研究任务页恢复历史与运行中状态）。"""
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        stmt = (
            select(ResearchTask)
            .order_by(ResearchTask.updated_at.desc())
            .limit(max(1, min(int(limit or 50), 200)))
        )
        rows = list((await s.execute(stmt)).scalars().all())
        return {"tasks": [_research_task_meta(t) for t in rows]}


@router.get("/research/{task_id}")
async def get_research_task(task_id: str) -> dict:
    """单任务当前状态：阶段、阶段事件流、各阶段耗时、错误、关联报告。"""
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        t = await s.get(ResearchTask, task_id)
        if not t:
            raise HTTPException(404, "task not found")
        meta = _research_task_meta(t)
        meta["progress_events"] = t.progress_events or []
        meta["timings"] = t.timings or {}
        return meta


@router.post("/research")
async def create_research(req: ResearchRequest) -> dict:
    if req.template not in VALID_TEMPLATES:
        raise HTTPException(400, "template 仅支持 equity_deep/industry/earnings/risk")
    # 并发去重只复用本进程确实仍在运行的任务，或刚创建尚未建立 SSE 的 pending 任务。
    # 数据库里没有对应 runtime 的 running 行属于上次进程/断线遗留，必须终结，不能永久复用。
    async with session_scope() as s:
        if s is not None:
            stmt = (
                select(ResearchTask)
                .where(
                    ResearchTask.code == req.code,
                    ResearchTask.template == req.template,
                    ResearchTask.status.in_(["pending", "running"]),
                )
                .order_by(ResearchTask.created_at.desc())
                .limit(1)
            )
            existing = (await s.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                runtime = _RESEARCH_RUNS.get(existing.id)
                is_active = bool(runtime and runtime.task and not runtime.task.done())
                updated = existing.updated_at or existing.created_at
                now = dt.datetime.now(updated.tzinfo) if updated and updated.tzinfo else dt.datetime.now()
                age_s = (now - updated).total_seconds() if updated else 9999
                if is_active or (existing.status == "pending" and age_s <= 30):
                    _remember_task_llm(existing.id, req.llm or decrypt_config(existing.llm_config))
                    return {"task_id": existing.id, "reused": True}
                existing.status = "error"
                existing.stage = "error"
                existing.error = "检测到上次进程遗留任务，已终止并重新创建。"
                await s.commit()
    task_id = uuid.uuid4().hex
    _remember_task_llm(task_id, req.llm)
    async with session_scope() as s:
        if s is not None:
            s.add(
                ResearchTask(
                    id=task_id,
                    code=req.code,
                    template=req.template,
                    query=req.query,
                    status="pending",
                    llm_config=encrypt_config(req.llm),
                )
            )
            await s.commit()
    return {"task_id": task_id}


@router.get("/research/{task_id}/stream")
async def research_stream(
    request: Request,
    task_id: str,
    code: Optional[str] = None,
    template: str = "equity_deep",
    query: Optional[str] = None,
):
    """SSE 驱动固定管线。

    task 优先从 DB 取（含 llm_config）；DB 不可用/无此任务时用查询参数 + _TASK_LLM 兜底，
    保证无 DB 也能跑（前端可一并传）。
    """
    # 已结束任务直接回放终态；进程重启后遗留的 running 任务不能凭同一个 checkpoint 暗中重跑。
    terminal_frame: Optional[str] = None
    async with session_scope() as s:
        if s is not None:
            existing = await s.get(ResearchTask, task_id)
            if existing and existing.status == "done" and existing.report_id:
                saved = await s.get(Report, existing.report_id)
                if saved:
                    terminal_frame = sse.sse({
                        "type": "done", "report_id": saved.id, "report": saved.payload or {}
                    })
            elif existing and existing.status == "error":
                terminal_frame = sse.ev_error(existing.error or "研究任务失败")
            elif existing and existing.status == "running":
                runtime = _RESEARCH_RUNS.get(task_id)
                if not runtime or not runtime.task or runtime.task.done():
                    existing.status = "error"
                    existing.stage = "error"
                    existing.error = "研究任务因应用重启而中断，请重新提交。"
                    await s.commit()
                    terminal_frame = sse.ev_error(existing.error)

    if terminal_frame is not None:
        async def terminal_gen():
            yield terminal_frame
        return StreamingResponse(
            terminal_gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _persist_event(node: str, label: str, elapsed_ms: int, node_timings: dict, stage: str, merged: dict) -> None:
        """阶段事件追加进 ResearchTask.progress_events，更新 stage/timings（每节点完成时调用，供切页恢复）。"""
        merged[f"{node}_ms"] = elapsed_ms
        if node_timings:
            merged.update(node_timings)
        async with session_scope() as s:
            if s is None:
                return
            t = await s.get(ResearchTask, task_id)
            if t is None:
                return
            events = list(t.progress_events or [])
            events.append({"node": node, "label": label, "elapsed_ms": elapsed_ms, "timings": node_timings or {}, "ts": now_iso()})
            t.progress_events = events
            t.stage = stage
            t.timings = dict(merged)
            await s.commit()

    async def pipeline():
        # 解析任务参数 + LLM 配置（DB 行优先，回退内存）
        code_, tpl_, q_ = code, template, query
        llm_cfg = _TASK_LLM.get(task_id)
        merged_timings: dict = {}
        user_profile: dict = {}
        async with session_scope() as s:
            if s is not None:
                t = await s.get(ResearchTask, task_id)
                if t:
                    code_ = t.code or code_
                    tpl_ = t.template or tpl_
                    q_ = t.query or q_
                    if t.llm_config is not None:
                        llm_cfg = decrypt_config(t.llm_config)
                    t.status = "running"
                    t.stage = "running"
                    await s.commit()
                # 读一次用户画像，注入研究链路各段撰写；缺行时由 DEFAULT_PROFILE 逐字段回填
                prof_row = await s.get(LocalSetting, "user-profile")
                user_profile = (prof_row.payload if prof_row else None) or {}
        if not code_:
            yield sse.ev_error("缺少 code（DB 无此任务且未传 code 查询参数）")
            return

        yield sse.ev_tool(f"开始研究 {code_}")
        await asyncio.sleep(0)
        agent = build_research_agent(
            task_id=task_id,
            template=tpl_ or "equity_deep",
            llm_config=llm_cfg,
            profile=user_profile,
            checkpointer=get_checkpointer()[0],
        )
        if agent is None:
            yield sse.ev_error("模型未配置：请在「模型配置」中保存 API Key 与模型后重试。")
            return

        ask = f"请研究 {code_}，模板 {tpl_ or 'equity_deep'}。"
        if q_:
            ask += f"用户额外关注：{q_}"

        graph_t0 = time.perf_counter()
        last_stage_done = graph_t0
        summary_streamed = False
        try:
            # emit_tokens=False：agent 的正文是"好的，开始研究"这类过程话术，不是报告内容；
            # 报告正文由 compose_report 经 custom 事件（section/summary）另行送达。
            async for _line, _piece in stream_agent_sse(
                agent,
                {"messages": [{"role": "user", "content": ask}]},
                {
                    "configurable": {"thread_id": task_id},
                    "recursion_limit": int(settings.agent_recursion_limit),
                },
                emit_tokens=False,
            ):
                _ev = json.loads(_line[6:].strip())
                if _ev.get("type") == "summary":
                    summary_streamed = True
                elif _ev.get("type") == "tool_call" and _ev.get("status") in ("ok", "error"):
                    # progress_events 落库的新事件源：旧图是"节点完成"，现在是"工具调用完成"。
                    # 这条链路必须接上，否则 /research/{task_id} 的 progress_events 永远是空的，
                    # 切页回来就看不到已完成的阶段了（ResearchTask.progress_events 的唯一写入者）。
                    _now = time.perf_counter()
                    _elapsed = int((_now - last_stage_done) * 1000)
                    last_stage_done = _now
                    await _persist_event(
                        _ev["name"], _ev.get("label", _ev["name"]), _elapsed, {}, _ev["name"], merged_timings
                    )
                yield _line
                await asyncio.sleep(0)

            # 报告不经模型上下文，直接从工具层存储取回（见 research_tools 的设计说明）
            draft = research_get_stored(task_id, "draft_report")
            merged_timings.update(research_get_stored(task_id, "timings", {}) or {})

            if not draft:
                async with session_scope() as s:
                    if s is not None:
                        t = await s.get(ResearchTask, task_id)
                        if t:
                            t.status = "error"
                            t.stage = "error"
                            t.error = "未生成报告草稿（数据源可能不可用）"
                            await s.commit()
                yield sse.ev_error("未生成报告草稿（数据源可能不可用）")
                return

            # 1) 先落库报告 + 任务 done/report_id（done 必须在落库后发送）
            report_id = uuid.uuid4().hex
            async with session_scope() as s:
                if s is not None:
                    s.add(
                        Report(
                            id=report_id,
                            task_id=task_id,
                            ticker=draft.get("ticker", code_),
                            name=draft.get("name", ""),
                            template=tpl_ or "equity_deep",
                            summary=draft.get("summary", ""),
                            payload=draft,
                            html=draft.get("html", ""),
                            as_of=draft.get("as_of")
                            or dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                        )
                    )
                    t = await s.get(ResearchTask, task_id)
                    if t:
                        t.status = "done"
                        t.stage = "done"
                        t.report_id = report_id
                        t.timings = dict(merged_timings)
                    await s.commit()

            await _persist_context_diagnostics(
                "research",
                task_id,
                context_diagnostics(limit=20, kind="research", obj_id=task_id),
            )

            # 2) 摘要已在 summary 节点优先推送；兼容异常/旧图路径时再补发。
            summary = draft.get("summary", "") or ""
            if not summary_streamed:
                yield sse.sse({"type": "summary", "text": summary})

            yield sse.ev_tool(f"研究任务总耗时 {int((time.perf_counter() - graph_t0) * 1000)}ms")
            # 3) done（落库后），携带真实 report_id
            yield sse.sse({"type": "done", "report_id": report_id, "report": draft})
        except Exception as e:  # noqa: BLE001
            logger.exception("research_stream 失败")
            async with session_scope() as s:
                if s is not None:
                    t = await s.get(ResearchTask, task_id)
                    if t:
                        t.status = "error"
                        t.stage = "error"
                        t.error = str(e)
                    await s.commit()
            yield sse.ev_error(str(e))

    async def connection():
        runtime = _start_research_runtime(task_id, pipeline)
        queue: asyncio.Queue = asyncio.Queue()
        runtime.subscribers.add(queue)
        try:
            # 新订阅者先回放本进程内已经产生的事件，切页回来不会只剩一个转圈。
            for frame in list(runtime.history):
                yield frame
            if runtime.terminal:
                return
            while True:
                if await request.is_disconnected():
                    logger.info("research_stream 客户端断开；后台任务继续：%s", task_id)
                    return
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=10.0)
                except TimeoutError:
                    yield sse.sse({"type": "heartbeat", "task_id": task_id})
                    continue
                if frame is None:
                    return
                yield frame
        finally:
            runtime.subscribers.discard(queue)

    return StreamingResponse(
        connection(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ────────────────────────────────────────────────────────────
# reports
# ────────────────────────────────────────────────────────────
@router.get("/reports")
async def list_reports(limit: int = 100) -> dict:
    """报告库列表（从 SQLite 读取真实报告，重启不丢）。"""
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        stmt = select(Report).order_by(Report.created_at.desc()).limit(max(1, min(int(limit or 100), 500)))
        rows = list((await s.execute(stmt)).scalars().all())
        return {"reports": [{
            "id": r.id,
            "task_id": r.task_id,
            "ticker": r.ticker,
            "name": r.name or "",
            "template": r.template,
            "summary": (r.summary or "")[:300],
            "as_of": r.as_of or "",
            "created_at": r.created_at.isoformat() if r.created_at else "",
        } for r in rows]}


@router.get("/reports/{report_id}")
async def get_report(report_id: str) -> dict:
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "DB 不可用；请从研究流的 done 事件获取报告。")
        r = await s.get(Report, report_id)
        if not r:
            stmt = (
                select(Report)
                .where(Report.task_id == report_id)
                .order_by(Report.created_at.desc())
                .limit(1)
            )
            res = await s.execute(stmt)
            r = res.scalar_one_or_none()
            if not r:
                raise HTTPException(404, "report not found")
        payload = r.payload or {}
        return {
            "ticker": r.ticker,
            "name": r.name or "",
            "template": r.template,
            "summary": r.summary or "",
            "catalyst": payload.get("catalyst", ""),
            "financials": payload.get("financials", {"years": [], "rows": []}),
            "sections": payload.get("sections", []),
            "risks": payload.get("risks", []),
            "sources": payload.get("sources", []),
            "disclaimer": payload.get("disclaimer", DISCLAIMER),
            "as_of": r.as_of or "",
            "html": r.html or "",
        }


# ────────────────────────────────────────────────────────────
# 报告下载（真实 docx/pdf/txt/md 文件）
# ────────────────────────────────────────────────────────────
@router.get("/reports/{report_id}/download")
async def download_report(report_id: str, format: str = "pdf") -> Response:
    """报告真实文件下载：format=docx|pdf|txt|md。生成失败返 500，绝不返空文件。"""
    fmt = (format or "pdf").lower().strip()
    if fmt not in ("docx", "pdf", "txt", "md"):
        raise HTTPException(400, "format 仅支持 docx/pdf/txt/md")
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        r = await s.get(Report, report_id)
        if not r:
            stmt = (
                select(Report)
                .where(Report.task_id == report_id)
                .order_by(Report.created_at.desc())
                .limit(1)
            )
            r = (await s.execute(stmt)).scalar_one_or_none()
        if not r:
            raise HTTPException(404, "report not found")
        payload = r.payload or {}
        doc = {
            "ticker": r.ticker, "name": r.name or "", "template": r.template,
            "summary": r.summary or "", "catalyst": payload.get("catalyst", ""),
            "financials": payload.get("financials", {"years": [], "rows": []}),
            "sections": payload.get("sections", []), "risks": payload.get("risks", []),
            "sources": payload.get("sources", []), "disclaimer": payload.get("disclaimer", DISCLAIMER),
            "as_of": r.as_of or "",
        }
    try:
        if fmt == "txt":
            data, mime = export.render_txt(doc).encode("utf-8"), "text/plain; charset=utf-8"
        elif fmt == "md":
            data, mime = export.render_md(doc).encode("utf-8"), "text/markdown; charset=utf-8"
        elif fmt == "docx":
            data, mime = export.render_docx(doc), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        else:
            data, mime = export.render_pdf(doc), "application/pdf"
    except Exception as e:  # noqa: BLE001
        logger.exception("报告导出失败 format=%s", fmt)
        raise HTTPException(500, f"报告导出失败：{e}")
    if not data:
        raise HTTPException(500, "报告导出生成空文件")
    from urllib.parse import quote
    filename = export.safe_filename(doc, fmt)
    # HTTP 头为 latin-1：中文文件名用 RFC 5987 的 filename*=UTF-8'' 编码，并给 ASCII 兜底
    ascii_fallback = "".join(ch for ch in (doc.get("ticker") or "report") if ch.isascii() and ch not in '\\/:*?"<>|') or "report"
    cd = f"attachment; filename=\"{ascii_fallback}.{fmt}\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": cd},
    )


@router.delete("/reports/{report_id}")
async def delete_report(report_id: str) -> dict:
    """报告库真删除：物理删除报告行 + 其来源研究任务 + 该任务的检查点线程。

    是硬删除，不是软删除（不置 archived 标记）。报告内容全部落在 report 行
    （html / payload / summary），没有独立磁盘文件，删行即彻底删内容。再连带删掉
    1:1 的 research_task 与 LangGraph 检查点线程（thread_id == task_id），避免
    「研究任务」页残留一条指向已删报告的死链接、检查点表里遗留中间状态。
    """
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        rep = await s.get(Report, report_id)
        if rep is None:
            # 兼容以 task_id 调用：取该任务下最新一份报告
            stmt = (
                select(Report)
                .where(Report.task_id == report_id)
                .order_by(Report.created_at.desc())
                .limit(1)
            )
            rep = (await s.execute(stmt)).scalar_one_or_none()
        if rep is None:
            raise HTTPException(404, "report not found")
        rid = rep.id
        task_id = rep.task_id
        await s.delete(rep)  # 物理 DELETE FROM report
        if task_id:
            task = await s.get(ResearchTask, task_id)
            if task is not None:
                await s.delete(task)  # 连带删来源任务（task↔report 1:1）
        await s.commit()
    # 会话已提交、写锁已释放，再做副作用清理（这些即便失败也不该让删除回滚）
    if task_id:
        # 极少见：报告已存在通常意味任务已结束；防御性停掉仍在跑的运行时
        runtime = _RESEARCH_RUNS.pop(task_id, None)
        rt = getattr(runtime, "task", None) if runtime is not None else None
        if rt is not None and not rt.done():
            rt.cancel()
        # 删检查点线程（清中间状态，真正不留痕）；用检查点自己的连接，不占主库写锁
        cp, _kind = get_checkpointer()
        if cp is not None and hasattr(cp, "adelete_thread"):
            try:
                await cp.adelete_thread(task_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("删除检查点线程失败 task_id=%s：%s", task_id, e)
    return {"ok": True, "deleted": rid}


# ────────────────────────────────────────────────────────────
# chart/analyze（轻量单次 LLM 速读；支持前端所选模型 override）
# ────────────────────────────────────────────────────────────
class ChartPoint(BaseModel):
    time: Optional[str] = None
    date: Optional[str] = None
    price: float
    avg: Optional[float] = None
    volume: Optional[float] = None
    open: Optional[float] = None
    close: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None


class ChartAnalyzeRequest(BaseModel):
    code: str
    kind: str = Field(default="intraday")  # intraday | kline
    point: ChartPoint
    prevClose: float
    llm: Optional[dict] = None  # {base_url, api_key, model}：前端所选模型


@router.post("/chart/analyze")
async def chart_analyze(req: ChartAnalyzeRequest) -> dict:
    from ..tools import make_provenance
    from urllib.parse import urlparse

    target_host = urlparse((req.llm or {}).get("base_url") or settings.llm_base_url).netloc

    base_prov = make_provenance(
        source="llm",
        endpoint="chart_analyze",
        raw_value={"code": req.code, "kind": req.kind, "prevClose": req.prevClose},
    )

    llm = build_chart_llm(override=req.llm)
    used_model = resolved_chart_model(req.llm)
    logger.info("chart_analyze target host=%s model=%s", target_host, used_model)
    if llm is None:
        return {
            "text": "(未配置 LLM，跳过 AI 速读)",
            "provenance": {**base_prov, "host": target_host, "model": used_model, "error": "llm_unconfigured"},
        }

    p = req.point
    rel = ((p.price - req.prevClose) / req.prevClose * 100.0) if req.prevClose else 0.0
    fact_json = {
        "code": req.code,
        "kind": req.kind,
        "time": p.time or p.date or "",
        "price": p.price,
        "avg": p.avg,
        "volume": p.volume,
        "prev_close": req.prevClose,
        "rel_pct": round(rel, 2),
    }
    try:
        resp = await llm.ainvoke(
            build_chart_context(
                system_prompt=CHART_SYSTEM,
                facts_json=json.dumps(fact_json, ensure_ascii=False),
            )
        )
        text = resp.content if hasattr(resp, "content") else str(resp)
        return {
            "text": text.strip() or "(速读为空)",
            "provenance": {**base_prov, "host": target_host, "model": used_model},
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("chart_analyze LLM 失败：%s", e)
        return {
            "text": f"(AI 速读失败：{e}；相对昨收 {rel:+.2f}%)",
            "provenance": {**base_prov, "host": target_host, "model": used_model, "error": str(e)},
        }


# ────────────────────────────────────────────────────────────
# models（真·拉厂商 /v1/models，供前端"拉取最新模型"按钮）
# ────────────────────────────────────────────────────────────
class ModelsRequest(BaseModel):
    base_url: str
    api_key: str


@router.post("/models")
async def list_models(req: ModelsRequest) -> dict:
    """代理调用厂商 OpenAI 兼容 /models，返回当前可用模型 id 列表。"""
    import json as _json
    import urllib.request as _u

    def _fetch():
        url = req.base_url.rstrip("/") + "/models"
        r = _u.Request(url, headers={"Authorization": f"Bearer {req.api_key}"})
        with _u.urlopen(r, timeout=15) as resp:
            return _json.loads(resp.read().decode("utf-8"))

    from ..tools import make_provenance

    try:
        data = await asyncio.to_thread(_fetch)
        items = data.get("data", data) if isinstance(data, dict) else data
        ids = [
            {"id": m.get("id", ""), "label": m.get("id", "")}
            for m in items
            if isinstance(m, dict) and m.get("id")
        ]
        return {"models": ids, "provenance": make_provenance(source="vendor", endpoint="/models")}
    except Exception as e:  # noqa: BLE001
        logger.warning("list_models 失败：%s", e)
        return {"models": [], "provenance": make_provenance(source="vendor", endpoint="/models", error=str(e))}
