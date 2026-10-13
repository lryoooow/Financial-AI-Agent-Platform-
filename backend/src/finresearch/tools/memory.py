"""用户记忆与知识库服务（本地 RAG + 会话生命周期）。

数据落点（%APPDATA%/FinResearch/）：
- rag.db                 向量库（sqlite-vec）：chat 片段 / report 块 / summary 摘要
- archive/conversations/{id}.jsonl.gz   超期会话的原文压缩归档（本地不丢数据）
- 主库 LocalSetting      conv-archive:{id} = {"summary", "archived_at", "message_count"}

生命周期：
- 对话完成 → ingest_exchange（问+答片段入向量库，哈希去重）
- 报告完成 → ingest_report（摘要+各段落分块入库，报告永久保留）
- 会话闲置超过 memory_archive_days → LLM 生成摘要（存 LocalSetting + 向量库）→
  原文写归档文件 → 删除 chat_delta 行。之后打开该会话/继续对话时以摘要入上下文，
  "知道很久之前聊了什么"而不占活跃存储。
- 对话片段向量超过 rag_chat_purge_days → 清理（摘要与报告块不受影响）。

线程模型：向量库同步操作一律经 asyncio.to_thread（调用方包）；本模块提供同步内核。
LLM 未配置时归档与摘要跳过（下次维护再试），摄入与检索不受影响。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import app_data_dir, settings
from .embedder import (
    embedder_signature,
    probe_provider_embedding,
    provider_config,
    provider_embedding_active,
    set_provider_embedding,
)
from .rag_store import RagStore, open_rag_store

logger = logging.getLogger(__name__)

_store: Optional[RagStore] = None

SOURCE_CHAT = "chat"
SOURCE_REPORT = "report"
SOURCE_SUMMARY = "summary"
SOURCE_KB = "kb"

# 检索结果的 ref 引用号前缀（模型把 ref 原样传给 read_memory 深读原文）
REF_CHAT = "chat:"
REF_REPORT = "report:"
REF_SUMMARY = "summary:"
REF_KB = "kb:"

_EXCHANGE_MAX = 1200      # 单次问答记忆块的字符上限
_SECTION_CHUNK = 900      # 报告段落分块上限


def rag_db_path() -> str:
    return str(Path(app_data_dir()) / "rag.db")


def archive_dir() -> Path:
    return Path(app_data_dir()) / "archive" / "conversations"


async def init_memory() -> bool:
    """打开/创建向量库（进程内单例）。失败返回 False，记忆功能整体优雅降级。"""
    global _store
    if _store is not None:
        return True
    if not getattr(settings, "memory_enabled", True):
        return False
    _store = await asyncio.to_thread(open_rag_store, rag_db_path())
    if _store is None:
        logger.warning("向量库不可用（sqlite-vec 加载失败），记忆/知识库功能本次停用")
        return False
    await _load_embedding_config_async()
    await asyncio.to_thread(_align_embedder_signature)
    return True


EMBEDDING_CONFIG_KEY = "embedding-config"


async def _load_embedding_config_async() -> None:
    _load_embedding_config()


def _load_embedding_config() -> None:
    """从主库读嵌入配置并激活（api_key 加密存储）。启动时调用。"""
    try:
        import asyncio

        from ..db.models import LocalSetting
        from ..db.session import session_scope
        from .secrets import decrypt_config

        async def _get():
            async with session_scope() as s:
                if s is None:
                    return None
                return await s.get(LocalSetting, EMBEDDING_CONFIG_KEY)

        row = asyncio.run(_get())
        if row is None or not (row.payload or {}).get("model"):
            return
        cfg = decrypt_config(row.payload)
        if cfg and cfg.get("api_key") and cfg.get("model"):
            set_provider_embedding(cfg)
            logger.info("供应商嵌入已激活：%s", cfg.get("model"))
    except Exception as e:  # noqa: BLE001
        logger.debug("嵌入配置加载跳过：%s", e)


def _align_embedder_signature() -> None:
    """启动时对齐：库内签名与当前嵌入器不一致（配置在别处被改过）→ 后台重建。"""
    if _store is None:
        return
    current = embedder_signature()
    stored = _store.get_meta("embedder", "")
    if stored and stored != current:
        logger.info("嵌入器签名变化（%s → %s），后台重建向量库", stored, current)

        def _rebuild():
            try:
                _store.rebuild_embeddings(current)
            except Exception as e:  # noqa: BLE001
                logger.warning("向量库后台重建失败（将回退签名）：%s", e)
                _store.set_meta("embedder", stored)

        import threading

        threading.Thread(target=_rebuild, daemon=True, name="rag-rebuild").start()
    elif not stored:
        _store.set_meta("embedder", current)


async def get_embedding_config() -> dict:
    """当前嵌入配置（脱敏）+ 向量库状态。"""
    if _store is None:
        return {"mode": "disabled", "reason": "向量库不可用"}
    if not provider_embedding_active():
        return {
            "mode": "local",
            "note": "本地哈希嵌入（离线可用，零成本）。配置供应商向量模型可获得更强的语义检索。",
            "signature": embedder_signature(),
            "chunks": _store.count(),
        }
    cfg = provider_config() or {}
    return {
        "mode": "provider",
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "dim": cfg.get("dim"),
        "signature": embedder_signature(),
        "chunks": _store.count(),
        "note": "供应商语义嵌入已激活。",
    }


async def save_embedding_config(base_url: str, api_key: str, model: str) -> dict:
    """保存并激活供应商嵌入：探测连通性与维度 → 落库（key 加密）→ 返回。

    重建向量库放后台（曾经同步做，有存量数据时轻松超 30s，
    浏览器 fetch 超时报 "Failed to fetch"）。
    """
    if _store is None:
        raise RuntimeError("向量库不可用")
    base_url = (base_url or "").strip()
    api_key = (api_key or "").strip()
    model = (model or "").strip()
    if not (base_url and api_key and model):
        raise ValueError("base_url / api_key / model 均不能为空")
    dim = await asyncio.to_thread(probe_provider_embedding, base_url, api_key, model)
    from ..db.models import LocalSetting
    from ..db.session import session_scope
    from .secrets import encrypt_config

    async with session_scope() as s:
        if s is None:
            raise RuntimeError("主库不可用")
        row = await s.get(LocalSetting, EMBEDDING_CONFIG_KEY)
        if row is None:
            row = LocalSetting(key=EMBEDDING_CONFIG_KEY)
            s.add(row)
        row.payload = encrypt_config({"base_url": base_url, "api_key": api_key, "model": model, "dim": dim})
        await s.commit()
    set_provider_embedding({"base_url": base_url, "api_key": api_key, "model": model, "dim": dim})
    logger.info("供应商嵌入已激活：%s（%d 维），后台重建向量库", model, dim)

    # 重建放后台：新 chunks 待下一轮摄入自动用新嵌入器；存量的在后台补齐
    asyncio.get_event_loop().run_in_executor(None, _background_rebuild)
    return {"model": model, "dim": dim, "rebuilt": -1}  # -1 = 后台进行中


def _background_rebuild() -> None:
    """后台重建向量库（线程池执行，不阻塞事件循环/HTTP 请求）。"""
    try:
        n = _store.rebuild_embeddings(embedder_signature())
        logger.info("向量库后台重建完成：%d 条", n)
    except Exception as e:  # noqa: BLE001
        logger.warning("向量库后台重建失败：%s", e)


async def clear_embedding_config() -> dict:
    """清除供应商配置，回到本地哈希档并重建。"""
    if _store is None:
        raise RuntimeError("向量库不可用")
    from ..db.models import LocalSetting
    from ..db.session import session_scope

    async with session_scope() as s:
        if s is not None:
            row = await s.get(LocalSetting, EMBEDDING_CONFIG_KEY)
            if row is not None:
                await s.delete(row)
                await s.commit()
    set_provider_embedding(None)
    asyncio.get_event_loop().run_in_executor(None, _background_rebuild)
    return {"mode": "local", "rebuilt": -1}  # -1 = 后台进行中


def close_memory() -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None


def memory_available() -> bool:
    return _store is not None


def _ts_label(ts: Optional[float]) -> str:
    if not ts:
        return "未知时间"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")


# ────────────────────────────────────────────────────────────
# 摄入
# ────────────────────────────────────────────────────────────
def ingest_exchange_sync(conv_id: str, title: str, user_text: str, reply_text: str) -> None:
    """一轮问答 → 一条记忆块（问截 200 / 答截 1000，哈希去重天然幂等）。"""
    if _store is None:
        return
    q = (user_text or "").strip()[:200]
    a = (reply_text or "").strip()[:1000]
    if not q and not a:
        return
    text = f"问：{q}\n答：{a}"[:_EXCHANGE_MAX]
    _store.add(
        source_type=SOURCE_CHAT, ref_id=conv_id, title=(title or "")[:40], text=text,
        meta={"kind": "exchange"},
    )


def ingest_report_sync(report_id: str, doc: dict, ticker: str, name: str) -> None:
    """一份研究报告 → 摘要 + 各段落分块（报告是用户的成果资产，永久保留）。"""
    if _store is None:
        return
    base_meta = {"ticker": ticker, "template": doc.get("template", "")}
    # 同报告重生成 → 整体覆盖后重写（ref_id 统一为 report_id，深读走主库原文）
    _store.delete_by_ref(SOURCE_REPORT, report_id)
    summary = (doc.get("summary") or "").strip()
    if summary:
        _store.add(
            source_type=SOURCE_REPORT, ref_id=report_id, title=f"{name}·研究摘要",
            text=summary[:_SECTION_CHUNK], meta=base_meta,
        )
    for sec in doc.get("sections") or []:
        body = (sec.get("body") or "").strip()
        title = (sec.get("title") or "").strip()
        if not body:
            continue
        for i in range(0, len(body), _SECTION_CHUNK):
            _store.add(
                source_type=SOURCE_REPORT, ref_id=report_id, title=f"{name}·{title}",
                text=body[i : i + _SECTION_CHUNK],
                meta={**base_meta, "section": title},
            )
    # 风险提示也入库（对"XX 有什么风险"类检索更友好）
    risks = doc.get("risks") or []
    if risks:
        risk_text = "\n".join(f"· {r}" for r in risks if r and str(r).strip())
        if risk_text:
            _store.add(
                source_type=SOURCE_REPORT, ref_id=report_id, title=f"{name}·风险提示",
                text=risk_text[:_SECTION_CHUNK], meta={**base_meta, "section": "风险"},
            )


# ────────────────────────────────────────────────────────────
# 检索（供 agent 工具调用）
# ────────────────────────────────────────────────────────────
def purge_report_chunks(report_id: str) -> int:
    """删除报告时清理对应的向量块（防死块：深读时"原文不存在"）。"""
    if _store is None:
        return 0
    return _store.delete_by_ref(SOURCE_REPORT, report_id)


def search_memory_sync(query: str, limit: int = 5) -> dict:
    """一级检索（索引层）：返回带 ref 引用号的索引卡（每条仅 ≤300 字摘要片段）。

    模型先据此定位"有哪些相关内容"；需要完整内容时把 ref 原样传给
    read_memory（二级检索）深读原文。
    """
    if _store is None:
        return {"items": [], "provenance": {"source": "memory", "error": "记忆库未启用"}}
    limit = max(1, min(int(limit or 5), 10))
    hits = _store.search(query, k=limit + 2)  # 多取几条，过滤后截断
    items = []
    for h in hits:
        st = h["source_type"]
        label = {
            SOURCE_CHAT: "历史对话", SOURCE_REPORT: "历史研究报告",
            SOURCE_SUMMARY: "会话摘要", SOURCE_KB: "知识库文档",
        }.get(st, st)
        prefix = {SOURCE_CHAT: REF_CHAT, SOURCE_REPORT: REF_REPORT,
                  SOURCE_SUMMARY: REF_SUMMARY, SOURCE_KB: REF_KB}.get(st, "")
        items.append({
            "kind": label,
            "title": h.get("title") or "",
            "date": _ts_label(h.get("created_at")),
            "ref": f"{prefix}{h.get('ref_id') or ''}" if prefix else "",
            "text": (h.get("text") or "")[:300],
        })
    note = "本地记忆与知识库检索（向量+词法混合）"
    _pcfg = provider_config()
    if _pcfg:
        note += f"；嵌入：{_pcfg.get('model')}"
    return {
        "items": items[:limit],
        "provenance": {"source": "sqlite-vec", "note": note},
    }


# 深读的字符上限：足够模型拿到完整论证，又不至于单工具调用撑爆上下文
_READ_MAX_CHARS = 10000


def _read_report(report_id: str) -> tuple:
    """主库读报告全文（payload 的 sections + summary）。返回 (text, title)。"""
    import asyncio

    from ..db.models import Report
    from ..db.session import session_scope

    async def _load():
        async with session_scope() as s:
            if s is None:
                return None
            return await s.get(Report, report_id)

    # read_memory 由 to_thread 调用（工作线程无事件循环），独立 loop 跑协程
    rep = asyncio.run(_load())
    if rep is None:
        return "", ""
    payload = rep.payload or {}
    parts = []
    if rep.summary:
        parts.append("【摘要】" + rep.summary)
    for sec in payload.get("sections") or []:
        title = (sec.get("title") or "").strip()
        body = (sec.get("body") or "").strip()
        if body:
            parts.append(("【" + title + "】\n" + body) if title else body)
    return "\n\n".join(parts), rep.name or ""


def _read_archived_conversation(conv_id: str) -> str:
    """解压归档原文 → 可读对话转录。"""
    target = archive_dir() / (conv_id + ".jsonl.gz")
    if not target.is_file():
        return ""
    lines = gzip.decompress(target.read_bytes()).decode("utf-8", errors="replace").splitlines()
    msgs = []
    for line in lines:
        try:
            m = json.loads(line)
            msgs.append(str(m.get("role", "user")) + ": " + str(m.get("content", "")))
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(msgs)


def read_memory_sync(ref: str, max_chars: int = _READ_MAX_CHARS) -> dict:
    """二级检索（原文层）：按 search_memory 返回的 ref 深读完整内容。

    ref 形态：chat:{会话id} / report:{报告id} / summary:{会话id} / kb:{文档id}。
    字符上限保护 + 如实标注截断——绝不让单次深读撑爆上下文。
    """
    if _store is None:
        return {"error": "记忆库未启用"}
    ref = (ref or "").strip()
    max_chars = max(500, min(int(max_chars or _READ_MAX_CHARS), 20000))
    text = ""
    kind = ""
    title = ""
    try:
        if ref.startswith(REF_REPORT):
            text, title = _read_report(ref[len(REF_REPORT):])
            kind = "历史研究报告"
        elif ref.startswith(REF_KB):
            doc = _store.kb_get_doc(ref[len(REF_KB):])
            if doc:
                text, title, kind = doc["full_text"], doc["name"], "知识库文档"
        elif ref.startswith(REF_CHAT):
            cid = ref[len(REF_CHAT):]
            text = _read_archived_conversation(cid)
            if not text:
                # 未归档：用向量库中该会话的全部记忆块拼转录
                with _store._lock:
                    rows = _store._db.execute(
                        "SELECT text FROM chunks WHERE source_type=? AND ref_id=? ORDER BY created_at",
                        (SOURCE_CHAT, cid),
                    ).fetchall()
                text = "\n".join(r[0] for r in rows)
            kind = "历史对话"
        elif ref.startswith(REF_SUMMARY):
            cid = ref[len(REF_SUMMARY):]
            text = _read_archived_conversation(cid)
            kind = "会话归档原文"
            if not text:
                import asyncio

                async def _sum():
                    return await archived_summary(cid)

                text = asyncio.run(_sum()) or ""
                kind = "会话摘要"
        else:
            return {"error": "无法识别的 ref（应为 chat:/report:/summary:/kb: 前缀）：" + ref[:40]}
    except Exception as e:  # noqa: BLE001
        return {"error": "深读失败：" + str(e)}
    text = (text or "").strip()
    if not text:
        return {"error": "原文不存在或已被清理", "ref": ref}
    truncated = len(text) > max_chars
    out = text[:max_chars]
    if truncated:
        out += "\n…（内容过长已截断，可缩小检索范围后再次深读）"
    return {
        "kind": kind,
        "title": title,
        "ref": ref,
        "text": out,
        "truncated": truncated,
        "provenance": {"source": "local-db", "note": "本地深读：完整原文自主库/归档/知识库"},
    }


# ────────────────────────────────────────────────────────────
# 会话生命周期：归档 + 摘要
# ────────────────────────────────────────────────────────────
_ARCHIVE_SUMMARY_PROMPT = (
    "你是对话记忆压缩器。把下面这轮金融研究助手与用户的完整对话压缩成不超过 300 字的要点摘要："
    "保留讨论过的标的/行业、用户表达的偏好与观点、得出的关键结论与数据。只输出摘要正文。"
    "\n\n【对话记录】\n{transcript}"
)
_TRANSCRIPT_MAX = 12000


def _summarize_with_llm(transcript: str, llm_cfg: Optional[dict]) -> str:
    from ..llm import build_llm

    llm = build_llm(streaming=False, override=llm_cfg)
    if llm is None:
        return ""
    from langchain_core.messages import HumanMessage

    result = llm.invoke([HumanMessage(content=_ARCHIVE_SUMMARY_PROMPT.format(transcript=transcript[:_TRANSCRIPT_MAX]))])
    return (getattr(result, "content", "") or "").strip()


async def _load_history_for_archive(conv_id: str) -> list[dict]:
    from ..db.session import session_scope
    from ..db.models import ChatDelta

    from sqlalchemy import select

    async with session_scope() as s:
        if s is None:
            return []
        rows = (await s.execute(
            select(ChatDelta).where(ChatDelta.conversation_id == conv_id)
            .order_by(ChatDelta.seq.asc(), ChatDelta.id.asc())
        )).scalars().all()
        out = []
        for r in rows:
            msg = ((r.payload or {}).get("message") or {})
            role = msg.get("role", r.role or "user")
            content = msg.get("content", "")
            if content:
                out.append({"role": role, "content": content, "seq": r.seq})
        return out


async def archive_old_conversations(llm_cfg: Optional[dict] = None) -> int:
    """归档闲置超期会话：摘要 → 压缩原文 → 删行。返回归档条数（LLM 不可用时返回 0）。

    性能铁律：LLM 调用与磁盘 IO 必须 asyncio.to_thread——曾经直接在事件循环上
    跑阻塞 HTTP（llm.invoke），启动 +60s 的维护任务把整个后端（含静态文件）冻结。
    限量 5 个/pass 防止一次维护霸占过久。
    """
    from ..db.session import session_scope
    from ..db.models import ChatConversation, ChatDelta, LocalSetting

    from sqlalchemy import delete, select

    if _store is None:
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - int(getattr(settings, "memory_archive_days", 90)) * 86400
    archived = 0
    try:
        async with session_scope() as s:
            if s is None:
                return 0
            convs = (await s.execute(
                select(ChatConversation).order_by(ChatConversation.updated_at.asc()).limit(5)
            )).scalars().all()
            for conv in convs:
                key = f"conv-archive:{conv.id}"
                if await s.get(LocalSetting, key) is not None:
                    continue
                updated = conv.updated_at or conv.created_at
                if updated is None:
                    continue
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated.timestamp() > cutoff:
                    continue
                history = await _load_history_for_archive(conv.id)
                if not history:
                    continue
                transcript = chr(10).join(f"{m['role']}: {m['content']}" for m in history)
                summary = await asyncio.to_thread(_summarize_with_llm, transcript, llm_cfg)
                if not summary:
                    continue

                def _write_archive(cid=conv.id, hist=history):
                    d = archive_dir()
                    d.mkdir(parents=True, exist_ok=True)
                    target = d / f"{cid}.jsonl.gz"
                    payload = chr(10).join(json.dumps(m, ensure_ascii=False) for m in hist).encode("utf-8")
                    target.write_bytes(gzip.compress(payload))
                await asyncio.to_thread(_write_archive)

                row = await s.get(LocalSetting, key)
                if row is None:
                    row = LocalSetting(key=key)
                    s.add(row)
                row.payload = {
                    "summary": summary, "archived_at": time.time(),
                    "message_count": len(history),
                }
                await s.commit()
                _store.add(
                    source_type=SOURCE_SUMMARY, ref_id=conv.id, title=(conv.title or "")[:40],
                    text=f"会话摘要：{summary}", meta={"kind": "conversation_summary"},
                )
                await s.execute(delete(ChatDelta).where(ChatDelta.conversation_id == conv.id))
                await s.commit()
                archived += 1
                logger.info("会话 %s 已归档（%d 条消息，摘要 %d 字）", conv.id[:8], len(history), len(summary))
    except Exception as e:  # noqa: BLE001
        logger.debug("会话归档跳过（主库不可用或结构异常）：%s", e)
    return archived


async def archived_summary(conv_id: str) -> Optional[str]:
    """取会话的归档摘要（未归档返回 None）。"""
    from ..db.session import session_scope
    from ..db.models import LocalSetting

    async with session_scope() as s:
        if s is None:
            return None
        row = await s.get(LocalSetting, f"conv-archive:{conv_id}")
        if row is None:
            return None
        return (row.payload or {}).get("summary") or None


async def run_memory_maintenance(llm_cfg: Optional[dict] = None) -> dict:
    """周期维护：向量清理 + 会话归档。启动后延迟触发、此后每 24h 一次。"""
    purged = 0
    if _store is not None:
        purged = _store.purge_older_than(
            int(getattr(settings, "rag_chat_purge_days", 180)), [SOURCE_CHAT]
        )
    archived = await archive_old_conversations(llm_cfg)
    if purged or archived:
        logger.info("记忆维护完成：清理对话向量 %d 条，归档会话 %d 个", purged, archived)
    return {"purged": purged, "archived": archived}
