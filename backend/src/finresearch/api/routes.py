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
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from ..agents.deep.research_tools import clear_task as research_clear_task
from ..agents.deep.research_tools import get_stored as research_get_stored
from ..agents.graph import get_checkpointer
from ..agents.prompts import CHART_SYSTEM
from ..config import settings
from ..db.models import ChatContextSummary, ChatConversation, ChatDelta, LocalSetting, Report, ResearchTask
from ..db.session import db_ping, session_scope
from ..llm import build_chart_llm, build_llm, llm_available, model_window_tokens, resolved_chart_model
from ..tools import export, now_iso
from ..tools.context import build_chart_context
from ..tools.context import recent_diagnostics as context_diagnostics
from ..tools.context_store import (
    delete_conversation_data,
    image_asset_data_urls,
    image_asset_path,
    image_asset_urls,
    media_path,
    offload_message_media,
    save_image_asset,
    validate_legacy_images,
)
from ..tools.entity import resolve_entity
from ..tools.market import (
    get_bid_ask,
    get_chart_bundle,
    get_indicators,
    get_intraday,
    get_kline,
    get_realtime_quote,
)
from ..cache import quote_cache
from ..tools import kb as tools_kb
from ..tools import memory as tools_memory
from ..agents.deep.research_agent import build_research_agent
from ..agents.deep.stream import stream_agent_sse, tool_label
from ..tools.market_provider import index_overview
from ..tools.report import DISCLAIMER
from ..tools.secrets import decrypt_config, encrypt_config
from ..tools.text import strip_md
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


def _coalesce_history_append(history: list, frame: str, cap: int = 600) -> None:
    """研究流回放历史的段级折叠：同段连续的 section_token 合并为一帧。

    直播订阅者仍逐帧收到（打字机效果不受影响），折叠只作用于重连回放：
    真流式下 section_token 逐 chunk 一帧，3000 字报告即数千帧，600 帧上限
    必然先裁掉最早的段——刷新后前几段丢失，直到末尾 done 才补齐。
    折叠后每段至多占 1-2 帧，600 帧足以覆盖完整报告。
    """

    def _parse(item: str):
        try:
            payload = json.loads(item[6:].strip()) if item.startswith("data: ") else None
        except Exception:  # noqa: BLE001
            return None
        return payload if isinstance(payload, dict) else None

    payload = _parse(frame)
    if payload is not None and payload.get("type") == "section_token" and history:
        last = _parse(history[-1])
        if (
            isinstance(last, dict)
            and last.get("type") in ("section_token", "summary_token")
            and (last.get("title") == payload.get("title") or (last.get("type") == "summary_token" and payload.get("type") == "summary_token"))
        ):
            merged = {
                **last,
                "text": str(last.get("text", "")) + str(payload.get("text", "")),
            }
            history[-1] = sse.sse(merged)
            return
    history.append(frame)
    if len(history) > cap:
        del history[: len(history) - cap]

# 每会话串行锁：send_chat_message 全程持有（请求体开始 → 流式生成器结束）。
# 同一会话并发请求会因 _chat_next_seq 读最大值+1 且无唯一约束而写出重复 seq，
# 并以同一 thread_id 并发写 LangGraph 检查点——历史乱序/回复串台的根源。
# 家用单进程场景锁常驻内存（数量 = 会话数），不做淘汰。
_CHAT_LOCKS: dict[str, asyncio.Lock] = {}

# 对话流心跳间隔：与 research_stream 的 10s 对齐。前端 dispatch 未识别的类型直接忽略。
_CHAT_HEARTBEAT_S = 10.0

# 研究运行时终态后的保留时长（秒）：供断线重连回放，到期回收防内存无限增长。
_RUNTIME_TTL_S = 30 * 60


def _chat_lock(conversation_id: str) -> asyncio.Lock:
    lock = _CHAT_LOCKS.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _CHAT_LOCKS[conversation_id] = lock
    return lock


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
                _coalesce_history_append(runtime.history, frame)
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
            # 终态延迟回收：留 TTL 窗口供断线重连回放（history 尾部含 done 帧），
            # 到期后从 _RESEARCH_RUNS/_TASK_LLM 移除——曾经完成的运行时永不回收，
            # 每个任务连 history 与明文模型配置一起常驻内存，长期使用无限累积。
            # 回收后重连走 DB 路径（done 任务读库返回终态帧，不会重跑）。
            async def _reap_later() -> None:
                await asyncio.sleep(_RUNTIME_TTL_S)
                if _RESEARCH_RUNS.get(task_id) is runtime:
                    _RESEARCH_RUNS.pop(task_id, None)
                    _TASK_LLM.pop(task_id, None)

            asyncio.create_task(_reap_later(), name=f"research-reap:{task_id}")

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
    # 与 agent 工具层同理设防：n 无上界时单次可抓 900 根（响应数 MB），请求量与
    # 返回量静默不符（_MAX_FETCH 截断）。图表页正常用量 ≤250。
    n = max(5, min(int(n or 60), 250))
    return await _market_call(get_kline, code, n, adjust)


@router.get("/quote/{code}/book")
async def book(code: str) -> dict:
    return await _market_call(get_bid_ask, code)


@router.get("/quote/{code}/indicators")
async def indicators(code: str) -> list:
    return (await _market_call(get_indicators, code)).get("data", [])


@router.get("/quote/{code}/chart-bundle")
async def chart_bundle(code: str, n: int = 120, adjust: str = "qfq") -> dict:
    """行情分析页聚合数据；一次 K 线计算蜡烛与完整指标序列。"""
    return await _market_call(get_chart_bundle, code, n, adjust)


# ────────────────────────────────────────────────────────────
# 诊断（默认零网络快速；?refresh=1 才现场探测）
# ────────────────────────────────────────────────────────────
# ────────────────────────────────────────────────────────────
# 知识库（用户文档上传 → 解析 → 分块 → 嵌入 rag.db）
# ────────────────────────────────────────────────────────────
@router.post("/kb/documents")
async def kb_upload_document(request: Request) -> dict:
    """上传用户文档（txt/md/pdf/docx，≤20MB）。文件名经 X-Filename 头传递（URL 编码）。"""
    from urllib.parse import unquote

    try:
        content_length = int(request.headers.get("content-length") or 0)
    except ValueError as exc:
        raise HTTPException(400, "invalid content-length") from exc
    if content_length > 20 * 1024 * 1024:
        raise HTTPException(413, "文档不能超过 20MB")
    filename = unquote(request.headers.get("x-filename") or "").strip()
    if not filename:
        raise HTTPException(400, "缺少 X-Filename 头（URL 编码的文件名）")
    if "/" in filename or "\\" in filename:
        raise HTTPException(400, "文件名不能包含路径分隔符")
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "文档内容为空")
    if not tools_memory.memory_available():
        raise HTTPException(503, "记忆/知识库未启用（向量库不可用）")
    try:
        meta = await asyncio.to_thread(tools_kb.ingest_document_sync, filename, raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("知识库文档入库失败")
        raise HTTPException(500, f"文档解析失败：{exc}") from exc
    return {"document": meta}


@router.get("/kb/documents")
async def kb_list_documents() -> dict:
    return {"documents": await asyncio.to_thread(tools_kb.list_documents_sync)}


@router.delete("/kb/documents/{doc_id}")
async def kb_delete_document(doc_id: str) -> dict:
    ok = await asyncio.to_thread(tools_kb.delete_document_sync, doc_id)
    if not ok:
        raise HTTPException(404, "document not found")
    return {"ok": True}


@router.get("/memory/embedding")
async def memory_embedding_config() -> dict:
    """当前记忆嵌入配置（模式/模型/维度/向量数）。"""
    return await tools_memory.get_embedding_config()


class EmbeddingConfigRequest(BaseModel):
    base_url: str
    api_key: str
    model: str


@router.post("/memory/embedding")
async def memory_embedding_save(req: EmbeddingConfigRequest) -> dict:
    """保存供应商嵌入配置：探测连通性 → 加密落库 → 重建向量库。"""
    if not tools_memory.memory_available():
        raise HTTPException(503, "记忆/知识库未启用")
    try:
        return await tools_memory.save_embedding_config(req.base_url, req.api_key, req.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # 探测失败（401/模型名错/网络）——给可操作的报文，不泄漏完整堆栈
        msg = str(exc)
        if "401" in msg or "Unauthorized" in msg:
            friendly = "认证失败（401）：API Key 无效"
        elif "404" in msg or "Not Found" in msg:
            friendly = "接口不存在（404）：请检查 Base URL 与向量模型名"
        elif "429" in msg:
            friendly = "请求过于频繁（429）：请稍后重试"
        else:
            friendly = f"嵌入接口调用失败：{msg[:120]}"
        raise HTTPException(400, friendly) from exc


@router.delete("/memory/embedding")
async def memory_embedding_clear() -> dict:
    """清除供应商嵌入，回到本地哈希档并重建。"""
    if not tools_memory.memory_available():
        raise HTTPException(503, "记忆/知识库未启用")
    return await tools_memory.clear_embedding_config()


@router.get("/kb/search")
async def kb_search(q: str, limit: int = 5) -> dict:
    """知识库检索测试（agent 对话走全域 search_memory，这里供 UI 验证）。"""
    if not q.strip():
        return {"items": []}
    return await asyncio.to_thread(tools_kb.search_documents_sync, q, limit)


@router.get("/market/indexes")
async def market_indexes() -> dict:
    """大盘指数条（上证/深成/创业板/科创50/沪深300 一次批量）。3 秒缓存供多卡轮询共享。"""
    cached = quote_cache.get("index_overview")
    if cached is not None:
        return cached
    result = await asyncio.to_thread(index_overview)
    quote_cache.set("index_overview", result, ttl=3)
    return result


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


class MarketContextRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    kind: Literal["intraday", "kline"]
    adjust: Literal["qfq", "hfq", ""] = "qfq"
    visible_start: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    visible_end: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    indicator: Optional[Literal["MACD", "RSI", "KDJ", "BOLL"]] = None


class ChatSendRequest(BaseModel):
    message: str = ""
    llm: Optional[dict] = None
    images: list[str] = Field(default_factory=list)  # 旧版 data URL，只读兼容
    image_asset_ids: list[str] = Field(default_factory=list)
    market_context: Optional[MarketContextRequest] = None


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


def _trim_seed_history(history: list, budget_chars: int) -> tuple[list, int]:
    """播种历史按预算从最新往回保留；返回 (新列表, 被裁掉的条数)。

    空线程播种是一次性把全量 DB 历史塞给 agent：近似 token 计数对中文正文
    低估约 2 倍，首字前就可能打爆小窗口模型（且播种消息无 usage_metadata，
    reported-token 触发不可用）。丢最老的历史（DB 仍在），补一条省略说明。
    """
    items = list(history or [])
    kept: list = []
    used = 0
    dropped = 0
    for m in reversed(items):
        size = len(str(m.get("content") or ""))
        size += sum(len(str(x)) for x in (m.get("images") or []))
        if kept and used + size > budget_chars:
            dropped = len(items) - len(kept)
            break
        kept.append(m)
        used += size
    if not dropped:
        return items, 0
    kept.append({
        "role": "user",
        "content": f"（更早的 {dropped} 条历史已省略；如需关键背景可请用户重述。）",
    })
    return list(reversed(kept)), dropped


async def _deepagent_chat_stream(
    *,
    conv_id: str,
    text: str,
    history: list,
    profile: dict,
    llm_cfg: Optional[dict],
    images: Optional[list] = None,
    user_msg: Optional[dict] = None,
    market_context: Optional[dict] = None,
    memory_note: str = "",
):
    """deepagents 对话链路：产出 (sse_line, text_piece)。

    取数决定权在模型手里：不再每轮预取行情塞 prompt，模型自己决定调什么工具、调几次。
    """
    from ..agents.deep.chat_agent import build_chat_agent
    from ..tools.context import _to_lc_content

    checkpointer, _kind = get_checkpointer()
    context_note = ""
    if memory_note:
        # 归档会话继续对话时以摘要恢复上下文（"很久之前聊了什么"）
        context_note = memory_note.strip() + chr(10) * 2
    if market_context:
        # 拼接而非覆盖——曾经 if market_context: context_note = (...)
        # 直接整体重写，两者同现时归档摘要被静默丢弃
        context_note += (
            "【当前行情页面上下文】\n"
            f"用户正在查看标的 {market_context.get('code')}，图表类型 {market_context.get('kind')}，"
            f"复权 {market_context.get('adjust') or 'qfq'}，可见区间 "
            f"{market_context.get('visible_start') or '未指定'} 至 {market_context.get('visible_end') or '未指定'}，"
            f"所选指标 {market_context.get('indicator') or '未指定'}。\n"
            "这些字段只描述界面选择，不包含可信行情数值。回答任何价格、涨跌、指标或趋势问题前，"
            "必须调用对应行情工具重新核验，不得根据界面上下文猜测数值。"
        )
    agent = build_chat_agent(
        llm_config=llm_cfg,
        profile=profile,
        context_note=context_note,
        checkpointer=checkpointer,
    )
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
            if not text.strip():
                text = "用户仅发送了图片；当前模型不支持视觉，请提示用户补充文字描述后再分析。"

    def _content(msg: dict):
        """带图消息 → 多模态 content blocks；无图 → 纯字符串。复用既有转换，不另造。"""
        return _to_lc_content(msg) if msg.get("images") else (msg.get("content") or "")

    # agent 的历史在 checkpointer 里，UI 的历史在 DB 里，两套状态各自独立。
    # 迁移前建的老对话在 checkpointer 里没有线程状态：此时只发新消息的话，agent 会对前文
    # 一无所知——而界面上明明白白显示着历史，用户会觉得"它失忆了"。
    # 所以线程为空时用 DB 历史播种一次，之后每轮只发增量（history 末条就是本轮提问，不会重复）。
    this_turn = dict(user_msg or {"role": "user", "content": text})
    this_turn["content"] = text
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
        seed_history = [*list(history or [])[:-1], this_turn] if history else [this_turn]
        # 播种预算：窗口的 40% token 折成字符（中文按 1.5 字符/token 保守估）。
        # 无模型名信息时 model_window_tokens 回落 32K 最保守档。
        seed_model = (llm_cfg or {}).get("model") or settings.llm_model or ""
        seed_budget = int(model_window_tokens(seed_model) * 0.4 * 1.5)
        seed_history, _dropped = _trim_seed_history(seed_history, seed_budget)
        if images is None:
            seed_history = [{key: value for key, value in message.items() if key != "images"} for message in seed_history]
        msgs = [
            {"role": m.get("role") or "user", "content": _content(m)}
            for m in seed_history
            if (m.get("content") or "").strip() or m.get("images")
        ] or [{"role": "user", "content": _content(this_turn)}]

    if note:
        yield sse.ev_warning(note.strip()), ""

    async for line, piece in stream_agent_sse(agent, {"messages": msgs}, config):
        yield line, piece


def _chat_msg(
    role: str,
    content: str,
    images: Optional[list[str]] = None,
    image_asset_ids: Optional[list[str]] = None,
) -> dict:
    msg = {
        "id": uuid.uuid4().hex,
        "role": role,
        "content": content,
        "created_at": now_iso(),
    }
    if images:
        msg["images"] = images
    if image_asset_ids:
        msg["image_asset_ids"] = image_asset_ids
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


@router.post("/assets/images")
async def upload_image_asset(request: Request) -> dict:
    """保存受控图片资产；响应只暴露 asset_id 与同源 URL。"""
    try:
        content_length = int(request.headers.get("content-length") or 0)
    except ValueError as exc:
        raise HTTPException(400, "invalid content-length") from exc
    if content_length > 5 * 1024 * 1024:
        raise HTTPException(413, "单张图片不能超过 5MB")
    raw = await request.body()
    try:
        return await asyncio.to_thread(
            save_image_asset,
            raw,
            request.headers.get("content-type", ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/assets/images/{asset_id}")
async def get_image_asset(asset_id: str):
    try:
        path, mime = await asyncio.to_thread(image_asset_path, asset_id)
    except ValueError as exc:
        raise HTTPException(400, "invalid asset_id") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "asset not found") from exc
    return FileResponse(path, media_type=mime)


def _utc_iso(v) -> str:
    """DB 时间戳 → 带时区 ISO 串。

    SQLite 的 CURRENT_TIMESTAMP 是 UTC naive，裸 isoformat() 出来的串没有时区，
    前端 new Date() 会按本地时区解析（中国差 8 小时：晚上创建的会话显示成下午）。
    统一按 UTC 解释并补上时区再输出。
    """
    if v is None:
        return ""
    d = v if v.tzinfo is not None else v.replace(tzinfo=dt.UTC)
    return d.astimezone(dt.UTC).isoformat()


def _chat_meta(conv: ChatConversation, messages: Optional[list[dict]] = None) -> dict:
    stored_model = conv.model_config or {}
    return {
        "id": conv.id,
        "title": conv.title,
        "snapshot_interval": conv.snapshot_interval,
        "model_config": {
            "model": stored_model.get("model", ""),
            "reasoning_effort": stored_model.get("reasoning_effort", "low"),
            "configured": bool(stored_model.get("api_key")),
        } if stored_model else None,
        "archived_at": _utc_iso(conv.archived_at) or None,
        "created_at": _utc_iso(conv.created_at),
        "updated_at": _utc_iso(conv.updated_at),
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
    # 持会话锁删除：若该会话还有流式回复在跑，等它结束再删，避免删掉历史后
    # 流末尾的落库又把助手回复写回来（复活出半个会话）
    async with _chat_lock(conversation_id):
        async with session_scope() as s:
            if s is None:
                raise HTTPException(503, "local database unavailable")
            conv = await s.get(ChatConversation, conversation_id)
            if not conv:
                raise HTTPException(404, "conversation not found")
            await s.execute(delete(ChatDelta).where(ChatDelta.conversation_id == conversation_id))
            await s.execute(delete(ChatContextSummary).where(ChatContextSummary.conversation_id == conversation_id))
            await s.execute(
                delete(LocalSetting).where(LocalSetting.key == f"context-diag:chat:{conversation_id}")
            )
            await s.delete(conv)
            await s.commit()
        # 检查点线程也删（报告链路 delete_report 早就做了，这里补齐）：
        # 每轮对话都向 finresearch.db 写含完整消息日志的检查点，不删则
        # 删掉的会话数据永久残留，SQLite 文件无限膨胀
        cp, _kind = get_checkpointer()
        if cp is not None and hasattr(cp, "adelete_thread"):
            try:
                await cp.adelete_thread(conversation_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("删除会话检查点线程失败 conversation=%s：%s", conversation_id, e)
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


# Markdown 清洗实现搬到 tools/text.py：报告链路也要用同一份，
# 否则"杜绝 .md"只在对话链路生效（报告正文里的 **加粗** 会原样显示成字面符号）。
_strip_md = strip_md


@router.post("/chat/conversations/{conversation_id}/messages")
async def send_chat_message(conversation_id: str, req: ChatSendRequest):
    """流式对话：POST 返回 text/event-stream。

    事件序列：stage(understanding/thinking/composing) → 多个 token → done(含 conversation/messages/reply)。
    用户消息在流式开始前持久化（独立 session 提交）；助手回复在流式结束后持久化；
    流式中断/异常时把已流出的半截回复也落库，保证 DB 与 LangGraph 检查点不分叉。
    前端用 fetch + ReadableStream 解析（EventSource 仅支持 GET，无法带 llm body）。
    """
    # 每会话串行锁：同一会话并发两条消息会各算出相同 seq（_chat_next_seq 读最大值+1，
    # (conversation_id, seq) 无唯一约束）并以同一 thread_id 并发写检查点——
    # 历史乱序、回复串台的根源。锁在请求体开始前获取、gen() 结束时释放（全程串行）。
    lock = _chat_lock(conversation_id)
    # 有界等待：曾经无限等待——锁泄漏（SSE 流从未被迭代时 finally 不执行）会让
    # 该会话的下一条消息永久挂起，前端 fetch 无超时 → loading 永远 true → 输入被锁
    try:
        await asyncio.wait_for(lock.acquire(), timeout=180)
    except asyncio.TimeoutError:
        raise HTTPException(409, "上一条回复仍在处理中（超过 3 分钟），请稍后重试或刷新页面。")
    text = (req.message or "").strip()
    try:
        asset_ids = list(dict.fromkeys(req.image_asset_ids or []))
        if len(asset_ids) + len(req.images or []) > 4:
            raise HTTPException(400, "每条消息最多上传 4 张图片")
        try:
            validate_legacy_images(req.images)
            await asyncio.to_thread(image_asset_urls, asset_ids)
            asset_data_urls = await asyncio.to_thread(image_asset_data_urls, asset_ids)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(400, str(exc)) from exc
        model_images = [*(req.images or []), *asset_data_urls]
        if not text and not model_images:
            raise HTTPException(400, "message is empty")

        # 1) 持久化用户消息（独立 session，提交后才进入流式，确保即使流式中断也保留提问）
        async with session_scope() as s:
            if s is None:
                raise HTTPException(503, "local database unavailable")
            conv = await s.get(ChatConversation, conversation_id)
            if not conv:
                raise HTTPException(404, "conversation not found")
            history = await _chat_history(s, conversation_id)
            user_msg = _chat_msg(
                "user",
                text,
                images=req.images or None,
                image_asset_ids=asset_ids or None,
            )
            history = await _chat_append(s, conv, user_msg, history)
            if req.llm is not None:
                conv.model_config = encrypt_config(req.llm)
            if conv.title == "新对话":
                conv.title = text[:36] or "图片分析"
            conv.updated_at = dt.datetime.now(dt.UTC)
            await s.commit()
            conv_id = conv.id
            llm_cfg = req.llm or decrypt_config(conv.model_config)
            history_snapshot = list(history)
            # 数据库存 asset_id，进入模型前才按需读取为 data URL；公共消息不暴露真实路径。
            for index, stored_message in enumerate(history_snapshot):
                stored_asset_ids = list(stored_message.get("image_asset_ids") or [])
                if not stored_asset_ids:
                    continue
                model_message = dict(stored_message)
                try:
                    model_message["images"] = await asyncio.to_thread(
                        image_asset_data_urls,
                        stored_asset_ids,
                    )
                except (ValueError, FileNotFoundError):
                    model_message["images"] = []
                history_snapshot[index] = model_message
            if model_images and history_snapshot:
                model_message = dict(history_snapshot[-1])
                model_message["images"] = model_images
                history_snapshot[-1] = model_message
            # 加载本地用户画像（默认稳健研究型），供上下文治理注入 system prompt
            # or {}（而非 or None）：缺行或空 payload 时交给 render_profile_block 逐字段回填 DEFAULT_PROFILE
            prof_row = await s.get(LocalSetting, "user-profile")
            user_profile = (prof_row.payload if prof_row else None) or {}
        # 行级消息为空且存在归档摘要 → 以摘要恢复上下文（会话被生命周期归档后继续聊）
        memory_note = ""
        if not history:
            summary = await tools_memory.archived_summary(conversation_id)
            if summary:
                memory_note = "【本会话更早内容的机器摘要（原文已归档压缩）】" + chr(10) + summary
    except BaseException:
        lock.release()
        raise

    # 只判"有没有配模型"，不在这里构造模型：真正要用的那个由 build_chat_agent 内部建，
    # 这里再建一个只为判空是白白多造一套 httpx 客户端。
    has_model = llm_available(llm_cfg)

    async def _persist_assistant_reply(content: str) -> None:
        """把助手回复（含中断/失败时已流出的半截）落库。独立 session，尽力而为，绝不向上抛。"""
        content = (content or "").strip()
        if not content:
            return
        try:
            async with session_scope() as s:
                if s is None:
                    return
                conv2 = await s.get(ChatConversation, conv_id)
                if conv2 is not None:
                    history2 = await _chat_history(s, conv_id)
                    await _chat_append(s, conv2, _chat_msg("assistant", content), history2)
                    await s.commit()
        except Exception:  # noqa: BLE001
            logger.warning("助手回复落库失败（conversation=%s）", conv_id, exc_info=True)

    async def gen():
        reply_text = ""
        try:

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
                stream = _deepagent_chat_stream(
                    conv_id=conv_id,
                    text=text,
                    history=history_snapshot,
                    profile=user_profile,
                    llm_cfg=llm_cfg,
                    images=model_images,
                    user_msg=history_snapshot[-1] if history_snapshot else None,
                    market_context=(
                        req.market_context.model_dump(exclude_none=True)
                        if req.market_context
                        else None
                    ),
                    memory_note=memory_note,
                )
                # 手动迭代以注入心跳：模型首字静默期（推理模型可达分钟级）若无字节下发，
                # 代理/网关会按空闲超时掐断连接。不能用 wait_for 包 __anext__——超时会
                # 取消掉底层流；改为挂 task + asyncio.wait(timeout)，超时只补心跳不动流。
                nxt: Optional[asyncio.Task] = None
                try:
                    while True:
                        if nxt is None:
                            nxt = asyncio.ensure_future(stream.__anext__())
                        done, _pending = await asyncio.wait({nxt}, timeout=_CHAT_HEARTBEAT_S)
                        if not done:
                            yield sse.sse({"type": "heartbeat"})
                            continue
                        try:
                            item = nxt.result()
                        except StopAsyncIteration:
                            break
                        nxt = None
                        _line, _piece = item
                        yield _line
                        reply_text += _piece
                except (asyncio.CancelledError, GeneratorExit):
                    # 客户端断开（刷新/关窗/断网）：已流出的半截回复必须落库——
                    # 检查点已按轮持久化这轮对话，DB 若缺这条，界面与模型记忆将永久分叉
                    # （下一轮模型说"如我刚才所说"，界面上却没有那条回答）。
                    try:
                        await _persist_assistant_reply(_strip_md(reply_text) + "…（回复被中断）")
                    finally:
                        raise
                except Exception:  # noqa: BLE001
                    logger.exception("对话链路失败")
                    # 已流出的半截同样落库，刷新后问题不再悬空无答
                    await _persist_assistant_reply(_strip_md(reply_text) + "…（回复中断）")
                    yield sse.ev_error("模型调用失败，请检查模型配置或稍后重试。")
                    return
                finally:
                    # 客户端断开时终止挂起的流读取，避免后台继续消费模型输出
                    if nxt is not None and not nxt.done():
                        nxt.cancel()
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
            if tools_memory.memory_available():
                # 记忆摄入放后台：本轮问答入本地知识库（哈希去重幂等）
                _title = (final_meta or {}).get("title") or ""
                asyncio.create_task(asyncio.to_thread(
                    tools_memory.ingest_exchange_sync, conv_id, _title, text, reply_text
                ))
        finally:
            # 请求全程持有会话锁：gen 正常结束/异常/被取消都在此释放
            lock.release()

    from starlette.background import BackgroundTask

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        # 兜底释放：Starlette 在客户端断开时不一定拉取 body_iterator，
        # 从未被迭代的 async generator 的 finally 不会执行 → 锁泄漏到进程结束
        background=BackgroundTask(lambda: lock.locked() and lock.release()),
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
        "status": "succeeded" if t.status == "done" else t.status,
        "stage": "succeeded" if t.stage == "done" else t.stage,
        "report_id": t.report_id,
        "error": t.error,
        "created_at": _utc_iso(t.created_at),
        "updated_at": _utc_iso(t.updated_at),
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
                # SQLite 的 CURRENT_TIMESTAMP 是 UTC naive（PG 是 aware）：统一按 UTC 解释，
                # 再与 UTC now 相减。曾经 naive-UTC 对本地 naive now 相减，UTC+8 机器上
                # age_s 恒 ≥28800，pending 30 秒复用窗口永不生效（双击提交即误报"遗留任务"）
                if updated is not None and updated.tzinfo is None:
                    updated = updated.replace(tzinfo=dt.UTC)
                age_s = (dt.datetime.now(dt.UTC) - updated).total_seconds() if updated is not None else 9999.0
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
    existing: Optional[ResearchTask] = None  # 无 DB（s=None）时下面的回放分支引用它，必须先定义
    async with session_scope() as s:
        if s is not None:
            existing = await s.get(ResearchTask, task_id)
            if existing and existing.status in ("done", "succeeded") and existing.report_id:
                saved = await s.get(Report, existing.report_id)
                if saved:
                    terminal_frame = sse.sse({
                        "type": "done", "report_id": saved.id, "report": saved.payload or {}
                    })
            elif existing and existing.status == "error":
                terminal_frame = sse.ev_error(existing.error or "研究任务失败")
            # 终态回放时补发 tool_call ok 帧：断线重连后左侧轨迹不再留转圈孤儿
            elif existing and existing.status == "running":
                runtime = _RESEARCH_RUNS.get(task_id)
                if not runtime or not runtime.task or runtime.task.done():
                    existing.status = "error"
                    existing.stage = "error"
                    existing.error = "研究任务因应用重启而中断，请重新提交。"
                    await s.commit()
                    terminal_frame = sse.ev_error(existing.error)

    if terminal_frame is not None:
        # 从已持久化的 progress_events 合成 tool_call start+ok 帧（回放真实执行轨迹）。
        # 只发 ok 不发 start 的话，前端配不上就不渲染芯片——重连后轨迹面板是空的；
        # start+ok 成对回放，断线重连/切页回来能看到完整的绿色轨迹。
        replay_frames: list[str] = []
        if existing is not None:
            for evt in (existing.progress_events or []):
                node = evt.get("node") or ""
                if node and node not in ("_task",):
                    replay_frames.append(sse.sse({
                        "type": "tool_call", "name": node,
                        "label": evt.get("label") or tool_label(node),
                        "status": "start", "call_id": "",
                    }))
                    replay_frames.append(sse.sse({
                        "type": "tool_call", "name": node,
                        "label": evt.get("label") or tool_label(node),
                        "status": "ok", "call_id": "",
                    }))

        async def terminal_gen():
            for frame in replay_frames:
                yield frame
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

    async def _fail_task(message: str) -> None:
        """早退路径把任务行标 error——曾经早退只发 ev_error 不回写 DB，任务永久卡 running，
        列表永远显示'运行中'，重开 stream 还会被误判为'应用重启而中断'。"""
        async with session_scope() as s:
            if s is None:
                return
            t = await s.get(ResearchTask, task_id)
            if t is not None and t.status not in ("done", "succeeded", "error"):
                t.status = "error"
                t.stage = "error"
                t.error = message
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
            await _fail_task("缺少 code（DB 无此任务且未传 code 查询参数）")
            yield sse.ev_error("缺少 code（DB 无此任务且未传 code 查询参数）")
            return

        await asyncio.sleep(0)
        from ..agents.deep.research_agent import build_research_agent  # 延迟导入
        agent = build_research_agent(
            task_id=task_id,
            template=tpl_ or "equity_deep",
            llm_config=llm_cfg,
            profile=user_profile,
            checkpointer=get_checkpointer()[0],
        )
        if agent is None:
            await _fail_task("模型未配置：请在「模型配置」中保存 API Key 与模型后重试。")
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
                # 取数工具失败时会把真实原因写进 fatal_error。没有它才退回笼统文案——
                # 只报"数据源可能不可用"的话，用户不知道是哪一步挂了、该重试还是换个说法。
                reason = research_get_stored(task_id, "fatal_error", "") or "未生成报告草稿（数据源可能不可用）"
                async with session_scope() as s:
                    if s is not None:
                        t = await s.get(ResearchTask, task_id)
                        if t:
                            t.status = "error"
                            t.stage = "error"
                            t.error = reason
                            await s.commit()
                yield sse.ev_error(reason)
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
                        t.status = "succeeded"
                        t.stage = "succeeded"
                        t.report_id = report_id
                        t.timings = dict(merged_timings)
                    await s.commit()

            if tools_memory.memory_available():
                # 报告入用户知识库（后台执行；报告是用户成果资产，永久保留可被回忆）
                asyncio.create_task(asyncio.to_thread(
                    tools_memory.ingest_report_sync,
                    report_id, draft, draft.get("ticker", code_), draft.get("name", ""),
                ))

            await _persist_context_diagnostics(
                "research",
                task_id,
                context_diagnostics(limit=20, kind="research", obj_id=task_id),
            )

            # 2) 摘要已在 summary 节点优先推送；兼容异常/旧图路径时再补发。
            summary = draft.get("summary", "") or ""
            if not summary_streamed:
                yield sse.sse({"type": "summary", "text": summary})

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
            "created_at": _utc_iso(r.created_at),
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
        # 进程内残留一并清掉，"真删除"不能只删 DB：
        # _STORE 存着被删报告的完整正文/K线/新闻素材；_TASK_LLM 存着含明文 api_key
        # 的模型配置；context-diag 行是诊断环形缓冲快照
        research_clear_task(task_id)
        _TASK_LLM.pop(task_id, None)
        # 同步清理向量库中的报告块（防死块）
        if tools_memory.memory_available():
            tools_memory.purge_report_chunks(rid)
        try:
            async with session_scope() as s:
                if s is not None:
                    await s.execute(
                        delete(LocalSetting).where(
                            LocalSetting.key == f"context-diag:research:{task_id}"
                        )
                    )
                    await s.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("清理诊断行失败 task_id=%s：%s", task_id, e)
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
    from urllib.parse import urlparse

    from ..tools import make_provenance

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

    from urllib.parse import urlparse

    scheme = urlparse(req.base_url).scheme
    if scheme not in ("http", "https"):
        raise HTTPException(400, f"不支持的协议：{scheme or '(空)'}，仅允许 http/https")

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
