"""对话中断落库的实际验证逻辑（被 test_chat_interrupt.py 以子进程调用）。

单独成文件并以下划线开头：不被 pytest 直接收集为用例，只在自己的进程里跑，
用临时 DB，既不碰用户真实数据，也不污染共享 pytest 会话的 DB 引擎单例。

验证三件事（对应 routes.send_chat_message 的会话一致性修复）：
1. 流中途模型调用抛异常 → 已流出的半截回复落库（问题不再悬空无答）；
2. 客户端断开（CancelledError）→ 半截回复同样落库（DB 与检查点不分叉）；
3. 同一会话锁：持锁期间第二个请求被阻塞（串行化），流结束后锁被释放。
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

from finresearch.db.session import init_db, session_scope  # noqa: E402
from finresearch.db.models import ChatConversation, ChatDelta  # noqa: E402
from finresearch.api import routes  # noqa: E402

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


LLM = {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-test", "model": "deepseek-chat"}


async def seed_conversation(cid: str) -> None:
    async with session_scope() as s:
        s.add(ChatConversation(id=cid, title="t"))
        await s.commit()


async def deltas(cid: str) -> list[dict]:
    async with session_scope() as s:
        rows = (await s.execute(
            ChatDelta.__table__.select().where(ChatDelta.__table__.c.conversation_id == cid)
        )).all()
        return [
            {"seq": r.seq, "role": r.role, "content": (r.payload or {}).get("message", {}).get("content", "")}
            for r in rows
        ]


def fake_stream(tokens, then_raise):
    """构造 _deepagent_chat_stream 替身：吐出 tokens 后抛 then_raise。"""
    async def _stream(**kwargs):
        for t in tokens:
            yield (f"data: {{\"type\": \"token\"}}\n\n", t)
        raise then_raise
    return _stream


async def main() -> None:
    await init_db()

    # ── 场景 1：模型调用异常（Exception 路径）→ 半截落库 + ev_error ──
    cid1 = "conv-error"
    await seed_conversation(cid1)
    routes._deepagent_chat_stream = fake_stream(["茅台", "最新价"], RuntimeError("boom"))
    resp = await routes.send_chat_message(cid1, routes.ChatSendRequest(message="茅台多少钱", llm=LLM))
    events = []
    try:
        async for line in resp.body_iterator:
            events.append(line)
    except Exception:
        pass
    rows = await deltas(cid1)
    texts = [r["content"] for r in rows]
    check(any("茅台多少钱" in t for t in texts), "异常路径：用户消息已落库")
    check(any("茅台最新价" in t and "（回复中断）" in t for t in texts),
          f"异常路径：半截回复带中断标注落库，实际={texts}")
    check(not routes._chat_lock(cid1).locked(), "异常路径：会话锁已释放")
    check(any("error" in str(e) for e in events), "异常路径：发出了 ev_error 事件")

    # ── 场景 2：客户端断开（CancelledError 路径）→ 半截落库后向上传递取消 ──
    cid2 = "conv-cancel"
    await seed_conversation(cid2)
    routes._deepagent_chat_stream = fake_stream(["第一段", "第二段"], asyncio.CancelledError())
    resp2 = await routes.send_chat_message(cid2, routes.ChatSendRequest(message="继续分析", llm=LLM))
    cancelled = False
    try:
        async for _line in resp2.body_iterator:
            pass
    except asyncio.CancelledError:
        cancelled = True
    rows2 = await deltas(cid2)
    texts2 = [r["content"] for r in rows2]
    check(cancelled, "取消路径：CancelledError 语义被保留（向上 re-raise）")
    check(any("第一段第二段" in t and "（回复被中断）" in t for t in texts2),
          f"取消路径：半截回复落库，实际={texts2}")
    check(not routes._chat_lock(cid2).locked(), "取消路径：会话锁已释放")

    # ── 场景 3：会话锁串行化——持锁时第二个请求被阻塞 ──
    cid3 = "conv-lock"
    await seed_conversation(cid3)
    lock = routes._chat_lock(cid3)
    async with lock:
        routes._deepagent_chat_stream = fake_stream(["x"], RuntimeError("unused"))
        try:
            await asyncio.wait_for(
                routes.send_chat_message(cid3, routes.ChatSendRequest(message="hi", llm=LLM)),
                timeout=0.5,
            )
            check(False, "并发路径：第二个请求应当被会话锁阻塞")
        except asyncio.TimeoutError:
            check(True, "并发路径：第二个请求被会话锁阻塞（串行化生效）")

    # 释放锁后同一会话可以正常进入（死锁防护）
    resp3 = await routes.send_chat_message(cid3, routes.ChatSendRequest(message="hi", llm=LLM))
    async for _line in resp3.body_iterator:
        pass
    check(not routes._chat_lock(cid3).locked(), "并发路径：锁释放后可重入，无死锁")

    # ── 场景 4：静默期心跳——模型首字前也必须有字节下发 ──
    cid4 = "conv-hb"
    await seed_conversation(cid4)
    routes._CHAT_HEARTBEAT_S = 0.2  # 测试用短间隔（生产 10s）

    async def slow_stream(**kwargs):
        await asyncio.sleep(0.5)
        yield ('data: {"type": "token"}\n\n', "ok")

    routes._deepagent_chat_stream = slow_stream
    resp4 = await routes.send_chat_message(cid4, routes.ChatSendRequest(message="hi", llm=LLM))
    hb = 0
    async for line in resp4.body_iterator:
        if "heartbeat" in str(line):
            hb += 1
    check(hb >= 1, f"心跳路径：静默期发出 heartbeat（实际 {hb} 个）")
    check(not routes._chat_lock(cid4).locked(), "心跳路径：会话锁已释放")

    print("FAILED" if fails else "ALL PASS")
    sys.exit(1 if fails else 0)


asyncio.run(main())
