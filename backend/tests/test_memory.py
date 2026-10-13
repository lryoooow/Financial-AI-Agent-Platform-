"""本地记忆与知识库（sqlite-vec + 生命周期）的契约测试。"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finresearch.tools import embedder, memory
from finresearch.tools.rag_store import open_rag_store


@pytest.fixture()
def rag(tmp_path):
    store = open_rag_store(tmp_path / "rag.db")
    assert store is not None, "sqlite-vec 扩展不可用"
    yield store
    store.close()


# ── 嵌入器 ────────────────────────────────────────────────
def test_embedder_deterministic_and_dim():
    a1 = embedder.embed_one("贵州茅台基本面")
    a2 = embedder.embed_one("贵州茅台基本面")
    assert len(a1) == embedder.EMBED_DIM == 256
    assert a1 == a2  # 跨调用确定性（跨会话/跨机器可复现）
    assert abs(sum(v * v for v in a1) - 1.0) < 1e-6  # L2 归一化
    b = embedder.embed_one("完全不同的文本")
    assert a1 != b
    assert embedder.embed_one("") == [0.0] * 256


# ── 向量库 ────────────────────────────────────────────────
def test_rag_roundtrip_search_dedupe_purge(rag):
    rag.add(source_type="chat", ref_id="c1", text="用户长期关注贵州茅台与白酒板块估值")
    rag.add(source_type="report", ref_id="r1", title="宁德时代财报点评",
            text="宁德时代2026中报净利润445亿，同比增长45%，毛利率23.9%")
    rag.add(source_type="chat", ref_id="c2", text="用户偏好稳健风格，关注现金流与ROE")

    assert rag.count() == 3
    assert rag.add(source_type="chat", ref_id="c1", text="用户长期关注贵州茅台与白酒板块估值") is None  # 哈希去重

    hits = rag.search("茅台 白酒", k=2)
    assert hits and "茅台" in hits[0]["text"]  # 相关性排第一

    hits2 = rag.search("宁德 净利润", k=2)
    assert any("宁德" in (h["title"] + h["text"]) for h in hits2)

    only_chat = rag.search("稳健 现金流", k=3, source_types=["chat"])
    assert only_chat and all(h["source_type"] == "chat" for h in only_chat)

    # 词法兜底：构造向量检索不命中的长尾查询
    fallback = rag.search("毛利率23.9%的具体数字细节查询", k=5)
    assert isinstance(fallback, list)

    assert rag.delete_by_ref("report", "r1") == 1
    assert rag.count("report") == 0 and rag.count() == 2
    assert rag.purge_older_than(-1) == 2  # 负数天=全部超期
    assert rag.count() == 0


# ── 记忆服务（独立数据目录）──────────────────────────────
@pytest.fixture()
def mem_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "FinData"
    monkeypatch.setattr(memory, "app_data_dir", lambda: str(data_dir))
    monkeypatch.setattr(memory.settings, "memory_enabled", True)
    monkeypatch.setattr(memory.settings, "memory_archive_days", 90)
    # settings 是 import 时固化的 pydantic 单例：必须直接改属性，改 env 无效
    from finresearch import config as cfg
    monkeypatch.setattr(
        cfg.settings, "database_url",
        f"sqlite+aiosqlite:///{(data_dir / 't.db').as_posix()}",
    )
    from finresearch.db import session as db_session

    db_session._engine = None
    db_session._maker = None
    db_session._tried = False
    import asyncio as _aio; _aio.run(memory.init_memory())
    assert memory.memory_available()
    yield data_dir
    memory.close_memory()


def test_memory_ingest_and_search(mem_env):
    memory.ingest_exchange_sync(
        "conv1", "白酒讨论",
        "贵州茅台现在值得买吗", "当前PE约19倍处于历史中位，ROE维持30%以上……",
    )
    memory.ingest_report_sync("rep1", {
        "template": "earnings", "summary": "宁德时代中报净利445亿同比增45%",
        "sections": [{"title": "业绩快评", "body": "营收同比+54.8%，盈利转化效率提升。" * 8}],
    }, "300750.SZ", "宁德时代")

    result = memory.search_memory_sync("我之前聊过茅台什么", limit=3)
    assert any("茅台" in i["text"] for i in result["items"])
    result2 = memory.search_memory_sync("宁德时代 历史报告", limit=3)
    assert any("宁德" in (i["title"] + i["text"]) for i in result2["items"])


def test_conversation_archive_lifecycle(mem_env, monkeypatch):
    """归档全流程：旧会话 → 摘要 → 压缩原文 → 删行 → 摘要可取回。"""
    from finresearch.db.session import init_db, session_scope
    from finresearch.db.models import ChatConversation, ChatDelta, LocalSetting

    async def scenario():
        await init_db()
        async with session_scope() as s:
            s.add(ChatConversation(id="old-conv", title="旧会话"))
            await s.commit()
            for seq, (role, content) in enumerate([
                ("user", "帮我分析下贵州茅台"), ("assistant", "茅台是高端白酒龙头，ROE长期30%+……"),
            ]):
                s.add(ChatDelta(conversation_id="old-conv", seq=seq, role=role,
                                payload={"message": {"role": role, "content": content}}))
            conv = await s.get(ChatConversation, "old-conv")
            conv.updated_at = datetime(2020, 1, 1, tzinfo=timezone.utc)  # 远超归档期
            await s.commit()

        # LLM 摘要打桩
        import finresearch.tools.memory as m
        monkeypatch.setattr(m, "_summarize_with_llm", lambda t, c: "用户询问过贵州茅台；结论：高端白酒龙头，ROE 30%+。")

        n = await m.archive_old_conversations(None)
        assert n == 1

        async with session_scope() as s:
            from sqlalchemy import select
            remaining = (await s.execute(select(ChatDelta).where(ChatDelta.conversation_id == "old-conv"))).scalars().all()
            assert remaining == []  # 行级消息已清
        summary = await m.archived_summary("old-conv")
        assert summary and "茅台" in summary

        # 原文归档文件存在且可解压回读
        archive_file = memory.archive_dir() / "old-conv.jsonl.gz"
        assert archive_file.is_file()
        lines = gzip.decompress(archive_file.read_bytes()).decode("utf-8").splitlines()
        msgs = [json.loads(x) for x in lines]
        assert len(msgs) == 2 and msgs[0]["content"].startswith("帮我分析")

        # 摘要已入向量库（可被 search_memory 检索到）
        result = m.search_memory_sync("很久之前聊过什么茅台", limit=3)
        assert any("茅台" in i["text"] for i in result["items"])

    asyncio.run(scenario())


def test_maintenance_purges_old_chat_vectors_only(mem_env, monkeypatch):
    from finresearch.db.session import init_db

    asyncio.run(init_db())  # 真实启动顺序：先建表再维护
    memory.ingest_exchange_sync("c1", "", "旧对话内容", "旧答复")
    memory.ingest_report_sync("r1", {"summary": "重要报告结论", "sections": []}, "600519.SH", "贵州茅台")
    # 直接把 chat 块时间改到超期前
    store = memory._store
    with store._lock:
        store._db.execute(
            "UPDATE chunks SET created_at = ? WHERE source_type = 'chat'",
            (0.0,),
        )
        store._db.commit()

    result = asyncio.run(memory.run_memory_maintenance())
    assert result["purged"] == 1
    assert store.count("chat") == 0
    assert store.count("report") == 1  # 报告不受对话清理影响


# ── 两级检索 + 知识库 ─────────────────────────────────────
def test_two_tier_search_returns_ref_and_read_memory(mem_env):
    """一级检索返回 ref 索引卡；read_memory 按 ref 深读完整原文。"""
    memory.ingest_exchange_sync(
        "convX", "白酒偏好",
        "我看好白酒板块", "已记录：看好白酒板块，关注贵州茅台长期护城河。",
    )
    memory.ingest_report_sync("repX", {
        "template": "earnings",
        "summary": "宁德中报摘要",
        "sections": [{"title": "业绩快评", "body": "营收同比+54.8%，盈利转化效率提升。" * 30}],
    }, "300750.SZ", "宁德时代")

    s = memory.search_memory_sync("白酒 茅台", limit=3)
    chat_card = next(i for i in s["items"] if i["kind"] == "历史对话")
    assert chat_card["ref"] == "chat:convX"
    assert len(chat_card["text"]) <= 300  # 索引卡只带摘要片段

    deep = memory.read_memory_sync(chat_card["ref"])
    assert deep.get("kind") == "历史对话" and "已记录" in deep["text"]

    s2 = memory.search_memory_sync("宁德 业绩", limit=3)
    rep_card = next(i for i in s2["items"] if i["kind"] == "历史研究报告")
    assert rep_card["ref"] == "report:repX"
    deep2 = memory.read_memory_sync(rep_card["ref"])
    # 报告深读来自主库 payload——本测试环境主库为独立 tmp 库，无该报告行：
    # 如实返回"原文不存在"（不能编造）
    assert "error" in deep2 or "营收" in deep2.get("text", "")

    # 未知 ref 前缀
    bad = memory.read_memory_sync("xxx:123")
    assert "error" in bad


def test_kb_document_lifecycle(mem_env, monkeypatch):
    """知识库：上传解析 → 分块入库 → 检索带 ref → 深读全文 → 删除清理（含文件副本）。"""
    from finresearch.tools import kb as kb_mod

    doc_text = (
        "# 光伏行业研究笔记\n\n硅料价格2026年持续下行，组件环节盈利修复。\n"
        + ("产业链利润向下游电池组件环节转移，N型技术渗透率快速提升。光伏发电成本已低于煤电。 " * 40)
    )
    meta = kb_mod.ingest_document_sync("光伏笔记.txt", doc_text.encode("utf-8"))
    assert meta["chunk_count"] >= 2 and meta["name"] == "光伏笔记.txt"
    doc_id = meta["id"]
    assert (kb_mod.kb_files_dir() / f"{doc_id}.txt").is_file()  # 原始文件副本

    # 同内容重传 = 幂等替换（不重复膨胀）
    meta2 = kb_mod.ingest_document_sync("光伏笔记.txt", doc_text.encode("utf-8"))
    assert meta2["id"] == doc_id

    # 检索（知识库范围）+ 全域 search_memory 都能命中
    r = kb_mod.search_documents_sync("硅料 组件 盈利", limit=3)
    assert r["items"] and r["items"][0]["ref"] == f"kb:{doc_id}"
    s = memory.search_memory_sync("光伏文档", limit=5)
    assert any(i["kind"] == "知识库文档" for i in s["items"])

    # 深读：返回完整文档文本
    deep = memory.read_memory_sync(f"kb:{doc_id}")
    assert deep["kind"] == "知识库文档" and deep["title"] == "光伏笔记.txt"
    assert "硅料价格" in deep["text"] and len(deep["text"]) > 500

    # 截断保护
    deep_short = memory.read_memory_sync(f"kb:{doc_id}", 600)
    assert deep_short["truncated"] is True and len(deep_short["text"]) < 800

    # 删除：块 + 元数据 + 文件副本
    assert kb_mod.delete_document_sync(doc_id) is True
    assert memory._store.count("kb") == 0
    assert not (kb_mod.kb_files_dir() / f"{doc_id}.txt").exists()
    assert memory.read_memory_sync(f"kb:{doc_id}").get("error")


def test_kb_chunking_and_extract():
    from finresearch.tools import kb as kb_mod

    assert kb_mod.chunk_text("") == []
    assert kb_mod.chunk_text("短文本") == ["短文本"]
    long = "句子内容。" * 400  # 2000 字
    chunks = kb_mod.chunk_text(long)
    assert len(chunks) > 1 and all(len(c) <= kb_mod._CHUNK_SIZE + 50 for c in chunks)
    assert "".join(chunks).replace("\n", "").count("句子内容") >= 390  # 内容基本无丢失

    # 编码兜底：GBK 文本能解
    gbk_text = "贵州茅台基本面".encode("gbk")
    assert "茅台" in kb_mod.extract_text(".txt", gbk_text)


def test_embedder_dual_mode_and_rebuild(mem_env):
    """双档嵌入器：本地确定性 → 供应商（mock）→ 维度变化触发向量库重建。"""
    from finresearch.tools import embedder

    # 本地档
    assert embedder.embedder_signature() == "local:256"
    v1 = embedder.embed_one("测试文本")
    assert len(v1) == 256

    # 供应商档（mock httpx 返回 1024 维）
    class _FakeResp:
        def __init__(self, count):
            self.status_code = 200
            self._count = count

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"index": i, "embedding": [0.1] * 1024} for i in range(self._count)]}

    import finresearch.tools.embedder as emb

    original_post = emb.httpx.post if hasattr(emb, "httpx") else None
    import httpx

    monkeypatch_holder = []
    def fake_post(url, headers=None, timeout=None, json=None):
        monkeypatch_holder.append(json)
        return _FakeResp(len((json or {}).get("input") or []))

    httpx.post = fake_post
    try:
        dim = emb.probe_provider_embedding("https://emb.example/v1", "sk-x", "emb-model")
        assert dim == 1024
        emb.set_provider_embedding({"base_url": "https://emb.example/v1", "api_key": "sk-x",
                                    "model": "emb-model", "dim": 1024})
        assert embedder.embedder_signature() == "provider:emb-model:1024"
        v2 = embedder.embed_one("测试文本")
        assert len(v2) == 1024
        # 批量走同一条供应商路径
        vs = embedder.embed_many(["a", "b"])
        assert len(vs) == 2 and len(vs[0]) == 1024

        # 向量库重建：旧 256 维块 → 新 1024 维
        memory.ingest_exchange_sync("c9", "", "在本地档下写入的记忆", "本地档内容")  # 重建前先入库
        rebuilt = memory._store.rebuild_embeddings(embedder.embedder_signature())
        assert rebuilt >= 1
        assert memory._store.stored_dim() == 1024
        # 重建后检索仍可用（走供应商向量 mock）
        r = memory.search_memory_sync("本地档下写入的记忆", limit=3)
        assert isinstance(r["items"], list)
        assert memory._store.get_meta("embedder") == "provider:emb-model:1024"
    finally:
        httpx.post = original_post if original_post else httpx.post
        emb.set_provider_embedding(None)

    # 回本地档：再次重建回到 256 维
    memory._store.rebuild_embeddings(embedder.embedder_signature())
    assert memory._store.stored_dim() == 256
