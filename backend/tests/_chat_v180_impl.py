"""v1.8.0 对话/研究融合与上下文治理的路由级验证（被 test_chat_v180.py 以子进程调用，临时 DB）。

覆盖：
1. 研究挂会话：POST /research 带 conversation_id → 会话追加「研究请求 + 研究卡片」，标题更新；归档会话 409
2. 研究结果注入：上一轮正常回复之后的研究卡片在下一轮【本轮上下文】注入一次（含 report_id 与摘要）
3. 自动压缩：线程用量超阈值 → 交接摘要、代际 +1、新线程 thread_id=conv:g1、以摘要播种，摘要入知识库
4. 未超阈值：只发本轮消息（带时间锚点上下文），线程不变
5. 未播种老会话：按预算取最近几轮，不再整段塞历史
6. 删除会话：逐代删除检查点线程 + 知识库会话摘要
7. 单次流式研究端到端（路由 → 确定性编排 → 单次流式撰写）：一次模型请求、摘要 token 先于章节、done 后入知识库
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + os.path.join(_tmp, "t.db").replace(os.sep, "/")
os.environ["FINRESEARCH_KNOWLEDGE_DB"] = os.path.join(_tmp, "knowledge.db")
os.environ["LLM_API_KEY"] = ""
os.environ["SERPER_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from finresearch.agents.deep import chat_agent as chat_agent_mod  # noqa: E402
from finresearch.agents.deep import compaction  # noqa: E402
from finresearch.api import routes  # noqa: E402
from finresearch.db.models import ChatContextSummary, ChatConversation, Report, ResearchTask  # noqa: E402
from finresearch.db.session import init_db, session_scope  # noqa: E402
from finresearch.knowledge import store as kb_store  # noqa: E402

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


_LLM = {"base_url": "http://relay.invalid/v1", "api_key": "sk-test", "model": "relay-model"}


async def _new_conv(title="新对话") -> str:
    async with session_scope() as s:
        c = ChatConversation(title=title)
        s.add(c)
        await s.commit()
        return c.id


async def t_research_in_conversation():
    conv = await _new_conv()
    out = await routes.create_research(routes.ResearchRequest(
        code="600519.SH", template="equity_deep", conversation_id=conv, name="贵州茅台", llm=_LLM))
    msgs = out.get("messages") or []
    check(len(msgs) == 2 and msgs[0]["kind"] == "research_request" and msgs[1]["kind"] == "research",
          "研究挂会话：追加研究请求 + 研究卡片两条消息")
    check(msgs[1]["research"]["task_id"] == out["task_id"], "研究卡片携带 task_id")
    async with session_scope() as s:
        history = await routes._chat_history(s, conv)
        c = await s.get(ChatConversation, conv)
    check(len(history) == 2 and c.title.startswith("研究 · 贵州茅台"), f"会话落库 + 标题更新（{c.title}）")
    check("api_key" not in json.dumps(msgs, ensure_ascii=False), "卡片消息不含密钥")

    async with session_scope() as s:
        c = await s.get(ChatConversation, conv)
        import datetime as dt
        c.archived_at = dt.datetime.now(dt.UTC)
        await s.commit()
    try:
        await routes.create_research(routes.ResearchRequest(code="000858.SZ", conversation_id=conv))
        check(False, "归档会话发起研究应 409")
    except routes.HTTPException as e:
        check(e.status_code == 409, "归档会话发起研究 409")


async def t_research_injection_once():
    conv = await _new_conv()
    out = await routes.create_research(routes.ResearchRequest(
        code="600519.SH", template="equity_deep", conversation_id=conv, name="贵州茅台", llm=_LLM))
    report_id = "rep-inject-1"
    async with session_scope() as s:
        s.add(Report(id=report_id, task_id=out["task_id"], ticker="600519.SH", name="贵州茅台",
                     template="equity_deep", summary="茅台摘要",
                     payload={"summary": "我们认为茅台经营稳健。", "as_of": "2026-10-13 15:00",
                              "sections": [{"title": "估值分析", "body": "PE 22 倍"}]}))
        t = await s.get(ResearchTask, out["task_id"])
        t.status, t.report_id = "done", report_id
        await s.commit()
        history = await routes._chat_history(s, conv)
    history = history + [{"role": "user", "content": "这份报告怎么看？"}]
    ctx = await routes._build_turn_context(history)
    check("【本轮上下文】" in ctx and "【当前时间】" in ctx, "本轮上下文含时间锚点")
    check(report_id in ctx and "我们认为茅台经营稳健" in ctx, "研究完成后下一轮注入报告摘要与 report_id")
    later = history + [{"role": "assistant", "content": "回复"}, {"role": "user", "content": "再问"}]
    ctx2 = await routes._build_turn_context(later)
    check(report_id not in ctx2, "已回复过的研究不再重复注入")


class _FakeAgent:
    def __init__(self, threads):
        self.threads = threads

    async def aget_state(self, config):
        tid = config["configurable"]["thread_id"]
        return type("S", (), {"values": {"messages": self.threads.get(tid, [])}})()


class _FakeSumLLM:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, _msgs):
        self.calls += 1
        return AIMessage("【用户目标】研究茅台估值\n【涉及标的与关键数据】贵州茅台（600519.SH）PE 22 倍（2026-10-13）")


async def _drain(gen):
    out = []
    async for line, piece in gen:
        out.append(line)
    return out


async def t_compaction_rollover():
    conv = await _new_conv("茅台讨论")
    big = [HumanMessage("问" + "茅台估值" * 1500), AIMessage("答" + "现金流" * 1500)] * 2
    agent = _FakeAgent({conv: big})
    captured = {}
    sum_llm = _FakeSumLLM()

    async def fake_stream(_agent, inputs, config, **_kw):
        captured["msgs"] = inputs["messages"]
        captured["thread"] = config["configurable"]["thread_id"]
        yield 'data: {"type":"token","text":"ok"}\n\n', "ok"

    orig_build, orig_stream, orig_llm = chat_agent_mod.build_chat_agent, routes.stream_agent_sse, routes.build_llm
    chat_agent_mod.build_chat_agent = lambda **_kw: agent
    routes.stream_agent_sse = fake_stream
    routes.build_llm = lambda **_kw: sum_llm
    try:
        prefs = {"context_window_k": 16, "compact_threshold_pct": 70, "learn_profile": False}
        lines = await _drain(routes._deepagent_chat_stream(
            conv_id=conv, text="继续聊估值", history=[{"role": "user", "content": "继续聊估值"}],
            profile={}, llm_cfg=_LLM, prefs=prefs, turn_context="【本轮上下文】\nX\n【本轮上下文结束】",
            title="茅台讨论"))
        check(any('"context_compacted"' in ln for ln in lines), "超阈值触发自动压缩事件")
        check(sum_llm.calls == 1, "交接摘要只调用一次模型")
        check(captured.get("thread") == f"{conv}:g1", f"新线程代际 conv:g1（实际 {captured.get('thread')}）")
        msgs = captured.get("msgs") or []
        check(bool(msgs) and msgs[0]["content"].startswith(compaction.SUMMARY_PREFIX), "新线程以前情摘要播种")
        check("继续聊估值" in msgs[-1]["content"] and "【本轮上下文】" in msgs[-1]["content"], "本轮消息带本轮上下文")
        async with session_scope() as s:
            row = await s.get(ChatContextSummary, conv)
        check(row is not None and row.generation == 1 and "茅台" in row.summary, "压缩代际与摘要落库")
        hits = kb_store.search("茅台 估值", kind="conversation")
        check(bool(hits) and hits[0]["conversation_id"] == conv, "对话摘要进入知识库（跨会话记忆）")

        # 下一轮：新线程已有内容且未超阈值 → 只发本轮消息
        agent.threads[f"{conv}:g1"] = [HumanMessage("短"), AIMessage("短答")]
        captured.clear()
        await _drain(routes._deepagent_chat_stream(
            conv_id=conv, text="再问", history=[], profile={}, llm_cfg=_LLM, prefs=prefs, turn_context=""))
        check(captured.get("thread") == f"{conv}:g1" and len(captured.get("msgs") or []) == 1,
              "未超阈值：沿用当前代际线程，只发本轮消息")
        check(sum_llm.calls == 1, "未超阈值不再调用摘要")

        # 未播种的老会话：大历史按预算截取
        conv2 = await _new_conv()
        history = []
        for i in range(200):
            history += [{"role": "user", "content": f"第{i}问" + "字" * 200},
                        {"role": "assistant", "content": f"第{i}答" + "字" * 200}]
        history.append({"role": "user", "content": "最新问题"})
        captured.clear()
        await _drain(routes._deepagent_chat_stream(
            conv_id=conv2, text="最新问题", history=history, profile={}, llm_cfg=_LLM,
            prefs={"context_window_k": 16, "learn_profile": False}, turn_context=""))
        msgs = captured.get("msgs") or []
        seeded_chars = sum(len(m["content"]) for m in msgs[:-1])
        check(0 < seeded_chars / 1.5 <= 16_000 * 0.5 + 50, f"未播种会话按预算播种（约 {int(seeded_chars / 1.5)} token）")
        check(msgs[-1]["content"] == "最新问题" and "第199答" in msgs[-2]["content"], "保留最近几轮 + 本轮")
    finally:
        chat_agent_mod.build_chat_agent, routes.stream_agent_sse, routes.build_llm = orig_build, orig_stream, orig_llm

    # 删除会话：逐代删除线程 + 知识库会话摘要
    deleted = []

    class _CP:
        async def adelete_thread(self, tid):
            deleted.append(tid)

    orig_cp = routes.get_checkpointer
    routes.get_checkpointer = lambda: (_CP(), "fake")
    try:
        await routes.delete_chat_conversation(conv)
    finally:
        routes.get_checkpointer = orig_cp
    check(deleted == [conv, f"{conv}:g1"], f"删除会话逐代删除检查点线程（{deleted}）")
    check(kb_store.search("茅台 估值", kind="conversation") == [], "删除会话同步删除知识库摘要")


async def t_single_stream_research_e2e():
    """路由 → 确定性编排 → 单次流式撰写：取数打桩，模型打桩为逐字吐字的假流。"""
    import finresearch.agents.deep.research_tools as rt
    import finresearch.agents.workers.report as report

    async def fake_entity(state):
        return {"normalized_ticker": "600519", "data_as_of": "2026-10-13 10:00", "findings": []}

    async def fake_research(state):
        return {"findings": [
            {"key": "profile", "title": "公司概况", "name": "贵州茅台", "industry": "白酒"},
            {"key": "financials", "title": "财务指标", "financials": {
                "years": ["2024", "2025"], "rows": [{"name": "ROE", "values": ["30.0%", "31.0%"]}]}},
        ], "citations": [], "timings": {}}

    async def fake_market(state):
        kl = [{"date": f"2026-09-{i:02d}", "open": 100 + i, "close": 101 + i, "high": 102 + i,
               "low": 99 + i, "volume": 1000} for i in range(1, 29)]
        return {"findings": [
            {"key": "quote", "title": "实时快照", "quote": {"name": "贵州茅台", "price": 1500.0, "change": 10.0,
                                                          "changePct": 0.67, "pe": 25.0, "pb": 8.0}},
            {"key": "kline", "title": "K 线", "kline": kl},
            {"key": "indicators", "title": "技术指标",
             "indicators": [{"name": "RSI(14)", "value": 55.0, "signal": "中性"}]},
        ], "indicators": [], "risk_flags": [], "timings": {}}

    text = ("## 投资摘要\n我们认为经营稳健。\n## 技术面分析\n均线偏强。\n## 基本面分析\n营收增长。\n"
            "## 估值分析\nPE 22 倍。\n## 行业与公司分析\n龙头地位稳固。\n")

    class _Chunk:
        def __init__(self, c):
            self.content = c
            self.additional_kwargs = {}

    class _LLM:
        calls = 0

        async def astream(self, _m):
            _LLM.calls += 1
            for ch in text:
                await asyncio.sleep(0)
                yield _Chunk(ch)

    saved = (rt.entity_node, rt.research_node, rt.market_node, report.build_llm)
    rt.entity_node, rt.research_node, rt.market_node = fake_entity, fake_research, fake_market
    report.build_llm = lambda **_kw: _LLM()
    try:
        out = await routes.create_research(routes.ResearchRequest(code="600519.SH", template="equity_deep", llm=_LLM_CFG))
        task_id = out["task_id"]

        class _Req:
            async def is_disconnected(self):
                return False

        resp = await routes.research_stream(_Req(), task_id)
        events = []
        async for frame in resp.body_iterator:
            frame = frame.decode() if isinstance(frame, bytes) else frame
            if frame.startswith("data: "):
                ev = json.loads(frame[6:])
                events.append(ev)
                if ev.get("type") in ("done", "error"):
                    break
        types = [e.get("type") for e in events]
        errs = [e.get("message") for e in events if e.get("type") == "error"]
        check("done" in types, f"研究完成（{types[-3:]} {errs}）")
        check(_LLM.calls == 1, f"整份报告只发起一次模型请求（实际 {_LLM.calls}）")
        first_sum = types.index("summary_token") if "summary_token" in types else 10**6
        first_sec = types.index("section_token") if "section_token" in types else -1
        check(first_sum < first_sec, "摘要 token 先于章节 token（自上而下一次性流式）")
        summaries = [e for e in events if e.get("type") == "summary"]
        check(bool(summaries) and summaries[0].get("text") == "", "摘要头信息先行且不带规则摘要正文")
        done = next((e for e in events if e.get("type") == "done"), None)
        if done is None:
            return
        check(done["report"]["summary"].startswith("我们认为"), "最终报告摘要来自模型流")
        await asyncio.sleep(0.05)
        hits = kb_store.search("经营稳健")
        check(any(h["report_id"] == done.get("report_id") for h in hits), "报告自动进入知识库")

        # 评审 #1：报告删除后再连研究流（重开对话时研究卡片会这么做）→ 只回终态错误，绝不重跑
        await routes.delete_report(done["report_id"])
        # 模拟真实环境「有可用模型」：旧实现会拿它把已删任务整份重跑（测试须能抓到真重跑）
        routes._TASK_LLM[task_id] = _LLM_CFG
        calls_before = _LLM.calls
        resp2 = await routes.research_stream(_Req(), task_id, code="600519.SH")
        frames = []
        async for frame in resp2.body_iterator:
            frame = frame.decode() if isinstance(frame, bytes) else frame
            if frame.startswith("data: "):
                frames.append(json.loads(frame[6:]))
        kinds = [f.get("type") for f in frames]
        check(kinds == ["replay", "error"] and "已被删除" in frames[-1].get("message", ""),
              f"已删报告的研究流只回终态错误（{kinds}）")
        await asyncio.sleep(0.05)
        check(_LLM.calls == calls_before, "已删报告不会被静默重跑（无新的模型请求）")
        check(not any(h["report_id"] == done["report_id"] for h in kb_store.search("经营稳健")), "知识库同步移除已删报告")
    finally:
        rt.entity_node, rt.research_node, rt.market_node, report.build_llm = saved


_LLM_CFG = {"base_url": "http://relay.invalid/v1", "api_key": "sk-test", "model": "deepseek-chat"}


async def main():
    from finresearch.agents.graph import close_checkpointer, init_checkpointer

    await init_db()
    await init_checkpointer()
    try:
        await t_research_in_conversation()
        await t_research_injection_once()
        await t_compaction_rollover()
        await t_single_stream_research_e2e()
    finally:
        try:
            await close_checkpointer()
        except Exception:
            pass
    if fails:
        print(f"\n{len(fails)} FAILURES")
        sys.exit(1)
    print("ALL CHAT-V180 TESTS PASS")


if __name__ == "__main__":
    asyncio.run(main())
