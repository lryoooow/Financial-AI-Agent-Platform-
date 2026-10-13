# -*- coding: utf-8 -*-
"""五个根因一次修复。用后即删。"""
import ast, re

# ════════ 根因1+4：memory.py — 维护任务阻塞 + asyncio.run 崩溃 ════════
p = r"E:\jingyuan\langchain\backend\src\finresearch\tools\memory.py"
src = open(p, encoding="utf-8").read()

# 1a. init_memory 改 async，_load_embedding_config 去 asyncio.run
old_init = '''def init_memory() -> bool:
    """打开/创建向量库（进程内单例）。失败返回 False，记忆功能整体优雅降级。"""
    global _store
    if _store is not None:
        return True
    if not getattr(settings, "memory_enabled", True):
        return False
    _store = open_rag_store(rag_db_path())
    if _store is None:
        logger.warning("向量库不可用（sqlite-vec 加载失败），记忆/知识库功能本次停用")
        return False
    _load_embedding_config()
    _align_embedder_signature()
    return True'''
new_init = '''async def init_memory() -> bool:
    """打开/创建向量库（进程内单例）。失败返回 False，记忆功能整体优雅降级。

    必须在事件循环内 await（曾经是同步函数，内部 asyncio.run 在 lifespan 的
    async 上下文中必抛 RuntimeError 被 try/except 吞掉——嵌入配置每次启动都
    静默加载失败，日志出现 12+ 次 coroutine never awaited 警告）。
    """
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
    _align_embedder_signature()
    return True


async def _load_embedding_config_async() -> None:
    """从主库读嵌入配置并激活（api_key 加密存储）。"""
    try:
        from ..db.models import LocalSetting
        from ..db.session import session_scope
        from .secrets import decrypt_config

        async with session_scope() as s:
            if s is None:
                return
            row = await s.get(LocalSetting, EMBEDDING_CONFIG_KEY)
        if row is None or not (row.payload or {}).get("model"):
            return
        cfg = decrypt_config(row.payload)
        if cfg and cfg.get("api_key") and cfg.get("model"):
            set_provider_embedding(cfg)
            logger.info("供应商嵌入已激活：%s", cfg.get("model"))
    except Exception as e:  # noqa: BLE001
        logger.debug("嵌入配置加载跳过：%s", e)'''
assert src.count(old_init) == 1, "init"
src = src.replace(old_init, new_init)

# 1b. 删掉旧的 _load_embedding_config（含 asyncio.run 的版本）
old_load = re.search(
    r'async def _load_embedding_config_async\(\) -> None:\n    _load_embedding_config\(\)\n\n\ndef _load_embedding_config\(\) -> None:.*?(?=\ndef _align_embedder_signature)',
    src, re.DOTALL
)
if old_load:
    src = src.replace(old_load.group(0), "")
    # 清掉可能残留的旧定义头
    src = src.replace("\n\n\nasync def _load_embedding_config_async", "\n\nasync def _load_embedding_config_async")

# 1c. run_memory_maintenance 的阻塞调用改 to_thread
old_maint = '''    purged = 0
    if _store is not None:
        purged = _store.purge_older_than(
            int(getattr(settings, "rag_chat_purge_days", 180)), [SOURCE_CHAT]
        )
    archived = await archive_old_conversations(llm_cfg)'''
new_maint = '''    purged = 0
    if _store is not None:
        # 曾经直接在事件循环上跑同步 SQLite（存量向量时慢查询冻结全站）——必须 to_thread
        purged = await asyncio.to_thread(
            _store.purge_older_than,
            int(getattr(settings, "rag_chat_purge_days", 180)), [SOURCE_CHAT]
        )
    archived = await archive_old_conversations(llm_cfg)'''
assert src.count(old_maint) == 1, "maint"
src = src.replace(old_maint, new_maint)

# 1d. archive_old_conversations 中的阻塞 LLM 调用改 to_thread + 限量 + 缩进修复
old_arch = '''    archived = 0
    try:
        async with session_scope() as s:
            if s is None:
                return 0
        convs = (await s.execute(
            select(ChatConversation).order_by(ChatConversation.updated_at.asc()).limit(200)
        )).scalars().all()'''
new_arch = '''    archived = 0
    try:
        async with session_scope() as s:
            if s is None:
                return 0
            # 限量 5 个/pass：曾经 200 个，每次全量扫 + 逐个 LLM 摘要会霸占过久
            convs = (await s.execute(
                select(ChatConversation).order_by(ChatConversation.updated_at.asc()).limit(5)
            )).scalars().all()'''
assert src.count(old_arch) == 1, "arch indent"
src = src.replace(old_arch, new_arch)

# LLM 摘要改 to_thread
old_llm = '''            transcript = "\\n".join(f"{m['role']}: {m['content']}" for m in history)
            summary = _summarize_with_llm(transcript, llm_cfg)'''
new_llm = '''            transcript = "\\n".join(f"{m['role']}: {m['content']}" for m in history)
            # 阻塞 LLM 调用必须 off 事件循环——曾经直接 invoke 冻结全站（含静态文件）
            summary = await asyncio.to_thread(_summarize_with_llm, transcript, llm_cfg)'''
assert src.count(old_llm) == 1, "llm to_thread"
src = src.replace(old_llm, new_llm)

# 归档写文件也 to_thread
old_gz = '''            archive_dir().mkdir(parents=True, exist_ok=True)
            target = archive_dir() / f"{conv_id}.jsonl.gz"
            payload = "\\n".join(json.dumps(m, ensure_ascii=False) for m in history).encode("utf-8")
            target.write_bytes(gzip.compress(payload))'''
new_gz = '''            def _write_archive():
                archive_dir().mkdir(parents=True, exist_ok=True)
                target = archive_dir() / f"{conv_id}.jsonl.gz"
                payload = "\\n".join(json.dumps(m, ensure_ascii=False) for m in history).encode("utf-8")
                target.write_bytes(gzip.compress(payload))
            await asyncio.to_thread(_write_archive)'''
assert src.count(old_gz) == 1, "gz"
src = src.replace(old_gz, new_gz)

# _background_rebuild 也确保不阻塞
old_rb = '''def _background_rebuild() -> None:
    """后台重建向量库（线程池执行，不阻塞事件循环/HTTP 请求）。"""
    try:
        n = _store.rebuild_embeddings(embedder_signature())
        logger.info("向量库后台重建完成：%d 条", n)
    except Exception as e:  # noqa: BLE001
        logger.warning("向量库后台重建失败：%s", e)'''
new_rb = '''def _background_rebuild() -> None:
    """后台重建向量库（run_in_executor 的线程中执行，不阻塞事件循环）。"""
    try:
        n = _store.rebuild_embeddings(embedder_signature())
        logger.info("向量库后台重建完成：%d 条", n)
    except Exception as e:  # noqa: BLE001
        logger.warning("向量库后台重建失败：%s", e)'''
if old_rb in src:
    src = src.replace(old_rb, new_rb)

ast.parse(src)
open(p, "w", encoding="utf-8", newline="").write(src)
print("memory.py fixed (causes 1+4)")
