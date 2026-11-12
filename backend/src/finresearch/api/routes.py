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
import contextlib
import datetime as dt
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

import anyio  # v1.6.3：取消落库的 CancelScope(shield=True)（P1 修复）

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import defer

from ..agents._utils import sanitize_err_text
from ..agents.deep.research_agent import build_research_agent
from ..agents.deep.research_runner import is_direct_target, run_research_direct
from ..agents.deep.research_tools import clear_task as research_clear_task, get_stored as research_get_stored
from ..agents.deep.stream import stream_agent_sse
from ..agents.graph import get_checkpointer
from ..agents.prompts import CHART_SYSTEM
from ..agents.workers.report import check_generated  # 对话合规闸与报告共用同一份禁用词检测
from ..tools import export, now_cst, now_iso
from ..tools.secrets import (
    decrypt_config,
    decrypt_nested,
    encrypt_config,
    encrypt_nested,
    is_masked_secret,
    mask_config,
    mask_nested,
    merge_masked_nested,
)
from ..tools.context import build_chart_context, recent_diagnostics as context_diagnostics, render_time_anchor
from ..tools.context_store import (
    delete_conversation_data,
    media_path,
    offload_message_media,
    validate_images_input,
)
from ..app_settings import APP_SETTINGS_KEY, load_app_settings, normalize as normalize_app_settings
from ..config import settings
from ..knowledge import service as kb_service
from ..knowledge.store import TEMPLATE_LABELS
from ..db.models import ChatContextSummary, ChatConversation, ChatDelta, LocalSetting, Report, ResearchTask
from ..db.session import db_ping, session_scope
from ..llm import (
    REASONING_EFFORTS,
    build_chart_llm,
    build_llm,
    context_window_tokens,
    is_reasoning_model,
    llm_available,
    reasoning_effort_of,
    resolved_chart_model,
    strip_think,
)
from ..tools.entity import lookup_stock_name, resolve_entity
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
# 五档盘口依赖慢速源时独立限流，防止拖慢快照/分时/K线首屏渲染
_BOOK_SEMAPHORE = asyncio.Semaphore(2)


async def _market_call(fn, *args):
    async with _MARKET_SEMAPHORE:
        return await asyncio.to_thread(fn, *args)


async def _book_call(fn, *args):
    async with _BOOK_SEMAPHORE:
        return await asyncio.to_thread(fn, *args)

# SSE 是 GET（EventSource 不能带 body），前端所选模型配置在此按 task_id 暂存，供 stream 取用。
# 进程内单例（家用单进程）；DB 可用时也同步落 ResearchTask.llm_config（跨重启）。
_TASK_LLM: dict[str, Optional[dict]] = {}
_TASK_LLM_CAP = 500


@dataclass
class _ResearchRuntime:
    """研究计算与任意一个 SSE 客户端解耦的进程内事件总线。

    history 是给**新连接**回放用的压缩历史（v1.7.1，审计 A06）：
    - section_token 按段合并成一帧（文本累加，位置保持在该段首个 token 处）；
    - section_thinking 是快照，每段只留最新一帧。
    于是回放帧数 ≈ 进度事件数，不再随 token 数线性增长 —— 旧实现逐 token 存 3000 帧，
    长报告会把摘要/段落开头挤出回放窗口；且每个新连接都从头回放，前端在已有内容上
    再追加一遍，正文与工具轨迹成倍重复。现在每个连接以 replay 帧开头，前端据此清空
    本轮展示状态后按压缩历史重建（幂等），实时订阅者仍逐 token 收帧。
    """

    task_id: str
    history: list[str] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: Optional[asyncio.Task] = None
    terminal: bool = False
    cancel_reason: str = ""
    _merge_idx: dict = field(default_factory=dict)   # (type, title) → history 下标
    _merge_text: dict = field(default_factory=dict)  # title → 已累计正文

    def record(self, frame: str) -> None:
        """把一帧写入回放历史（必要时与同段已有帧合并）。"""
        ev = _peek_event(frame)
        kind = ev.get("type") if ev else None
        if kind == "summary":
            # 权威摘要帧覆盖已合并的 summary_token，清空增量缓存避免后续 token 串味
            self._merge_text.pop("__summary__", None)
            self._merge_idx.pop(("summary_token", ""), None)
        elif kind == "sections_reset":
            # 裸 </think> 跨章节回退时重置已合并章节缓存
            sum_txt = self._merge_text.get("__summary__")
            self._merge_text.clear()
            if sum_txt is not None:
                self._merge_text["__summary__"] = sum_txt
            for k in [k for k in self._merge_idx if k[0] in ("section_token", "section_thinking")]:
                self._merge_idx.pop(k, None)
        if kind == "section_token":
            title = str(ev.get("title") or "")
            text = self._merge_text.get(title, "") + str(ev.get("text") or "")
            self._merge_text[title] = text
            merged = sse.sse({"type": "section_token", "title": title, "text": text})
            idx = self._merge_idx.get(("section_token", title))
            if idx is not None and idx < len(self.history):
                self.history[idx] = merged
                return
            self._merge_idx[("section_token", title)] = len(self.history)
            self.history.append(merged)
            return
        if kind == "summary_token":
            # v1.8.0：摘要逐 token 合并成一帧（与 section_token 同理，回放时不重复追加）
            text = self._merge_text.get("__summary__", "") + str(ev.get("text") or "")
            self._merge_text["__summary__"] = text
            merged = sse.sse({"type": "summary_token", "text": text})
            idx = self._merge_idx.get(("summary_token", ""))
            if idx is not None and idx < len(self.history):
                self.history[idx] = merged
                return
            self._merge_idx[("summary_token", "")] = len(self.history)
            self.history.append(merged)
            return
        if kind == "report_thinking":
            idx = self._merge_idx.get(("report_thinking", ""))
            if idx is not None and idx < len(self.history):
                self.history[idx] = frame
                return
            self._merge_idx[("report_thinking", "")] = len(self.history)
            self.history.append(frame)
            return
        if kind == "section_thinking":
            title = str(ev.get("title") or "")
            idx = self._merge_idx.get(("section_thinking", title))
            if idx is not None and idx < len(self.history):
                self.history[idx] = frame
                return
            self._merge_idx[("section_thinking", title)] = len(self.history)
            self.history.append(frame)
            return
        self.history.append(frame)
        # 安全上限：压缩后正常远低于此（进度事件数十帧）。超限只截掉最早的非关键帧
        # 之外的部分 —— 截断会让合并下标失效，故整体重建下标。
        if len(self.history) > 3000:
            del self.history[: len(self.history) - 3000]
            self._merge_idx.clear()
            self._merge_text.clear()


def _peek_event(frame: str) -> Optional[dict]:
    """解析单帧 SSE（`data: {json}\n\n`）；非 JSON 帧返回 None。"""
    if not frame.startswith("data: {"):
        return None
    try:
        ev = json.loads(frame[6:].strip())
    except (ValueError, TypeError):
        return None
    return ev if isinstance(ev, dict) else None


# 每个研究 SSE 连接的首帧：告诉前端「接下来是完整回放」，先清空本轮展示状态再重建，
# 自动重连/切页重连都不会把同一段正文、同一条工具轨迹追加两遍（审计 A06）。
_REPLAY_FRAME = sse.sse({"type": "replay"})


_RESEARCH_RUNS: dict[str, _ResearchRuntime] = {}
# v1.6.3（E）：终态 runtime 延迟清理（最后一个订阅者离开 + 终态标记 → 延迟 60s 弹出，
# 给回放窗口留缓冲；旧实现永不回收，每个完成任务最多持 3000 帧回放历史至进程退出）
_RESEARCH_RUNS_CAP = 64


def _maybe_evict_research_runs() -> None:
    if len(_RESEARCH_RUNS) <= _RESEARCH_RUNS_CAP:
        return
    evictable = [
        k for k, rt in _RESEARCH_RUNS.items()
        if rt.terminal and not rt.subscribers
    ]
    for k in evictable[: len(_RESEARCH_RUNS) - _RESEARCH_RUNS_CAP]:
        _RESEARCH_RUNS.pop(k, None)

# v1.6.0（审计 B9）：同会话并发发送守卫。研究链路有 _start_research_runtime 去重，
# 对话此前没有 —— 两个并发 POST 在同一 thread_id 上并发跑两个 agent 实例，检查点
# 状态交错、轮次重复/丢失，_chat_next_seq 还会分到相同 seq。
_CHAT_LOCKS: dict[str, asyncio.Lock] = {}
_CHAT_LOCKS_CAP = 200


def _chat_lock(conversation_id: str) -> asyncio.Lock:
    # 核验修复：先驱逐再插入 —— 后插入先驱逐可能把"刚 setdefault 尚未 acquire 的当前
    # 会话锁"弹出，同会话第二个请求拿到另一把锁，互斥失效（需 200+ 并发持锁才触发，
    # 一行重排彻底消除）。
    if len(_CHAT_LOCKS) > _CHAT_LOCKS_CAP:
        for k in [k for k, v in _CHAT_LOCKS.items() if not v.locked()][: len(_CHAT_LOCKS) - _CHAT_LOCKS_CAP]:
            _CHAT_LOCKS.pop(k, None)
    return _CHAT_LOCKS.setdefault(conversation_id, asyncio.Lock())


async def sweep_stale_research_tasks() -> int:
    """启动清扫（审计 B4）：把上一进程遗留的 pending/running 研究任务终结为 error。

    此前无此清扫时，任务行会永久卡在「running」——研究任务页恢复出一条永远在转的
    僵尸（生产库实证 3 条）。仅在 lifespan 启动时调用一次（新进程内不可能有活跃 runtime）。
    """
    swept = 0
    async with session_scope() as s:
        if s is None:
            return 0
        stmt = select(ResearchTask).where(ResearchTask.status.in_(["pending", "running"]))
        rows = list((await s.execute(stmt)).scalars().all())
        for t in rows:
            t.status = "error"
            t.stage = "error"
            t.error = "应用重启，任务中断，请重新提交。"
            swept += 1
        if rows:
            await s.commit()
    if swept:
        logger.info("启动清扫：%d 条遗留研究任务已标记为中断。", swept)
    return swept


def _task_age_seconds(updated) -> float:
    """v1.6.0（审计 B5）：任务年龄（秒）。SQLite func.now() 存 naive-UTC（生产库实证），
    与本地 naive now() 直接相减在东八区恒偏 +8h —— pending 任务 30s 复用窗口永远失效。"""
    if updated is None:
        return 9999.0
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - updated).total_seconds()


async def _mark_research_interrupted(task_id: str, message: str) -> None:
    """把仍处于 pending/running 的任务终结为 error（已 done/error 的行不动）。

    兜底落库路径：数据库异常只记日志，绝不让收尾逻辑本身抛错打断 runtime 的 finally。
    """
    try:
        async with session_scope() as s:
            if s is None:
                return
            row = await s.get(ResearchTask, task_id)
            if row and row.status in {"pending", "running"}:
                row.status = "error"
                row.stage = "error"
                row.error = message
                await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("研究任务终态兜底落库失败 task=%s：%s", task_id, e)


def _start_research_runtime(
    task_id: str, factory: Callable[[], AsyncIterator[str]]
) -> _ResearchRuntime:
    current = _RESEARCH_RUNS.get(task_id)
    if current and (current.terminal or (current.task and not current.task.done())):
        return current
    runtime = _ResearchRuntime(task_id=task_id)
    _RESEARCH_RUNS[task_id] = runtime

    async def pump() -> None:
        last_error = ""
        try:
            async for frame in factory():
                # v1.7.1（A06）：回放历史压缩存储（见 _ResearchRuntime docstring）；
                # 实时订阅者照旧逐帧收。
                runtime.record(frame)
                ev = _peek_event(frame)
                if ev and ev.get("type") == "error":
                    last_error = str(ev.get("message") or "")
                for queue in tuple(runtime.subscribers):
                    queue.put_nowait(frame)
        except asyncio.CancelledError:
            reason = runtime.cancel_reason or "研究任务因应用关闭而中断，请重新提交。"
            await _mark_research_interrupted(task_id, reason)
            if not runtime.terminal:
                err_frame = sse.ev_error(reason)
                runtime.record(err_frame)
                for queue in tuple(runtime.subscribers):
                    queue.put_nowait(err_frame)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("research runtime failed: %s", task_id)
            safe_err = sanitize_err_text(exc)
            await _mark_research_interrupted(task_id, safe_err)
            frame = sse.ev_error(safe_err)
            runtime.record(frame)
            for queue in tuple(runtime.subscribers):
                queue.put_nowait(frame)
        else:
            # v1.7.1（审计 A05）：管线正常结束但没有落终态（任何未来新增的提前 return
            # 路径）时兜底 —— 任务行绝不能停在 running，失败原因取最后一个 error 帧。
            # 已是 done/error 的行不受影响（_mark_research_interrupted 只改 pending/running）。
            await _mark_research_interrupted(task_id, last_error or "研究任务异常结束（未生成报告）。")
        finally:
            runtime.terminal = True
            _TASK_LLM.pop(task_id, None)
            research_clear_task(task_id)
            for queue in tuple(runtime.subscribers):
                queue.put_nowait(None)
            _maybe_evict_research_runs()  # 超上限时先回收无订阅者的终态条目
            _schedule_run_eviction(task_id)  # 60s 后延迟回收（给回放窗口留缓冲）

    runtime.task = asyncio.create_task(pump(), name=f"research:{task_id}")
    return runtime


def _schedule_run_eviction(task_id: str) -> None:
    """终态 runtime 延迟 60s 弹出（订阅者可在窗口内重连回放；到期仍无订阅者则回收）。"""

    async def _evict() -> None:
        await asyncio.sleep(60)
        rt = _RESEARCH_RUNS.get(task_id)
        if rt is not None and rt.terminal and not rt.subscribers:
            _RESEARCH_RUNS.pop(task_id, None)

    try:
        asyncio.get_running_loop().create_task(_evict(), name=f"evict:{task_id}")
    except RuntimeError:  # 无运行 loop（单测直调）
        pass


def _norm_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


async def _current_key_for_provider_or_base(provider_id: str = "", base_url: str = "") -> Optional[str]:
    """按 provider_id 优先、base_url 次之，在 model-configs 全表找当前真实解密 key。"""
    pid = (provider_id or "").strip()
    req_base = _norm_base_url(base_url)
    if not pid and not req_base:
        return None
    try:
        async with session_scope() as s:
            if s is None:
                return None
            c_row = await s.get(LocalSetting, "model-configs")
            configs = c_row.payload if (c_row and isinstance(c_row.payload, dict)) else {}
        decrypted = decrypt_nested(configs)
        if pid and isinstance(decrypted.get(pid), dict):
            key = str(decrypted[pid].get("apiKey") or "").strip()
            if key and not is_masked_secret(key):
                return key
        if req_base:
            for cfg in decrypted.values():
                if isinstance(cfg, dict) and _norm_base_url(str(cfg.get("baseUrl") or "")) == req_base:
                    key = str(cfg.get("apiKey") or "").strip()
                    if key and not is_masked_secret(key):
                        return key
    except Exception as e:  # noqa: BLE001
        logger.debug("按 provider/base_url 找当前 key 失败：%s", e)
    return None


async def _current_key_for_base(base_url: str) -> Optional[str]:
    """按 base_url 在 model-configs 全表找当前 key（v1.6.3 C2）。"""
    return await _current_key_for_provider_or_base(base_url=base_url)


async def _overlay_current_key(llm_cfg: Optional[dict]) -> Optional[dict]:
    """缓存防腐（v1.6.2/v1.6.3/v1.8.1）：按 provider_id 优先、base_url 兜底，以 DB 当前真实 key 覆盖前端缓存或脱敏串。"""
    if not llm_cfg:
        return llm_cfg
    req_pid = str(llm_cfg.get("provider_id") or "").strip()
    req_base = _norm_base_url(llm_cfg.get("base_url") or "")
    raw_key = str(llm_cfg.get("api_key") or "")
    if not req_base and not req_pid:
        if is_masked_secret(raw_key):
            return {**llm_cfg, "api_key": ""}
        return llm_cfg
    current_key = None
    if req_pid:
        current_key = await _current_key_for_provider_or_base(provider_id=req_pid, base_url=req_base)
    if not current_key and req_base:
        current = await _active_llm_cfg()
        if current and _norm_base_url(current.get("base_url") or "") == req_base:
            current_key = (current.get("api_key") or "").strip() or None
        if not current_key:
            current_key = await _current_key_for_base(req_base)
    if current_key and current_key != raw_key:
        return {**llm_cfg, "api_key": current_key}
    if is_masked_secret(raw_key):
        return {**llm_cfg, "api_key": ""}
    return llm_cfg


async def _active_llm_cfg() -> Optional[dict]:
    """读取用户当前生效的模型配置（active-model + model-configs），无可用 key 返回 None。

    v1.6.0（发布后修复）：用户在设置里换了 API Key 后，旧对话 model_config 里冻存的
    仍是旧 key —— 未显式点选模型就发消息时按旧 key 请求，产生 401（实测：Settings 新
    key 有效、对话冻存旧 key 无效）。修复后的优先级：本次显式选择 > 当前激活配置 >
    对话冻存配置（最后兜底）。
    """
    try:
        async with session_scope() as s:
            if s is None:
                return None
            a_row = await s.get(LocalSetting, "active-model")
            c_row = await s.get(LocalSetting, "model-configs")
            active = a_row.payload if (a_row and isinstance(a_row.payload, dict)) else {}
            configs = c_row.payload if (c_row and isinstance(c_row.payload, dict)) else {}
        cfg = decrypt_nested(configs).get(str(active.get("providerId") or "")) or {}
        key = str(cfg.get("apiKey") or "").strip()
        if not key:
            return None
        base_url = str(cfg.get("baseUrl") or "").strip()
        # v1.6.4：baseUrl 为空（自定义/中转站漏填 URL）视为未配置 —— 否则 llm._resolve 会把
        # 空地址回退到官方 DeepSeek 端点，拼成「官方 URL + 中转 key」的必 401 组合。
        # 返回 None 让调用方回落对话冻存配置/环境变量，错误如实暴露而不是错配发出。
        if not base_url:
            return None
        model = str(active.get("modelId") or cfg.get("selectedModel") or "").strip()
        out = {"base_url": base_url,
               "api_key": key, "model": model}
        # v1.6.5：思考深度档位随激活配置下发（model-configs 存 camelCase，与 baseUrl/apiKey 同风格）
        effort = str(cfg.get("reasoningEffort") or "").strip().lower()
        if effort in REASONING_EFFORTS:
            out["reasoning_effort"] = effort
        return out
    except Exception as e:  # noqa: BLE001
        # warning 级（曾为 debug）：此函数失败=聊天/研究直接退化成「没有可用模型」，
        # 必须能在 sidecar 日志里看到根因（打包环境曾出现静默失败，靠本日志定位）。
        logger.warning("解析当前激活模型配置失败：%s", e, exc_info=True)
        return None


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
    llm_ok = settings.llm_configured or llm_available(await _active_llm_cfg())
    return {
        "status": "ok",
        "db": "ok" if db_ok else "down",
        "llm": "ok" if llm_ok else "unconfigured",
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
    safe_n = max(1, min(int(n or 60), 1000))
    safe_adj = adjust if adjust in ("qfq", "hfq", "") else "qfq"
    return await _market_call(get_kline, code, safe_n, safe_adj)


@router.get("/quote/{code}/book")
async def book(code: str) -> dict:
    return await _book_call(get_bid_ask, code)


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
        payload = row.payload if row else None
    # v1.8.1（审计 C-02）：公共 API 严禁返回明文 API Key。
    # 读取时先透明解密（兼容旧明文行），再脱敏为 ••••••••xxxx；PUT 保存时若收到未改动的脱敏串会自动保留库内真实密文。
    if key == "model-configs" and isinstance(payload, dict):
        payload = mask_nested(decrypt_nested(payload))
    return {"key": key, "payload": payload}


@router.put("/local-settings/{key}")
async def put_local_setting(key: str, req: LocalSettingRequest) -> dict:
    payload = req.payload
    if key == APP_SETTINGS_KEY:
        payload = normalize_app_settings(payload)  # v1.8.0：落库前校验，坏值回落默认
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        row = await s.get(LocalSetting, key)
        if key == "model-configs" and isinstance(payload, dict):
            existing = row.payload if (row and isinstance(row.payload, dict)) else {}
            payload = encrypt_nested(merge_masked_nested(payload, existing))
        if row:
            row.payload = payload
        else:
            s.add(LocalSetting(key=key, payload=payload))
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


_VISION_PROBE_CACHE: dict[tuple[str, str], bool] = {}
_VISION_PROBE_CAP = 128


async def _probe_vision(llm, images: list, *, cache_key: Optional[tuple[str, str]] = None) -> bool:
    """含图时，在 agent 跑之前先探一次模型支不支持视觉（结果按 base_url+model 内存缓存）。"""
    if cache_key and cache_key in _VISION_PROBE_CACHE:
        return _VISION_PROBE_CACHE[cache_key]
    from langchain_core.messages import HumanMessage

    probe = HumanMessage(
        content=[
            {"type": "text", "text": "."},
            {"type": "image_url", "image_url": {"url": images[0]}},
        ]
    )
    try:
        await llm.ainvoke([probe])
        supported = True
    except Exception as e:  # noqa: BLE001
        if _is_vision_error(e):
            logger.info("模型不支持视觉，本轮去图降级：%s", e)
            supported = False
        else:
            # 非视觉错误（网络/认证）不写入缓存，交由正式调用暴露完整错误
            logger.debug("视觉探测遇到非视觉错误，按支持处理，交由正式调用暴露：%s", e)
            return True
    if cache_key and cache_key[1]:
        _VISION_PROBE_CACHE[cache_key] = supported
        if len(_VISION_PROBE_CACHE) > _VISION_PROBE_CAP:
            for k in list(_VISION_PROBE_CACHE)[: len(_VISION_PROBE_CACHE) - _VISION_PROBE_CAP]:
                _VISION_PROBE_CACHE.pop(k, None)
    return supported


def _thread_has_dangling_turn(thread_msgs: list) -> bool:
    """检查 LangGraph 检查点线程是否因上次中途取消而残留悬挂的 tool_calls 或未回复提问。"""
    if not thread_msgs:
        return False
    last_type = str(getattr(thread_msgs[-1], "type", "") or "").lower()
    if last_type in ("human", "tool"):
        return True
    pending_tc: set[str] = set()
    for m in thread_msgs:
        mtype = str(getattr(m, "type", "") or "").lower()
        if mtype == "ai":
            for tc in getattr(m, "tool_calls", None) or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id:
                    pending_tc.add(str(tc_id))
        elif mtype == "tool":
            tc_id = getattr(m, "tool_call_id", None)
            if tc_id:
                pending_tc.discard(str(tc_id))
    return bool(pending_tc)


async def _load_compaction(conv_id: str) -> tuple[int, str]:
    """会话当前的压缩代际与前情摘要（无记录 = 第 0 代、无摘要）。"""
    async with session_scope() as s:
        if s is None:
            return 0, ""
        row = await s.get(ChatContextSummary, conv_id)
        if row is None:
            return 0, ""
        return int(row.generation or 0), str(row.summary or "")


async def _save_compaction(conv_id: str, generation: int, summary: str, cutoff: int, model: str) -> None:
    async with session_scope() as s:
        if s is None:
            return
        if await s.get(ChatConversation, conv_id) is None:
            return
        row = await s.get(ChatContextSummary, conv_id)
        if row is None:
            row = ChatContextSummary(conversation_id=conv_id)
            s.add(row)
        row.generation = generation
        row.summary = summary
        row.cutoff_count = cutoff
        row.history_path = ""
        row.model = (model or "")[:160]
        await s.commit()


def _with_turn_context(content: Any, turn_context: str) -> Any:
    """把【本轮上下文】块前置到本轮用户消息（纯文本或多模态首个文本块）。"""
    if not turn_context:
        return content
    if isinstance(content, list):
        blocks = list(content)
        for i, b in enumerate(blocks):
            if isinstance(b, dict) and b.get("type") == "text":
                blocks[i] = {**b, "text": f"{turn_context}\n\n{b.get('text') or ''}"}
                return blocks
        return [{"type": "text", "text": turn_context}, *blocks]
    return f"{turn_context}\n\n{content or ''}"


async def _deepagent_chat_stream(
    *,
    conv_id: str,
    text: str,
    history: list,
    profile: dict,
    llm_cfg: Optional[dict],
    images: Optional[list] = None,
    user_msg: Optional[dict] = None,
    prefs: Optional[dict] = None,
    turn_context: str = "",
    title: str = "",
):
    """deepagents 对话链路：产出 (sse_line, text_piece)。

    取数决定权在模型手里：不再每轮预取行情塞 prompt，模型自己决定调什么工具、调几次。

    v1.8.0 上下文治理（参考 Codex auto-compact，见 agents/deep/compaction.py）：
    - 线程用量（中文经验估算）超过窗口 × 阈值 → 中文交接摘要 → 线程代际 +1，新线程以
      「摘要 + 预算内的最近几轮」播种；
    - 未播种线程（老会话/检查点丢失）不再把整个 DB 历史无预算地塞进去；
    - 时间锚点与本会话新完成的研究报告作为【本轮上下文】前置到用户消息，系统提示保持稳定。
    """
    from ..agents.deep import compaction
    from ..agents.deep.chat_agent import build_chat_agent
    from ..tools.context import _to_lc_content

    prefs = prefs or {}
    checkpointer, _kind = get_checkpointer()
    model_name = str((llm_cfg or {}).get("model") or settings.llm_model or "")
    window = context_window_tokens(model_name, int(prefs.get("context_window_k") or 0))
    learned = await kb_service.learned_block(bool(prefs.get("learn_profile", True)))
    agent = build_chat_agent(
        llm_config=llm_cfg,
        profile=profile,
        checkpointer=checkpointer,
        learned_block=learned,
        knowledge_enabled=bool(prefs.get("knowledge_enabled", True)),
        learn_enabled=bool(prefs.get("learn_profile", True)),
        context_window=window,
    )
    if agent is None:
        return

    generation, summary = await _load_compaction(conv_id)

    def _config(gen: int) -> dict:
        return {
            "configurable": {"thread_id": compaction.thread_id_for(conv_id, gen)},
            "recursion_limit": int(settings.agent_recursion_limit),
        }

    config = _config(generation)

    # 多模态：避免预发 max_tokens=1 探测请求（部分视觉/推理端点对 max_tokens=1 直接报 400，
    # 旧实现曾误判为不支持视觉并永久写死缓存）。现改为仅命中已确认不支持的缓存时提前降级，
    # 首次带图直接走主请求；若首帧前确因视觉模态报错再精准缓存并无感去图重试。
    note = ""
    v_key = (_norm_base_url(str((llm_cfg or {}).get("base_url") or settings.llm_base_url or "")), model_name)
    if images and _VISION_PROBE_CACHE.get(v_key) is False:
        note = f"（当前模型不支持图片识别，已忽略 {len(images)} 张图片，仅根据文字回复。）\n\n"
        images = None

    def _content(msg: dict):
        """带图消息 → 多模态 content blocks；无图 → 纯字符串。复用既有转换，不另造。"""
        return _to_lc_content(msg) if msg.get("images") else (msg.get("content") or "")

    this_turn = dict(user_msg or {"role": "user", "content": text})
    if images:
        this_turn["images"] = images
    else:
        this_turn.pop("images", None)
    current = {"role": "user", "content": _with_turn_context(_content(this_turn), turn_context)}

    try:
        state = await agent.aget_state(config)
        thread_msgs = list((state.values or {}).get("messages") or [])
    except Exception as e:  # noqa: BLE001
        logger.warning("aget_state 失败，按未播种处理：%s", e)
        thread_msgs = []

    if thread_msgs and _thread_has_dangling_turn(thread_msgs):
        # 上一轮在工具调用中途或首字前被取消，检查点残留未闭合的 tool_calls / HumanMessage，
        # 继续往同一线程追加会导致模型网关报 400（tool_calls 缺少对应 tool 消息）。
        # 自愈：代际 +1 切新线程，由干净的 DB 历史 + 既有摘要重新播种。
        generation += 1
        await _save_compaction(conv_id, generation, summary, max(0, len(history or []) - 1), model_name)
        config = _config(generation)
        thread_msgs = []
        logger.info("检测到未闭合检查点线程，已自愈切换至新代际 conv=%s gen=%d", conv_id, generation)

    if thread_msgs:
        decision = compaction.decide(
            thread_msgs, text + turn_context, window, int(prefs.get("compact_threshold_pct") or 70)
        )
        if decision.needed:
            yield sse.ev_chat_stage("compacting", "上下文较长，正在压缩较早的对话…"), ""
            sum_llm = build_llm(streaming=False, override=llm_cfg)
            thinks = bool(reasoning_effort_of(llm_cfg)) or is_reasoning_model(model_name)
            new_summary, by_model = await compaction.summarize(
                sum_llm, thread_msgs, summary, window, timeout=300.0 if thinks else 120.0
            )
            generation += 1
            summary = new_summary
            prior_count = max(0, len(history or []) - 1)
            await _save_compaction(conv_id, generation, summary, prior_count, model_name)
            try:
                await kb_service.index_conversation_summary(conv_id, title, summary, generation)
            except Exception as e:  # noqa: BLE001
                logger.warning("对话摘要入知识库失败：%s", e)
            logger.info(
                "对话自动压缩 conv=%s 代际→%d（估算 %d/%d token，模型摘要=%s）",
                conv_id, generation, decision.used_tokens, window, by_model,
            )
            yield sse.sse({
                "type": "context_compacted",
                "generation": generation,
                "used_tokens": decision.used_tokens,
                "window": window,
            }), ""
            config = _config(generation)
            thread_msgs = []

    if thread_msgs:
        msgs = [current]
    else:
        # 未播种：前情摘要（若有）+ 预算内最近几轮原文 + 本轮。DB 历史末条即本轮提问，排除。
        prior = list(history or [])
        if prior and prior[-1].get("role") == "user":
            prior = prior[:-1]
        budget = int(window * (0.2 if summary else 0.5))
        msgs = compaction.seed_messages(summary, prior, budget) + [current]

    if note:
        yield sse.ev_token(note), note

    emitted_any = False
    try:
        async for line, piece in stream_agent_sse(agent, {"messages": msgs}, config):
            if piece:
                emitted_any = True
            yield line, piece
    except Exception as e:
        if images and not emitted_any and _is_vision_error(e):
            logger.info("模型明确拒绝视觉模态，缓存并自动去图重试：%s", e)
            if v_key[1]:
                _VISION_PROBE_CACHE[v_key] = False
                if len(_VISION_PROBE_CACHE) > _VISION_PROBE_CAP:
                    for k in list(_VISION_PROBE_CACHE)[: len(_VISION_PROBE_CACHE) - _VISION_PROBE_CAP]:
                        _VISION_PROBE_CACHE.pop(k, None)
            fallback_note = f"（当前模型不支持图片识别，已忽略 {len(images)} 张图片，仅根据文字回复。）\n\n"
            yield sse.ev_token(fallback_note), fallback_note
            text_only_turn = dict(this_turn)
            text_only_turn.pop("images", None)
            retry_current = {"role": "user", "content": _with_turn_context(_content(text_only_turn), turn_context)}
            # 首次尝试可能已在检查点留下未闭合的带图 HumanMessage，代际 +1 切干净线程重试
            generation += 1
            await _save_compaction(conv_id, generation, summary, max(0, len(history or []) - 1), model_name)
            retry_config = _config(generation)
            prior = list(history or [])
            if prior and prior[-1].get("role") == "user":
                prior = prior[:-1]
            budget = int(window * (0.2 if summary else 0.5))
            retry_msgs = compaction.seed_messages(summary, prior, budget) + [retry_current]
            async for line, piece in stream_agent_sse(agent, {"messages": retry_msgs}, retry_config):
                yield line, piece
            return
        raise


async def _research_turn_context(history: list) -> str:
    """本会话里、上一轮正常回复之后新出现的研究卡片 → 【本轮上下文】里的研究摘要块。

    每份研究只在它之后的第一轮对话注入一次（之后由 search_knowledge 按需检索），避免
    每轮重复塞整份报告撑爆上下文。
    """
    pending: list[dict] = []
    for m in reversed((history or [])[:-1]):
        if m.get("kind") == "research":
            pending.append(m.get("research") or {})
        elif m.get("role") == "assistant" and not m.get("kind"):
            break
    if not pending:
        return ""
    lines: list[str] = []
    async with session_scope() as s:
        if s is None:
            return ""
        for info in reversed(pending):
            task_id = str(info.get("task_id") or "")
            if not task_id:
                continue
            task = await s.get(ResearchTask, task_id)
            label = TEMPLATE_LABELS.get(str(info.get("template") or ""), "研究")
            who = f"{info.get('name') or info.get('code') or ''}（{info.get('code') or ''}）"
            if task is None:
                continue
            if task.status == "done" and task.report_id:
                rep = await s.get(Report, task.report_id)
                doc = (rep.payload if rep else None) or {}
                titles = "、".join(str(x.get("title")) for x in (doc.get("sections") or [])[:6] if isinstance(x, dict))
                lines.append(
                    f"· {who}{label}报告（数据时点 {doc.get('as_of') or '未知'}，report_id={task.report_id}）"
                    f"摘要：{str(doc.get('summary') or '')[:360].rstrip('。')}"
                    + (f"；章节：{titles}" if titles else "")
                    + "。需要章节细节时用 search_knowledge 检索。"
                )
            elif task.status in ("pending", "running"):
                lines.append(f"· {who}{label}研究仍在进行中，完成后会自动进入知识库。")
            else:
                lines.append(f"· {who}{label}研究未能完成：{(task.error or '原因未知')[:80]}。")
    if not lines:
        return ""
    return "【本会话新完成的研究】\n" + "\n".join(lines)[:1800]


async def _build_turn_context(history: list) -> str:
    parts = [render_time_anchor()]
    research = await _research_turn_context(history)
    if research:
        parts.append(research)
    return "【本轮上下文】\n" + "\n".join(parts) + "\n【本轮上下文结束】"


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
    """含图消息首个 chunk 失败时，判断是否为视觉模态不支持（可降级去图重试）。

    仅匹配明确的图像/多模态不支持错误，绝不把泛化的 400 / bad request（如参数或上下文报错）
    误判为模型不支持视觉，防止误伤多模态模型并污染缓存。
    """
    msg = str(e).lower()
    if any(kw in msg for kw in ("too large", "too long", "too big", "exceed", "rate limit", "quota")):
        return False
    if any(kw in msg for kw in (
        "image_url", "input_image", "multimodal", "vision",
        "图片", "图像", "视觉", "多模态",
    )):
        return True
    if "image" in msg and any(kw in msg for kw in ("not support", "unsupported", "invalid", "unknown", "expected string")):
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
    """同源展示已卸载媒体；路径经过白名单与目录边界校验，附带防嗅探与沙箱 CSP 头。"""
    try:
        path = media_path(conversation_id, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, "media not found")
    return FileResponse(
        path,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
        },
    )


def _iso_utc(value) -> str:
    """v1.6.0（审计 B5）：naive-UTC（SQLite func.now 实际存储）→ aware ISO，
    前端拿到带 +00:00 的规范时间戳，不再与本地时间差 8 小时。"""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.isoformat()


def _chat_meta(conv: ChatConversation, messages: Optional[list[dict]] = None) -> dict:
    return {
        "id": conv.id,
        "title": conv.title,
        "snapshot_interval": conv.snapshot_interval,
        "model_config": mask_config(conv.model_config),
        "archived_at": _iso_utc(conv.archived_at) or None,
        "created_at": _iso_utc(conv.created_at),
        "updated_at": _iso_utc(conv.updated_at),
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
    resolved_llm = await _overlay_current_key(req.llm)
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        conv = ChatConversation(
            title=(req.title or "新对话")[:160],
            snapshot_interval=max(2, min(int(req.snapshot_interval or 8), 50)),
            model_config=encrypt_config(resolved_llm),
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
    embedded_tasks: list[str] = []
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        conv = await s.get(ChatConversation, conversation_id)
        if not conv:
            raise HTTPException(404, "conversation not found")
        summary_row = await s.get(ChatContextSummary, conversation_id)
        generation = int(getattr(summary_row, "generation", 0) or 0) if summary_row else 0
        conv_msgs = await _chat_history(s, conversation_id)
        for m in conv_msgs:
            if m.get("kind") == "research" and isinstance(m.get("research"), dict):
                tid = str(m["research"].get("task_id") or "").strip()
                if tid:
                    embedded_tasks.append(tid)
        await s.execute(delete(ChatDelta).where(ChatDelta.conversation_id == conversation_id))
        await s.execute(delete(ChatContextSummary).where(ChatContextSummary.conversation_id == conversation_id))
        await s.delete(conv)
        await s.commit()
    # 若该会话内有尚未生成报告的研究任务，同步取消其后台协程并清理无报告孤儿任务行
    for tid in set(embedded_tasks):
        rt = _RESEARCH_RUNS.get(tid)
        if rt is not None and rt.task and not rt.task.done():
            rt.cancel_reason = "所属对话已删除，研究任务已取消"
            rt.task.cancel()
        async with session_scope() as st:
            if st is not None:
                t_row = await st.get(ResearchTask, tid)
                if t_row and not t_row.report_id:
                    await st.delete(t_row)
                    await st.commit()
                    _TASK_LLM.pop(tid, None)
                    research_clear_task(tid)
    # v1.6.0（审计 B4）：会话已删、写锁已释放，再做副作用清理（失败不让删除回滚）——
    # 1) LangGraph 检查点线程（thread_id == conv_id）：删报告早有对称清理，删会话此前没有，
    #    检查点按 super-step 全量序列化消息，会话越长库越肥且删后永久残留；
    # 2) 媒体目录；3) 上下文诊断行；4) 会话锁条目（核验补）。
    _CHAT_LOCKS.pop(conversation_id, None)
    cp, _kind = get_checkpointer()
    if cp is not None and hasattr(cp, "adelete_thread"):
        from ..agents.deep.compaction import all_thread_ids

        # v1.8.0：自动压缩会产生多代线程（conv、conv:g1…），逐代删除
        for thread_id in all_thread_ids(conversation_id, generation):
            try:
                await cp.adelete_thread(thread_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("删除检查点线程失败 thread=%s：%s", thread_id, e)
    await kb_service.on_conversation_deleted(conversation_id)
    try:
        await asyncio.to_thread(delete_conversation_data, conversation_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("删除会话媒体目录失败 conv=%s：%s", conversation_id, e)
    async with session_scope() as s2:
        if s2 is not None:
            await s2.execute(delete(LocalSetting).where(LocalSetting.key == f"context-diag:chat:{conversation_id}"))
            await s2.commit()
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
# 斜体：两侧不能紧邻单词字符/括号/百分号，避免吃掉 3*5、(A+B)*C*(D+E)、10%*50%*2 与 snake_case
_MD_ITALIC = re.compile(r"(?<![\w*)\]\d%])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*([\d])")
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


# ────────────────────────────────────────────────────────────
# 聊天 SSE 心跳（v1.7.0）
# ────────────────────────────────────────────────────────────
_CHAT_HEARTBEAT_S = 10.0
# CoT/工具静默期心跳间隔：远小于前端看门狗（普通 120s / 思考 330s）与 SSE 中间层空闲超时。


async def _pump_with_heartbeat(
    source: AsyncIterator,
    *,
    interval: float = _CHAT_HEARTBEAT_S,
    fallback_stage: tuple[str, str] = ("thinking", "正在思考"),
    max_idle: float = 360.0,
):
    """给上游 (line, piece) 流补心跳：静默超过 interval 秒就重发最近一次 stage 事件（带 heartbeat:true）。"""
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    async def _pump() -> None:
        try:
            async with contextlib.aclosing(source) as agen:
                async for item in agen:
                    queue.put_nowait(item)  # 无界队列：put_nowait 永不阻塞
            queue.put_nowait(done)
        except BaseException as e:  # noqa: BLE001  含 CancelledError：作哨兵投递，不在 task 里裸抛
            try:
                queue.put_nowait(e)
            except Exception:  # noqa: BLE001
                pass

    pump = asyncio.create_task(_pump())
    last_stage = fallback_stage
    idle_elapsed = 0.0
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
                idle_elapsed = 0.0
            except asyncio.TimeoutError:
                idle_elapsed += interval
                if max_idle and idle_elapsed >= max_idle:
                    raise TimeoutError("模型或工具长时间无响应，请重试或切换模型")
                yield sse.sse({"type": "stage", "stage": last_stage[0], "label": last_stage[1], "heartbeat": True}), ""
                continue
            if item is done:
                break
            if isinstance(item, BaseException):
                raise item  # 上游异常原样转交调用方（走既有 except 分支：日志/failed_note/闭环）
            line, piece = item
            if not piece and '"stage"' in line:  # 低频行才解析（stage 行 piece 恒空）
                try:
                    ev = json.loads(line[6:])
                    if ev.get("type") == "stage":
                        last_stage = (ev.get("stage") or last_stage[0],
                                      ev.get("label") or last_stage[1])
                except Exception:  # noqa: BLE001
                    pass
            yield item
    finally:
        pump.cancel()
        try:
            with anyio.CancelScope(shield=True):
                await asyncio.wait_for(pump, timeout=2.0)
        except BaseException:  # noqa: BLE001  有界收尾：不拖死生成器关闭，原取消（若有）继续上传
            pass


@router.post("/chat/conversations/{conversation_id}/messages")
async def send_chat_message(conversation_id: str, req: ChatSendRequest, request: Request = None):
    """流式对话：POST 返回 text/event-stream。

    事件序列：stage(understanding/thinking/composing) → 多个 token → done(含 conversation/messages/reply)。
    用户消息在流式开始前持久化（独立 session 提交）；助手回复在流式结束后持久化。
    前端用 fetch + ReadableStream 解析（EventSource 仅支持 GET，无法带 llm body）。
    """
    try:
        cleaned_images = validate_images_input(req.images)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    text = (req.message or "").strip()
    if not text and cleaned_images:
        text = "请结合上传的图片进行分析。"
    if not text:
        raise HTTPException(400, "message is empty")

    # v1.8.1+：在写库前原子获取会话锁；给刚点击「停止」立刻重发的请求留 0.8s 收尾宽限窗口。
    chat_lock = _chat_lock(conversation_id)
    try:
        await asyncio.wait_for(chat_lock.acquire(), timeout=0.8)
    except asyncio.TimeoutError as exc:
        raise HTTPException(409, "该对话仍在生成上一条回复，请等它完成或先停止") from exc
    lock_transferred = False
    try:
        resolved_req_llm = await _overlay_current_key(req.llm)
        user_msg = _chat_msg("user", text, images=cleaned_images)
        # 在开启 SQLite 读事务前完成图片落盘卸载，避免 DEFERRED 读快照期间夹杂磁盘 I/O 触发锁升级冲突
        stored_user_msg = (
            await asyncio.to_thread(offload_message_media, user_msg, conversation_id)
            if cleaned_images else user_msg
        )
        # 1) 持久化用户消息（独立 session，提交后才进入流式，确保即使流式中断也保留提问）
        async with session_scope() as s:
            if s is None:
                raise HTTPException(503, "local database unavailable")
            conv = await s.get(ChatConversation, conversation_id)
            if not conv:
                raise HTTPException(404, "conversation not found")
            if conv.archived_at is not None:
                raise HTTPException(409, "归档对话仅供查看，恢复后可继续")
            history = await _chat_history(s, conversation_id)
            history = await _chat_append(s, conv, stored_user_msg, history)
            if resolved_req_llm is not None:
                conv.model_config = encrypt_config(resolved_req_llm)
            if conv.title == "新对话":
                conv.title = text[:36]
            conv.updated_at = dt.datetime.now(dt.UTC)
            await s.commit()
            conv_id = conv.id
            conv_model_config = conv.model_config  # 会话已提交，读值出域
            history_snapshot = list(history)
            # 数据库存引用，当前这一轮仍把原始 data URL 交给视觉模型。
            if cleaned_images and history_snapshot:
                history_snapshot[-1] = user_msg
            prof_row = await s.get(LocalSetting, "user-profile")
            user_profile = (prof_row.payload if prof_row else None) or {}
            conv_title = conv.title or ""

        # 模型配置优先级：
        #   ① 本次显式选择（req.llm，已做 DB 最新 key 覆盖）
        #   ② 当前激活配置（active-model + model-configs）
        #   ③ 对话冻存配置（最后兜底）
        llm_cfg = resolved_req_llm
        if llm_cfg is None:
            llm_cfg = await _active_llm_cfg()
        if llm_cfg is None:
            llm_cfg = await _overlay_current_key(decrypt_config(conv_model_config))

        has_model = llm_available(llm_cfg)
        app_prefs = await load_app_settings()
        turn_context = await _build_turn_context(history_snapshot) if has_model else ""
        if app_prefs.get("learn_profile", True):
            await kb_service.record_chat_topics(text)

        gen_started = False

        async def _release_if_unconsumed() -> None:
            await asyncio.sleep(15.0)
            if not gen_started and chat_lock.locked():
                chat_lock.release()

        try:
            asyncio.get_running_loop().create_task(_release_if_unconsumed())
        except RuntimeError:
            pass

        async def gen():
            nonlocal gen_started
            gen_started = True
            # 统一闭环：无论是否有模型/是否出错/是否被取消，都最终持久化助手回复；正常路径再发 done。
            reply_text = ""
            violated = False
            interrupted = False   # CancelledError / 生成器被弃置（GeneratorExit）
            completed = False     # 正常走完流式主体
            failed_note = ""

            async def _persist_reply(final_text: str):
                """持久化助手回复（独立 session），返回 (meta, messages, assistant_msg)。"""
                assistant_msg = _chat_msg("assistant", final_text)
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
                return final_meta, final_messages, assistant_msg

            _ut_token = kb_service.CURRENT_USER_TEXT.set(text)
            try:
                if not has_model:
                    reply_text = (
                        "⚠️ 当前还没有可用模型。\n\n"
                        "请在「模型配置」中保存 API Key 与模型，再回到对话继续提问。"
                    )
                    yield sse.ev_chat_stage("composing", "正在组织回复")
                    await asyncio.sleep(0.1)
                    yield sse.ev_token(reply_text)
                else:
                    try:
                        gate_acc = ""
                        async with contextlib.aclosing(_pump_with_heartbeat(_deepagent_chat_stream(
                            conv_id=conv_id,
                            text=text,
                            history=history_snapshot,
                            profile=user_profile,
                            llm_cfg=llm_cfg,
                            images=cleaned_images,
                            user_msg=history_snapshot[-1] if history_snapshot else None,
                            prefs=app_prefs,
                            turn_context=turn_context,
                            title=conv_title,
                        ))) as hb_stream:
                            async for _line, _piece in hb_stream:
                                if request is not None and await request.is_disconnected():
                                    interrupted = True
                                    break
                                if _piece:
                                    gate_acc += _piece
                                    if check_generated(gate_acc, streaming=True):
                                        violated = True
                                        break
                                yield _line
                                reply_text += _piece
                    except asyncio.CancelledError:
                        interrupted = True
                        raise
                    except Exception as e:  # noqa: BLE001
                        logger.exception("对话链路失败")
                        safe_err = sanitize_err_text(e)
                        failed_note = f"\n\n⚠️ 模型调用失败：{safe_err}"
                        yield sse.ev_error(safe_err)
                    if not interrupted:
                        if violated:
                            note = "\n\n（以上内容包含受限的投资建议表述，已按合规要求截断。本助手不提供买卖建议。）"
                            reply_text = _strip_md(reply_text) + note
                            yield sse.ev_token(note)
                        elif reply_text and check_generated(reply_text, streaming=False):
                            violated = True
                            note = "\n\n（以上内容包含受限的投资建议表述，已按合规要求撤回。本助手不提供买卖建议。）"
                            reply_text = "（内容包含受限的投资建议表述，已按合规要求撤回。本助手不提供买卖建议。）"
                            yield sse.ev_token(note)
                        if not reply_text and not failed_note:
                            reply_text = "⚠️ 模型返回为空。可以换一个模型或把问题拆得更具体。"
                            yield sse.ev_token(reply_text)

                if reply_text.strip():
                    reply_text = _strip_md((reply_text + failed_note).strip())
                else:
                    reply_text = ""
                completed = not interrupted and not (failed_note and not reply_text)
            finally:
                try:
                    kb_service.CURRENT_USER_TEXT.reset(_ut_token)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    final_text = reply_text
                    if interrupted or not completed:
                        if final_text.strip():
                            final_text = final_text + "\n\n（生成中断）"
                    if final_text.strip():
                        try:
                            with anyio.CancelScope(shield=True):
                                final_meta, final_messages, assistant_msg = await _persist_reply(final_text)
                        except Exception:  # noqa: BLE001
                            logger.exception("中断/收尾落库失败 conv=%s", conv_id)
                            final_meta, final_messages, assistant_msg = None, [], None
                    else:
                        final_meta, final_messages, assistant_msg = None, [], None
                finally:
                    if chat_lock.locked():
                        chat_lock.release()
            if completed and not interrupted:
                yield sse.sse({
                    "type": "done",
                    "conversation": final_meta,
                    "messages": final_messages,
                    "reply": assistant_msg,
                    "channel": "deltachannel",
                })

        lock_transferred = True
        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    finally:
        if not lock_transferred and chat_lock.locked():
            chat_lock.release()


class ResearchRequest(BaseModel):
    code: str
    template: str = "equity_deep"
    query: Optional[str] = None
    llm: Optional[dict] = None  # {base_url, api_key, model}：前端所选模型
    # v1.8.0：研究嵌入对话 —— 带会话 id 时把「研究请求 + 研究卡片」两条消息写进该会话
    conversation_id: Optional[str] = None
    name: Optional[str] = None  # 标的显示名（仅用于卡片标题）


def _research_task_meta(t: ResearchTask, name: Optional[str] = None) -> dict:
    resolved_name = (
        (name or "").strip()
        or (getattr(t, "name", None) or "").strip()
        or lookup_stock_name(t.code)
        or t.code
    )
    return {
        "id": t.id,
        "code": t.code,
        "name": resolved_name,
        "template": t.template,
        "query": t.query,
        "status": t.status,        # pending/running/done/error
        "stage": t.stage,          # pending/entity/research/market/report/done/error
        "report_id": t.report_id,
        "error": t.error,
        "created_at": _iso_utc(t.created_at),
        "updated_at": _iso_utc(t.updated_at),
    }


# /pipeline/stages 已移除：它返回的是固定管线的五个节点名，供前端画那条写死的进度条。
# 现在研究由 agent 编排，阶段是运行时真实发生的工具调用（tool_call 事件），
# 不再有可以提前枚举的"阶段表"。前端已改为按 tool_call 实时渲染，不再调用本端点。


@router.get("/research/tasks")
async def list_research_tasks(limit: int = 50) -> dict:
    """研究任务列表（用于深度研究任务台恢复历史与运行中状态，并自动收口上次进程遗留的僵死任务）。"""
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        stmt = (
            select(ResearchTask)
            .order_by(ResearchTask.updated_at.desc())
            .limit(max(1, min(int(limit or 50), 200)))
        )
        rows = list((await s.execute(stmt)).scalars().all())
        dirty = False
        for t in rows:
            if t.status in ("running", "pending"):
                runtime = _RESEARCH_RUNS.get(t.id)
                is_active = bool(runtime and runtime.task and not runtime.task.done())
                age_s = _task_age_seconds(t.updated_at or t.created_at)
                if not is_active and (t.status == "running" or age_s > 60):
                    t.status = "error"
                    t.stage = "error"
                    t.error = t.error or "研究任务因应用重启而中断，请重新提交。"
                    dirty = True
        if dirty:
            await s.commit()
        rep_ids = [t.report_id for t in rows if t.report_id]
        rep_names: dict[str, str] = {}
        if rep_ids:
            r_rows = (await s.execute(select(Report.id, Report.name).where(Report.id.in_(rep_ids)))).all()
            rep_names = {str(rid): str(rname or "") for rid, rname in r_rows}
        return {"tasks": [_research_task_meta(t, rep_names.get(t.report_id or "")) for t in rows]}


@router.post("/research/{task_id}/cancel")
async def cancel_research_task(task_id: str) -> dict:
    """取消运行中或排队中的研究任务，释放模型流连接与内存槽位。"""
    cancel_msg = "用户已取消研究任务"
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        t = await s.get(ResearchTask, task_id)
        if not t:
            raise HTTPException(404, "task not found")
        if t.status in ("done", "error"):
            return {"ok": True, "task_id": task_id, "status": t.status}
        t.status = "error"
        t.stage = "error"
        t.error = cancel_msg
        await s.commit()
    runtime = _RESEARCH_RUNS.get(task_id)
    if runtime is not None:
        runtime.cancel_reason = cancel_msg
        err_frame = sse.ev_error(cancel_msg)
        runtime.record(err_frame)
        runtime.terminal = True
        for q in tuple(runtime.subscribers):
            q.put_nowait(err_frame)
            q.put_nowait(None)
        if runtime.task and not runtime.task.done():
            runtime.task.cancel()
    _TASK_LLM.pop(task_id, None)
    research_clear_task(task_id)
    return {"ok": True, "task_id": task_id, "status": "cancelled"}


@router.delete("/research/{task_id}")
async def delete_research_task(task_id: str) -> dict:
    """删除研究任务（审计 B4）：失败/中断任务此前没有任何删除入口，检查点线程永久残留。

    有关联报告的任务走报告库删除（连带任务与检查点），这里 409 引导。
    """
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        t = await s.get(ResearchTask, task_id)
        if not t:
            raise HTTPException(404, "task not found")
        if t.report_id:
            raise HTTPException(409, "该任务已生成报告，请从报告库删除（连带任务与检查点）")
        await s.delete(t)
        await s.execute(delete(LocalSetting).where(LocalSetting.key == f"context-diag:research:{task_id}"))
        await s.commit()
    runtime = _RESEARCH_RUNS.pop(task_id, None)
    rt = getattr(runtime, "task", None) if runtime is not None else None
    if rt is not None and not rt.done():
        rt.cancel()
    _TASK_LLM.pop(task_id, None)
    research_clear_task(task_id)
    cp, _kind = get_checkpointer()
    if cp is not None and hasattr(cp, "adelete_thread"):
        try:
            await cp.adelete_thread(task_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("删除检查点线程失败 task=%s：%s", task_id, e)
    return {"ok": True, "deleted": task_id}


def _shutdown_token() -> str:
    """读取 run_server 启动时生成的停机令牌（不存在返回空串 = 开发直跑模式，免鉴权）。"""
    try:
        from pathlib import Path

        from ..config import app_data_dir

        p = Path(app_data_dir()) / "shutdown-token.txt"
        return p.read_text(encoding="utf-8").strip() if p.is_file() else ""
    except Exception:  # noqa: BLE001
        return ""


@router.post("/shutdown")
async def shutdown_backend(request: Request) -> dict:
    """优雅停机（审计 B3）：Electron 退出前先握手，让 lifespan 收尾/检查点 flush 执行。

    run_server 把 uvicorn.Server 实例挂到 app.state；直连源码跑（无挂载）时本端点无效。
    核验修复：要求 X-Shutdown-Token 与启动令牌一致 —— 自定义头强制 CORS 预检，
    跨站 no-cors 表单无法再触发本机后端停机（轻微本地 DoS 面）。
    """
    expected = _shutdown_token()
    if expected:
        import secrets as _secrets

        got = request.headers.get("x-shutdown-token", "")
        if not _secrets.compare_digest(got, expected):
            raise HTTPException(403, "forbidden")
    server = getattr(request.app.state, "uvicorn_server", None)
    if server is not None:
        server.should_exit = True
        return {"ok": True}
    return {"ok": False, "note": "server handle unavailable（源码直跑模式）"}


@router.get("/research/{task_id}")
async def get_research_task(task_id: str) -> dict:
    """单任务当前状态：阶段、阶段事件流、各阶段耗时、错误、关联报告。"""
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        t = await s.get(ResearchTask, task_id)
        if not t:
            raise HTTPException(404, "task not found")
        rep_name = ""
        if t.report_id and not (getattr(t, "name", None) or "").strip():
            rep = await s.get(Report, t.report_id)
            rep_name = str(rep.name or "") if rep else ""
        meta = _research_task_meta(t, rep_name)
        meta["progress_events"] = t.progress_events or []
        meta["timings"] = t.timings or {}
        return meta


def _norm_query(q: Optional[str]) -> str:
    return " ".join(str(q or "").split())


def _llm_fingerprint(cfg: Optional[dict]) -> tuple:
    """影响研究输出的模型参数指纹（不含 api_key：同一供应商换 key 不改变任务语义）。"""
    if not cfg:
        return ()
    return (
        str(cfg.get("base_url") or "").strip().rstrip("/"),
        str(cfg.get("model") or "").strip(),
        reasoning_effort_of(cfg),
    )


@router.post("/research")
async def create_research(req: ResearchRequest) -> dict:
    """创建研究任务；带 conversation_id 时研究以卡片形式嵌入该对话（研究对话融合）。"""
    if req.template not in VALID_TEMPLATES:
        raise HTTPException(400, "template 仅支持 equity_deep/industry/earnings/risk")
    conv_id = (req.conversation_id or "").strip()
    if conv_id:
        # 先校验会话再建任务：会话不存在/已归档/正在生成回复时不留下孤儿任务
        if _chat_lock(conv_id).locked():
            raise HTTPException(409, "该对话仍在生成上一条回复，请等它完成或先停止")
        async with session_scope() as s:
            if s is None:
                raise HTTPException(503, "local database unavailable")
            conv = await s.get(ChatConversation, conv_id)
            if not conv:
                raise HTTPException(404, "conversation not found")
            if conv.archived_at is not None:
                raise HTTPException(409, "归档对话仅供查看，恢复后可继续")
    out = await _create_research_task(req)
    if not conv_id:
        return out
    label = TEMPLATE_LABELS.get(req.template, "研究")
    name = (req.name or "").strip() or req.code
    who = name if name == req.code else f"{name}（{req.code}）"
    ask = _chat_msg("user", f"深度研究：{who} · {label}" + (f"（关注：{req.query}）" if req.query else ""))
    ask["kind"] = "research_request"
    card = _chat_msg("assistant", f"已发起{label}研究：{who}。报告生成后会自动进入本地知识库。")
    card["kind"] = "research"
    card["research"] = {"task_id": out["task_id"], "code": req.code, "template": req.template, "name": name}
    # 追加消息时持会话锁：与对话回复互斥，避免两路并发追加拿到同一个 seq（评审）
    async with _chat_lock(conv_id), session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        conv = await s.get(ChatConversation, conv_id)
        if not conv:
            raise HTTPException(404, "conversation not found")
        history = await _chat_history(s, conv_id)
        history = await _chat_append(s, conv, ask, history)
        history = await _chat_append(s, conv, card, history)
        if conv.title == "新对话":
            conv.title = f"研究 · {name}"[:36]
        conv.updated_at = dt.datetime.now(dt.UTC)
        await s.commit()
        await s.refresh(conv)
        meta = _chat_meta(conv, history)
    return {**out, "conversation": meta, "messages": [ask, card]}


async def _create_research_task(req: ResearchRequest) -> dict:
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
                .limit(50)
            )
            candidates = list((await s.execute(stmt)).scalars().all())
            stale = False
            for existing in candidates:
                runtime = _RESEARCH_RUNS.get(existing.id)
                is_active = bool(runtime and runtime.task and not runtime.task.done())
                # v1.6.0（审计 B5）：统一 aware-UTC 计算（详见 _task_age_seconds docstring）
                age_s = _task_age_seconds(existing.updated_at or existing.created_at)
                if not (is_active or (existing.status == "pending" and age_s <= 30)):
                    existing.status = "error"
                    existing.stage = "error"
                    existing.error = "检测到上次进程遗留任务，已终止并重新创建。"
                    stale = True
                    continue
                # v1.7.1（审计 A04）：只有**同一个请求**（同问题、同模型参数）才复用 ——
                # 旧判定只比股票+模板，换个问题或换个模型再提交会被静默并进第一个任务，
                # 新意图丢失、执行的仍是旧模型。不同请求各自建任务。
                # 旧实现只看「最新一条」：同秒创建的多条任务 created_at 相同、排序不定，
                # 真正重复的请求可能匹配不到 —— 现在逐条比对所有仍存活的候选。
                same_request = (
                    _norm_query(existing.query) == _norm_query(req.query)
                    and _llm_fingerprint(decrypt_config(existing.llm_config)) == _llm_fingerprint(req.llm)
                )
                if same_request:
                    if stale:
                        await s.commit()
                    return {"task_id": existing.id, "reused": True}
            if stale:
                await s.commit()
    resolved_llm = await _overlay_current_key(req.llm) if req.llm else None
    resolved_name = (req.name or "").strip() or lookup_stock_name(req.code) or ""
    task_id = uuid.uuid4().hex
    _remember_task_llm(task_id, resolved_llm)
    async with session_scope() as s:
        if s is not None:
            s.add(
                ResearchTask(
                    id=task_id,
                    code=req.code,
                    name=resolved_name[:128] if resolved_name else None,
                    template=req.template,
                    query=req.query,
                    status="pending",
                    llm_config=encrypt_config(resolved_llm),
                )
            )
            await s.commit()
    return {"task_id": task_id, "name": resolved_name or req.code}


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
    replay_progress: list[str] = []
    async with session_scope() as s:
        if s is not None:
            existing = await s.get(ResearchTask, task_id)
            if existing is None:
                # v1.8.0（评审）：任务行不存在 = 报告已在报告库删除（删除连带删任务）或 id 无效。
                # 绝不能落到 pipeline() 用查询参数重跑 —— 研究卡片每次打开都会静默重跑、
                # 烧 token 并把已删报告「复活」进报告库与知识库。
                terminal_frame = sse.ev_error("该研究报告已被删除（或任务不存在），如需可重新发起研究。")
            else:
                for pev in (existing.progress_events or []):
                    if isinstance(pev, dict) and pev.get("node"):
                        replay_progress.append(
                            sse.ev_tool_call(
                                str(pev["node"]),
                                str(pev.get("label") or pev["node"]),
                                "ok",
                            )
                        )
                if existing.status == "done" and existing.report_id:
                    saved = await s.get(Report, existing.report_id)
                    terminal_frame = (
                        sse.sse({"type": "done", "report_id": saved.id, "report": saved.payload or {}})
                        if saved else sse.ev_error("该研究报告已被删除，如需可重新发起研究。")
                    )
                elif existing.status == "error":
                    terminal_frame = sse.ev_error(existing.error or "研究任务失败")
                elif existing.status == "running":
                    runtime = _RESEARCH_RUNS.get(task_id)
                    if not runtime or (not runtime.terminal and (not runtime.task or runtime.task.done())):
                        existing.status = "error"
                        existing.stage = "error"
                        existing.error = "研究任务因应用重启而中断，请重新提交。"
                        await s.commit()
                        terminal_frame = sse.ev_error(existing.error)

    if terminal_frame is not None:
        async def terminal_gen():
            yield _REPLAY_FRAME
            for pframe in replay_progress:
                yield pframe
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

    async def _fail(message: str) -> str:
        """落库 error 终态并返回 error 帧（v1.7.1 审计 A05：所有失败出口统一走这里，
        SSE、runtime 与数据库状态三者一致，失败原因如实保留）。"""
        async with session_scope() as s:
            if s is not None:
                t = await s.get(ResearchTask, task_id)
                if t and t.status != "done":
                    t.status = "error"
                    t.stage = "error"
                    t.error = message
                    await s.commit()
        research_clear_task(task_id)
        _TASK_LLM.pop(task_id, None)
        return sse.ev_error(message)

    async def pipeline():
        # 解析任务参数 + LLM 配置（DB 行优先，回退内存）
        code_, tpl_, q_ = code, template, query
        llm_cfg = _TASK_LLM.get(task_id)
        merged_timings: dict = {}
        user_profile: dict = {}
        async with session_scope() as s:
            if s is not None:
                t = await s.get(ResearchTask, task_id)
                if t is None and not code_:
                    yield sse.ev_error("该研究报告已被删除（或任务不存在），如需可重新发起研究。")
                    return
                if t is not None:
                    if t.status == "done" and t.report_id:
                        saved = await s.get(Report, t.report_id)
                        yield (
                            sse.sse({"type": "done", "report_id": saved.id, "report": saved.payload or {}})
                            if saved else sse.ev_error("该研究报告已被删除，如需可重新发起研究。")
                        )
                        return
                    if t.status == "error":
                        yield sse.ev_error(t.error or "研究任务已取消")
                        return
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
        # v1.6.0 发布后修复：前端未显式选模型时（llm 为 null），此前会落到 env 配置
        # （打包形态=未配置）直接报"模型未配置"。与对话链路同口径：当前激活配置兜底。
        if not llm_cfg:
            llm_cfg = await _active_llm_cfg()
        # v1.6.2 缓存防腐：同供应商 DB 当前 key 覆盖（与对话链路同口径）
        llm_cfg = await _overlay_current_key(llm_cfg)
        if not code_:
            yield await _fail("缺少 code（DB 无此任务且未传 code 查询参数）")
            return
        tpl_ = tpl_ or "equity_deep"
        if not llm_available(llm_cfg):
            yield await _fail("模型未配置：请在「模型配置」中保存 API Key 与模型后重试。")
            return

        yield sse.ev_tool(f"开始研究 {code_}")
        await asyncio.sleep(0)

        graph_t0 = time.perf_counter()
        last_stage_done = graph_t0
        summary_streamed = False
        # v1.7.1：模板化研究走确定性编排（零模型规划往返，见 research_runner docstring）；
        # 标的不是规范代码或显式开启 research_agent_loop 时才交给 agent 主循环。
        direct = (not settings.research_agent_loop) and is_direct_target(code_, tpl_)
        app_prefs = await load_app_settings()
        if direct:
            source = run_research_direct(
                task_id=task_id, code=code_, template=tpl_, query=q_,
                llm_config=llm_cfg, profile=user_profile,
                write_mode=app_prefs.get("research_write_mode"),
            )
        else:
            agent = build_research_agent(
                task_id=task_id,
                template=tpl_,
                llm_config=llm_cfg,
                profile=user_profile,
                checkpointer=get_checkpointer()[0],
                write_mode=app_prefs.get("research_write_mode"),
                query=q_,
            )
            if agent is None:
                yield await _fail("模型未配置：请在「模型配置」中保存 API Key 与模型后重试。")
                return
            ask = f"请研究 {code_}，模板 {tpl_}。"
            if q_:
                ask += f"用户额外关注：{q_}"

            async def _agent_lines():
                # emit_tokens=False：agent 的正文是"好的，开始研究"这类过程话术，不是报告内容；
                # 报告正文由 compose_report 经 custom 事件（section/summary）另行送达。
                async with contextlib.aclosing(stream_agent_sse(
                    agent,
                    {"messages": [{"role": "user", "content": ask}]},
                    {
                        "configurable": {"thread_id": task_id},
                        "recursion_limit": int(settings.agent_recursion_limit),
                    },
                    emit_tokens=False,
                )) as agen:
                    async for _line, _piece in agen:
                        yield _line

            source = _agent_lines()
        try:
            async with contextlib.aclosing(source) as lines:
                async for _line in lines:
                    _ev = _peek_event(_line) or {}
                    if _ev.get("type") in ("summary", "summary_token"):
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
                    # agent 编排路径：compose_report 一旦成功产出草稿即提前结束主循环，
                    # 跳过末尾毫无产出的多余收尾 LLM 轮（省去 5~60s 静默等待，并杜绝收尾轮偶发报错把已成稿作废）
                    if (
                        not direct
                        and _ev.get("type") == "tool_call"
                        and _ev.get("name") == "compose_report"
                        and _ev.get("status") == "ok"
                        and research_get_stored(task_id, "draft_report")
                    ):
                        break

            # 报告不经模型上下文，直接从工具层存储取回（见 research_tools 的设计说明）
            draft = research_get_stored(task_id, "draft_report")
            failure = research_get_stored(task_id, "failure")
            merged_timings.update(research_get_stored(task_id, "timings", {}) or {})
            # v1.6.0（审计 E2）：draft 已取回即确定性释放 _STORE 槽位（配合 LRU，
            # 不再依赖上限挤出；运行中任务不会被后续任务挤出）
            research_clear_task(task_id)
            # v1.6.3（D7）：_TASK_LLM 同步清理（明文 key 不在进程内滞留，旧实现仅删除路径弹出）
            _TASK_LLM.pop(task_id, None)

            if not draft:
                yield await _fail(failure or "未生成报告草稿（数据源可能不可用）")
                return

            # 1) 先落库报告 + 任务 done/report_id（done 必须在落库后发送；若任务中途已被取消或删除则不复活）
            report_id = uuid.uuid4().hex
            aborted_err: Optional[str] = None
            async with session_scope() as s:
                if s is not None:
                    t = await s.get(ResearchTask, task_id)
                    if t is None and not code:
                        aborted_err = "研究任务已被删除"
                    elif t is not None and t.status == "error":
                        aborted_err = t.error or "研究任务已取消"
                    else:
                        s.add(
                            Report(
                                id=report_id,
                                task_id=task_id,
                                ticker=draft.get("ticker", code_),
                                name=draft.get("name", ""),
                                template=tpl_,
                                summary=draft.get("summary", ""),
                                payload=draft,
                                html=draft.get("html", ""),
                                as_of=draft.get("as_of")
                                or now_cst().strftime("%Y-%m-%d %H:%M"),
                            )
                        )
                        if t:
                            t.status = "done"
                            t.stage = "done"
                            if draft.get("name"):
                                t.name = str(draft["name"])[:128]
                            t.report_id = report_id
                            t.timings = dict(merged_timings)
                        await s.commit()

            if aborted_err:
                yield sse.ev_error(aborted_err)
                return

            # 2) 摘要已在 summary 节点优先推送；兼容异常/旧图路径时再补发。
            summary = draft.get("summary", "") or ""
            if not summary_streamed:
                yield sse.sse({"type": "summary", "text": summary})

            yield sse.ev_tool(f"研究任务总耗时 {int((time.perf_counter() - graph_t0) * 1000)}ms")
            # 3) done 立即发送（不被后续知识库索引/诊断写入阻塞）
            yield sse.sse({"type": "done", "report_id": report_id, "report": draft})

            # 4) 落库与 done 发送完毕后再异步补齐诊断与知识库索引（有界超时，绝不阻塞前端收口）
            try:
                await asyncio.wait_for(
                    _persist_context_diagnostics(
                        "research",
                        task_id,
                        context_diagnostics(limit=20, kind="research", obj_id=task_id),
                    ),
                    timeout=5.0,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("研究上下文诊断持久化跳过：%s", e)
            try:
                await asyncio.wait_for(
                    kb_service.on_report_saved(report_id, draft, code=code_, template=tpl_),
                    timeout=10.0,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("报告入知识库失败（不影响研究结果）：%s", e)
        except Exception as e:  # noqa: BLE001
            logger.exception("research_stream 失败")
            # 若撰写中途已生成草稿而后置步骤抛错，不再把已落库 done 的任务打回 error
            yield await _fail(sanitize_err_text(e))

    async def connection():
        runtime = _start_research_runtime(task_id, pipeline)
        queue: asyncio.Queue = asyncio.Queue()
        # 订阅与历史快照必须在同一个同步片段里完成（中间不能有 yield/await）：
        # 否则首帧发送挂起期间 pump 新记录的帧会同时出现在快照与队列里，被前端收两遍。
        runtime.subscribers.add(queue)
        snapshot = list(runtime.history)
        was_terminal = runtime.terminal
        try:
            # 新订阅者先回放本进程内已经产生的事件，切页回来不会只剩一个转圈。
            # v1.7.1（A06）：以 replay 帧开头 —— 前端清空本轮展示后按压缩历史重建（幂等）。
            yield _REPLAY_FRAME
            for frame in snapshot:
                yield frame
            if was_terminal:
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
    """报告库列表（从 SQLite 读取真实报告，重启不丢；延迟加载大字段 html/payload 避免内存膨胀）。"""
    async with session_scope() as s:
        if s is None:
            raise HTTPException(503, "local database unavailable")
        stmt = (
            select(Report)
            .options(defer(Report.html), defer(Report.payload))
            .order_by(Report.created_at.desc())
            .limit(max(1, min(int(limit or 100), 500)))
        )
        rows = list((await s.execute(stmt)).scalars().all())
        return {"reports": [{
            "id": r.id,
            "task_id": r.task_id,
            "ticker": r.ticker,
            "name": r.name or "",
            "template": r.template,
            "summary": (r.summary or "")[:300],
            "as_of": r.as_of or "",
            "created_at": _iso_utc(r.created_at),
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
            "llm_degraded": bool(payload.get("llm_degraded", False)),
            "llm_fallback_reason": payload.get("llm_fallback_reason", ""),
        }


# ────────────────────────────────────────────────────────────
# 报告下载（真实 docx/pdf/txt/md 文件）
# ────────────────────────────────────────────────────────────
@router.get("/reports/{report_id}/download")
async def download_report(report_id: str, format: str = "pdf") -> Response:
    """报告真实文件下载：format=docx|pdf|txt|md。后台线程渲染不阻塞事件循环；生成失败返 500。"""
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
            txt = await asyncio.to_thread(export.render_txt, doc)
            data, mime = txt.encode("utf-8"), "text/plain; charset=utf-8"
        elif fmt == "md":
            md = await asyncio.to_thread(export.render_md, doc)
            data, mime = md.encode("utf-8"), "text/markdown; charset=utf-8"
        elif fmt == "docx":
            data = await asyncio.to_thread(export.render_docx, doc)
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        else:
            data = await asyncio.to_thread(export.render_pdf, doc)
            mime = "application/pdf"
    except Exception as e:  # noqa: BLE001
        logger.exception("报告导出失败 format=%s", fmt)
        raise HTTPException(500, f"报告导出失败：{sanitize_err_text(e)}")
    if not data:
        raise HTTPException(500, "报告导出生成空文件")
    from urllib.parse import quote
    filename = export.safe_filename(doc, fmt)
    # HTTP 头为 latin-1：中文文件名用 RFC 5987 的 filename*=UTF-8'' 编码，并给严格无空白/控制符的 ASCII 兜底
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]", "_", str(doc.get("ticker") or "report")).strip("._") or "report"
    cd = f"attachment; filename=\"{ascii_fallback}.{fmt}\"; filename*=UTF-8''{quote(filename, safe='')}"
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": cd, "Cache-Control": "no-store"},
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
            # v1.6.0（审计 B4）：连带清理上下文诊断行（此前删报告后永久残留，生产库实证 20+ 条）
            await s.execute(delete(LocalSetting).where(LocalSetting.key == f"context-diag:research:{task_id}"))
        await s.commit()
    # 会话已提交、写锁已释放，再做副作用清理（这些即便失败也不该让删除回滚）
    if task_id:
        # 极少见：报告已存在通常意味任务已结束；防御性停掉仍在跑的运行时
        runtime = _RESEARCH_RUNS.pop(task_id, None)
        rt = getattr(runtime, "task", None) if runtime is not None else None
        if rt is not None and not rt.done():
            rt.cancel()
        _TASK_LLM.pop(task_id, None)
        research_clear_task(task_id)
    # v1.8.0：知识库同步删除（报告删了，RAG 不得再检索到它）
    await kb_service.on_report_deleted(rid)
    if task_id:
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
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None


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

    # v1.6.3（C5）：图表速读同样过缓存防腐覆盖（同供应商 DB 当前 key 优先）；未传时回退当前激活配置
    chart_llm_cfg = await _overlay_current_key(req.llm) if req.llm else await _active_llm_cfg()
    target_host = urlparse((chart_llm_cfg or {}).get("base_url") or settings.llm_base_url).netloc
    llm = build_chart_llm(override=chart_llm_cfg)
    used_model = resolved_chart_model(chart_llm_cfg)
    logger.info("chart_analyze target host=%s model=%s", target_host, used_model)
    if llm is None:
        return {
            "text": "(未配置 LLM，跳过 AI 速读)",
            "provenance": {**base_prov, "host": target_host, "model": used_model, "error": "llm_unconfigured"},
        }

    p = req.point
    rel = ((p.price - req.prevClose) / req.prevClose * 100.0) if req.prevClose else 0.0
    fact_json: dict[str, Any] = {
        "code": req.code,
        "kind": req.kind,
        "time": p.time or p.date or "",
        "price": p.price,
        "avg": p.avg,
        "volume": p.volume,
        "prev_close": req.prevClose,
        "rel_pct": round(rel, 2),
    }
    if req.kind == "kline":
        for kline_field in ("open", "high", "low", "close", "ma5", "ma10", "ma20"):
            val = getattr(p, kline_field, None)
            if val is not None:
                fact_json[kline_field] = val
    try:
        resp = await llm.ainvoke(
            build_chart_context(
                system_prompt=CHART_SYSTEM,
                facts_json=json.dumps(fact_json, ensure_ascii=False),
            )
        )
        text = resp.content if hasattr(resp, "content") else str(resp)
        # content-block 列表形态（部分兼容端点）直接 .strip() 会 AttributeError → 被下方
        # except 吞成「速读失败」；推理端点内联的 <think> 也不能当速读结论展示（v1.7.1）
        if isinstance(text, list):
            text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in text)
        text = strip_think(str(text or ""))
        return {
            "text": text.strip() or "(速读为空)",
            "provenance": {**base_prov, "host": target_host, "model": used_model},
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("chart_analyze LLM 失败：%s", e)
        safe_err = sanitize_err_text(e)
        return {
            "text": f"(AI 速读失败：{safe_err}；相对昨收 {rel:+.2f}%)",
            "provenance": {**base_prov, "host": target_host, "model": used_model, "error": safe_err},
        }


# ────────────────────────────────────────────────────────────
# models（真·拉厂商 /v1/models，供前端"拉取最新模型"按钮）
# ────────────────────────────────────────────────────────────
class ModelsRequest(BaseModel):
    base_url: str
    api_key: str = ""
    provider_id: Optional[str] = None


def _validate_models_endpoint_url(raw_url: str, *, self_port: Optional[int] = None) -> str:
    """校验 /api/models 目标地址：仅允许 http/https，禁止 file://、内嵌凭据、云元数据网段及自身服务回环。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    u = (raw_url or "").strip()
    if not u:
        raise ValueError("Base URL 不能为空")
    parsed = urlparse(u)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError("Base URL 仅支持 http:// 或 https:// 协议")
    if not parsed.hostname:
        raise ValueError("Base URL 缺少有效主机名")
    if parsed.username or parsed.password:
        raise ValueError("Base URL 不允许内嵌用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("Base URL 不允许包含查询参数或 # 锚点")
    host = parsed.hostname
    port = parsed.port or (443 if scheme == "https" else 80)

    def _check_ip(ip_str: str) -> None:
        ip = ipaddress.ip_address(ip_str)
        # 严禁云元数据链路本地地址（169.254.169.254 / fe80::）、未指定地址、组播地址
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ValueError("目标地址属于受限网段，已拒绝访问")
        if ip.is_loopback and self_port and port == self_port:
            raise ValueError("禁止将本软件自身服务端口设为模型端点")

    try:
        _check_ip(host)
    except ValueError as ve:
        if "受限网段" in str(ve) or "自身服务端口" in str(ve):
            raise
        # 非字面 IP（域名）：解析 DNS 校验目标地址，防 DNS rebinding 到 169.254.169.254
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"无法解析主机名：{host}") from exc
        for info in infos:
            sockaddr = info[4]
            if sockaddr and sockaddr[0]:
                _check_ip(sockaddr[0])

    return u.rstrip("/") + "/models"


@router.post("/models")
async def list_models(req: ModelsRequest, request: Request = None) -> dict:
    """代理调用厂商 OpenAI 兼容 /models，返回当前可用模型 id 列表（带协议/SSRF 防护与脱敏密钥还原）。"""
    import json as _json
    import urllib.request as _u
    from ..tools import make_provenance

    from urllib.parse import urljoin, urlparse

    try:
        req_port = request.url.port if (request is not None and getattr(request, "url", None)) else None
        self_port = req_port or getattr(settings, "port", 9527)
        target_url = await asyncio.to_thread(_validate_models_endpoint_url, req.base_url, self_port=self_port)
    except Exception as e:  # noqa: BLE001
        return {
            "models": [],
            "provenance": make_provenance(source="vendor", endpoint="/models", error=sanitize_err_text(e)),
        }

    api_key = (req.api_key or "").strip()
    if not api_key or is_masked_secret(api_key):
        api_key = await _current_key_for_provider_or_base(req.provider_id or "", req.base_url) or ""

    class _SafeRedirectHandler(_u.HTTPRedirectHandler):
        def redirect_request(self, req_obj, fp, code, msg, headers, newurl):
            resolved_new = urljoin(req_obj.full_url, newurl)
            orig_p = urlparse(req_obj.full_url)
            dest_p = urlparse(resolved_new)
            if (dest_p.scheme or "").lower() != (orig_p.scheme or "").lower() or (dest_p.netloc or "").lower() != (orig_p.netloc or "").lower():
                raise ValueError("拒绝跨域或协议降级重定向，防止泄露 API Key")
            if dest_p.fragment:
                raise ValueError("重定向地址非法")
            _validate_models_endpoint_url(f"{dest_p.scheme}://{dest_p.netloc}", self_port=self_port)
            return super().redirect_request(req_obj, fp, code, msg, headers, resolved_new)

    def _fetch():
        headers = {"Accept": "application/json", "User-Agent": "FinResearch/1.8"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        opener = _u.build_opener(_SafeRedirectHandler())
        r = _u.Request(target_url, headers=headers)
        with opener.open(r, timeout=15) as resp:
            raw = resp.read(2 * 1024 * 1024)
            return _json.loads(raw.decode("utf-8", errors="replace"))

    try:
        data = await asyncio.to_thread(_fetch)
        items = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []
        ids = [
            {"id": str(m.get("id", "")), "label": str(m.get("id", ""))}
            for m in items
            if isinstance(m, dict) and m.get("id")
        ]
        return {"models": ids, "provenance": make_provenance(source="vendor", endpoint="/models")}
    except Exception as e:  # noqa: BLE001
        logger.warning("list_models 失败：%s", sanitize_err_text(e))
        return {
            "models": [],
            "provenance": make_provenance(source="vendor", endpoint="/models", error=sanitize_err_text(e)),
        }
