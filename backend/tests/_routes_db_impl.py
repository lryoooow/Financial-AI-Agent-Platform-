"""路由层 DB 相关修复的实际验证逻辑（被 test_routes_db.py 以子进程调用）。

单独成文件并以下划线开头：不被 pytest 直接收集，只在自己的进程里跑，
用临时 DB，既不碰用户真实数据，也不污染共享 pytest 会话的 DB 引擎单例。

覆盖（审计 B1/B4/B5/B9/B10）：
1. model-configs PUT 落库加密 / GET 透明解密 / 旧明文行兼容
2. 启动清扫僵尸研究任务
3. 删除会话连带清理 LangGraph 检查点线程 + context-diag 行
4. DELETE /research/{task_id}（无报告任务）+ 有报告任务 409
5. create_research 的 30s pending 复用窗口（时区修复后东八区生效）
6. 对话合规闸：禁用词命中即截断 + 落库带提示
7. 同会话并发守卫 409
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
_db = os.path.join(_tmp, "t.db").replace(os.sep, "/")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402
from finresearch.db.session import init_db, session_scope  # noqa: E402
from finresearch.db.models import ChatConversation, ChatDelta, LocalSetting, ResearchTask  # noqa: E402
from finresearch.api import routes  # noqa: E402
from finresearch.api.routes import (  # noqa: E402
    create_research,
    delete_chat_conversation,
    delete_research_task,
    get_local_setting,
    put_local_setting,
    send_chat_message,
    sweep_stale_research_tasks,
)
from finresearch.api.routes import ChatSendRequest, LocalSettingRequest, ResearchRequest  # noqa: E402

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


async def t_model_configs_encryption():
    await put_local_setting("model-configs", LocalSettingRequest(payload={
        "deepseek": {"baseUrl": "https://api.deepseek.com", "apiKey": "sk-plain-1234567890", "selectedModel": "m"},
    }))
    async with session_scope() as s:
        row = await s.get(LocalSetting, "model-configs")
        stored = row.payload
    raw = str(stored)
    check("sk-plain-1234567890" not in raw, "PUT 后 DB 中无明文 key")
    check("dpapi:" in raw or "enc:" in raw, "DB 中为前缀密文")
    got = await get_local_setting("model-configs")
    check(got["payload"]["deepseek"]["apiKey"] == "sk-plain-1234567890", "GET 透明解密还原明文")

    # 旧明文行（v1.5 遗留）GET 不炸且原样返回
    async with session_scope() as s:
        row = await s.get(LocalSetting, "model-configs")
        row.payload = {"deepseek": {"apiKey": "sk-legacy-plain"}}
        await s.commit()
    got = await get_local_setting("model-configs")
    check(got["payload"]["deepseek"]["apiKey"] == "sk-legacy-plain", "旧明文行 GET 原样返回（零迁移）")


async def t_zombie_sweep():
    async with session_scope() as s:
        s.add(ResearchTask(id="zombie-1", code="600519.SH", template="equity_deep", status="running", stage="market"))
        s.add(ResearchTask(id="zombie-2", code="000001.SZ", template="risk", status="pending"))
        s.add(ResearchTask(id="done-1", code="600519.SH", template="earnings", status="done"))
        await s.commit()
    n = await sweep_stale_research_tasks()
    check(n == 2, f"清扫 2 条僵尸（实际 {n}）")
    async with session_scope() as s:
        z1 = await s.get(ResearchTask, "zombie-1")
        d1 = await s.get(ResearchTask, "done-1")
        check(z1.status == "error" and "中断" in (z1.error or ""), "僵尸被标记中断")
        check(d1.status == "done", "已完成任务不受影响")


async def t_delete_chat_cleans_checkpoint_thread():
    conv_id = "conv-cp-cleanup"
    async with session_scope() as s:
        s.add(ChatConversation(id=conv_id, title="t"))
        s.add(LocalSetting(key=f"context-diag:chat:{conv_id}", payload={"items": []}))
        await s.commit()
    # 在检查点表直接造该 thread 的行（与生产库同 schema：thread_id/checkpoint_ns/checkpoint_id/...）
    import sqlite3 as _sq

    con = _sq.connect(_db)
    con.execute(
        "INSERT OR REPLACE INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
        "VALUES (?, '', 'seed-1', NULL, NULL, NULL, NULL)",
        (conv_id,),
    )
    con.commit()
    con.close()

    r = await delete_chat_conversation(conv_id)
    check(r["ok"] is True, "删除会话成功")

    con = _sq.connect(_db)
    after = con.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (conv_id,)).fetchone()[0]
    con.close()
    check(after == 0, f"会话删除后检查点线程被清（剩 {after} 行）")

    async with session_scope() as s:
        diag = await s.get(LocalSetting, f"context-diag:chat:{conv_id}")
        check(diag is None, "会话删除后 context-diag 行被清")
        gone = (await s.get(ChatConversation, conv_id)) is None
    check(gone, "会话行已删")


async def t_delete_research_task_rules():
    tid = "task-no-report"
    async with session_scope() as s:
        s.add(ResearchTask(id=tid, code="600519.SH", template="risk", status="error", stage="error"))
        await s.commit()
    r = await delete_research_task(tid)
    check(r["ok"] is True, "无报告任务可删")
    tid2 = "task-with-report"
    async with session_scope() as s:
        s.add(ResearchTask(id=tid2, code="600519.SH", template="risk", status="done", report_id="rep-1"))
        await s.commit()
    try:
        await delete_research_task(tid2)
        check(False, "有报告任务应 409")
    except HTTPException as e:
        check(e.status_code == 409, "有报告任务 409 引导去报告库删")


async def t_create_research_reuse_window():
    """B5：时区修复后，刚建的 pending 任务在 30s 窗口内应被复用而不是改判 error。"""
    import datetime as dt

    async with session_scope() as s:
        s.add(ResearchTask(id="fresh-pending", code="000999.SZ", template="risk", status="pending"))
        await s.commit()
        # 模拟刚写入：SQLite 侧 updated_at 由 server_default 生成（naive-UTC，几秒前）——正是生产形态
    r = await create_research(ResearchRequest(code="000999.SZ", template="risk"))
    check(r.get("reused") is True and r["task_id"] == "fresh-pending",
          f"30s 内重复提交复用 pending 任务（实际 {r}）")
    async with session_scope() as s:
        row = await s.get(ResearchTask, "fresh-pending")
        check(row.status == "pending", "复用时任务未被改判 error")


async def t_chat_compliance_gate_and_guard():
    conv_id = "conv-compliance"
    async with session_scope() as s:
        s.add(ChatConversation(id=conv_id, title="t"))
        await s.commit()

    # 合规闸：mock 流产出禁用词 token
    async def fake_stream(*, conv_id, text, history, profile, llm_cfg, images=None, user_msg=None):
        for piece in ("茅台基本面稳健。", "建议", "买入", "并持有。"):
            yield f'data: {{"type":"token","text":{chr(34)}{piece}{chr(34)}}}\n\n', piece

    routes._deepagent_chat_stream_orig = routes._deepagent_chat_stream
    routes._deepagent_chat_stream = fake_stream
    try:
        resp = await send_chat_message(conv_id, ChatSendRequest(
            message="怎么看茅台",
            llm={"base_url": "http://x", "api_key": "sk-test-key-1234567890", "model": "test-model"},
        ))
        body = ""
        async for chunk in resp.body_iterator:
            body += chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        check("并持有" not in body, "禁用词之后的 token 不再外发")
        check("合规要求截断" in body, "截断提示已发送")
        async with session_scope() as s:
            stmt = (
                select(ChatDelta)
                .where(ChatDelta.conversation_id == conv_id)
                .order_by(ChatDelta.seq.desc())
            )
            rows = list((await s.execute(stmt)).scalars().all())
            assistant = [r for r in rows if r.role == "assistant"][0]
            check("合规要求截断" in assistant.payload["message"]["content"], "落库文本含截断提示")
            check("并持有" not in assistant.payload["message"]["content"], "落库文本不含违禁后续")
    finally:
        routes._deepagent_chat_stream = routes._deepagent_chat_stream_orig

    # 并发守卫：占住锁后再发 → 409
    conv2 = "conv-guard"
    async with session_scope() as s:
        s.add(ChatConversation(id=conv2, title="t"))
        await s.commit()
    lock = routes._chat_lock(conv2)
    async with lock:
        try:
            await send_chat_message(conv2, ChatSendRequest(
                message="hi",
                llm={"base_url": "http://x", "api_key": "sk-test-key-1234567890", "model": "test-model"},
            ))
            check(False, "锁被占时应 409")
        except HTTPException as e:
            check(e.status_code == 409, f"同会话并发守卫 409（实际 {e.status_code}）")


async def t_active_llm_fallback_overrides_stale_conv_config():
    """发布后修复：对话冻存的旧 key 失效后，未显式选模型时应改用当前激活配置。

    用户实测场景：Settings 已换新 DeepSeek key（有效），旧对话 model_config 冻存
    旧 key（401）——旧逻辑 req.llm 为 None 时直接用冻存 key → 401。
    """
    conv_id = "conv-stale-key"
    from finresearch.tools.secrets import encrypt_config

    async with session_scope() as s:
        s.add(ChatConversation(id=conv_id, title="旧对话", model_config=encrypt_config({
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-old-invalid-key-5000",
            "model": "deepseek-old-model",
        })))
        await s.commit()
    await put_local_setting("active-model", LocalSettingRequest(payload={
        "providerId": "deepseek", "modelId": "deepseek-chat",
        "provider": "DeepSeek", "model": "deepseek-chat", "accent": "#000",
    }))
    await put_local_setting("model-configs", LocalSettingRequest(payload={
        "deepseek": {"baseUrl": "https://api.deepseek.com",
                     "apiKey": "sk-new-valid-key-d414", "selectedModel": "deepseek-chat"},
    }))

    captured = {}

    async def fake_stream(*, conv_id, text, history, profile, llm_cfg, images=None, user_msg=None):
        captured["llm_cfg"] = llm_cfg
        yield 'data: {"type":"token","text":"ok"}\n\n', "ok"

    orig = routes._deepagent_chat_stream
    routes._deepagent_chat_stream = fake_stream
    try:
        # 不带 req.llm（用户没点过模型选择器）
        resp = await send_chat_message(conv_id, ChatSendRequest(message="你好"))
        async for chunk in resp.body_iterator:
            pass
    finally:
        routes._deepagent_chat_stream = orig

    cfg = captured.get("llm_cfg") or {}
    check(cfg.get("api_key") == "sk-new-valid-key-d414", "未显式选模型时用当前激活 key（非对话冻存旧 key）")
    check(cfg.get("model") == "deepseek-chat", "模型取激活配置的 modelId")
    check(cfg.get("base_url") == "https://api.deepseek.com", "base_url 取激活配置")


async def t_active_llm_cfg_none_when_unconfigured():
    """无任何激活配置时返回 None（回落到冻存/env 的旧路径不回归）。"""
    cfg = await routes._active_llm_cfg()
    # 本 impl 的 DB 里 active-model/model-configs 已由上一用例写入——先清掉再验空
    async with session_scope() as s:
        await s.execute(__import__("sqlalchemy").delete(LocalSetting).where(
            LocalSetting.key.in_(["active-model", "model-configs"])))
        await s.commit()
    cfg = await routes._active_llm_cfg()
    check(cfg is None, "无激活配置时 _active_llm_cfg 返回 None")


async def t_overlay_current_key_wins_over_stale_cache():
    """v1.6.2 用户实测场景：应用开着换 key，前端 req.llm 带的是挂载时缓存的旧 key
    （…ebf8, 401）而 Settings 已是新 key（…8f9f, 有效）——同供应商必须 DB 优先。"""
    conv_id = "conv-overlay"
    async with session_scope() as s:
        s.add(ChatConversation(id=conv_id, title="t"))
        await s.commit()
    await put_local_setting("active-model", LocalSettingRequest(payload={
        "providerId": "deepseek", "modelId": "deepseek-v4-flash",
        "provider": "DeepSeek", "model": "deepseek-v4-flash", "accent": "#000",
    }))
    await put_local_setting("model-configs", LocalSettingRequest(payload={
        "deepseek": {"baseUrl": "https://api.deepseek.com",
                     "apiKey": "sk-fresh-8f9f", "selectedModel": "deepseek-v4-flash"},
        "moonshot": {"baseUrl": "https://api.moonshot.cn/v1",
                     "apiKey": "sk-moon-key", "selectedModel": "kimi-k3"},
    }))

    captured = {}

    async def fake_stream(*, conv_id, text, history, profile, llm_cfg, images=None, user_msg=None):
        captured["llm_cfg"] = llm_cfg
        yield 'data: {"type":"token","text":"ok"}\n\n', "ok"

    orig = routes._deepagent_chat_stream
    routes._deepagent_chat_stream = fake_stream
    try:
        # 同供应商：req.llm 带旧缓存 key + 自选模型 → DB 新 key 覆盖，模型保留
        resp = await send_chat_message(conv_id, ChatSendRequest(
            message="hi",
            llm={"base_url": "https://api.deepseek.com", "api_key": "sk-stale-ebf8", "model": "deepseek-v4-pro"},
        ))
        async for _ in resp.body_iterator:
            pass
        cfg = captured.get("llm_cfg") or {}
        check(cfg.get("api_key") == "sk-fresh-8f9f", "同供应商：DB 当前 key 覆盖缓存旧 key")
        check(cfg.get("model") == "deepseek-v4-pro", "显式选择的模型保留")

        # 跨供应商：req.llm 选 moonshot，active 是 deepseek → 不得越权替换
        captured.clear()
        resp = await send_chat_message(conv_id, ChatSendRequest(
            message="hi",
            llm={"base_url": "https://api.moonshot.cn/v1", "api_key": "sk-moon-key", "model": "kimi-k3"},
        ))
        async for _ in resp.body_iterator:
            pass
        cfg = captured.get("llm_cfg") or {}
        check(cfg.get("api_key") == "sk-moon-key" and cfg.get("base_url") == "https://api.moonshot.cn/v1",
              "跨供应商：显式选择不被 active 覆盖")
    finally:
        routes._deepagent_chat_stream = orig


async def main() -> None:
    await init_db()
    # 初始化检查点（与生产 lifespan 同序），让会话删除的线程清理断言真实生效
    from finresearch.agents.graph import close_checkpointer, init_checkpointer

    await init_checkpointer()
    try:
        await t_model_configs_encryption()
        await t_zombie_sweep()
        await t_delete_chat_cleans_checkpoint_thread()
        await t_delete_research_task_rules()
        await t_create_research_reuse_window()
        await t_chat_compliance_gate_and_guard()
        await t_active_llm_fallback_overrides_stale_conv_config()
        await t_active_llm_cfg_none_when_unconfigured()
        await t_overlay_current_key_wins_over_stale_cache()
    finally:
        # 不关检查点的话，aiosqlite 残留线程会让解释器在退出阶段挂死
        # （生产 lifespan 收尾同样会 close；这里必须对称）
        try:
            await close_checkpointer()
        except Exception:
            pass
    if fails:
        print(f"\n{len(fails)} FAILURES")
        sys.exit(1)
    print("ALL ROUTES-DB TESTS PASS")


if __name__ == "__main__":
    asyncio.run(main())
