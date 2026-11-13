"""v1.7.1 研究链路修复的实际验证逻辑（被 test_research_v171.py 以子进程调用，临时 DB）。

覆盖：
1. 确定性编排：零模型规划往返，事件序列完整（取数 → 摘要 → 排队 → 开写 → 思考 → 吐字 → 收口）
2. 思维链两种形态（delta.reasoning_content / 正文内联 <think>）都不进入正文，并以
   section_thinking 快照透出
3. 确定性编排被提前关闭时撰写任务确定性取消（不在后台继续烧 token）
4. 审计 A04：不同问题 / 不同模型参数不复用任务；真正重复的请求仍复用
5. 审计 A05：模型未配置等提前失败出口，任务行落 error 且保留原因
6. 审计 A06：每个 SSE 连接以 replay 帧开头，回放历史已压缩
7. 工具失败（行业取数失败）→ 失败原因如实落库，不被笼统的「未生成报告草稿」覆盖
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + os.path.join(_tmp, "t.db").replace(os.sep, "/")
os.environ["LLM_API_KEY"] = ""
# v1.8.0：默认撰写模式改为单次流式；本脚本专测 v1.7 分段并行语义，显式固定 parallel
os.environ["REPORT_WRITE_MODE"] = "parallel"
os.environ["SERPER_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langchain_core.messages import AIMessageChunk  # noqa: E402

import finresearch.agents.deep.research_runner as runner  # noqa: E402
import finresearch.agents.deep.research_tools as rt  # noqa: E402
import finresearch.agents.workers.report as report  # noqa: E402
from finresearch.api import routes  # noqa: E402
from finresearch.db.session import init_db, session_scope  # noqa: E402
from finresearch.db.models import ResearchTask  # noqa: E402

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


# ── 合成数据（不访问任何外部数据源）────────────────────────────
async def fake_entity(state):
    return {"normalized_ticker": "600519", "data_as_of": "2026-10-13 10:00",
            "findings": [{"key": "entity", "title": "标的归一化", "code": "600519", "name": "贵州茅台"}]}


async def fake_research(state):
    return {"findings": [
        {"key": "profile", "title": "公司概况", "name": "贵州茅台", "industry": "白酒"},
        {"key": "financials", "title": "财务指标", "financials": {
            "years": ["2024", "2025"],
            "rows": [{"name": "ROE", "values": ["30.0%", "31.0%"]},
                     {"name": "毛利率", "values": ["91%", "92%"]}]}},
        {"key": "news", "title": "相关新闻", "items": [{"title": "新闻A", "url": "http://x", "time": "2026-10-01"}]},
        {"key": "web_search", "title": "联网搜索", "items": [], "configured": False},
    ], "citations": [], "timings": {}}


async def fake_market(state):
    kl = [{"date": f"2026-09-{i:02d}", "open": 100 + i, "close": 101 + i, "high": 102 + i,
           "low": 99 + i, "volume": 1000} for i in range(1, 29)]
    return {"findings": [
        {"key": "quote", "title": "实时快照", "quote": {"name": "贵州茅台", "price": 1500.0, "change": 10.0,
                                                      "changePct": 0.67, "pe": 25.0, "pb": 8.0}},
        {"key": "kline", "title": "K 线", "kline": kl},
        {"key": "indicators", "title": "技术指标", "indicators": [{"name": "RSI(14)", "value": 55.0, "signal": "中性"}]},
    ], "indicators": [], "risk_flags": [], "timings": {}}


rt.entity_node = fake_entity
rt.research_node = fake_research
rt.market_node = fake_market


class FakeLLM:
    """分段撰写用：前两块是思维链（两种形态各一），之后是正文。"""

    calls = 0
    cancelled = 0
    delay = 0.0

    async def astream(self, messages):
        FakeLLM.calls += 1
        try:
            yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "先核对事实块。"})
            yield AIMessageChunk(content="<think>内联思考")
            yield AIMessageChunk(content="过程</think>\n")
            for piece in ("事实", "分析", "。"):
                await asyncio.sleep(FakeLLM.delay)
                yield AIMessageChunk(content=piece)
        except asyncio.CancelledError:
            FakeLLM.cancelled += 1
            raise


report.build_llm = lambda **kw: FakeLLM()
LLM = {"base_url": "http://relay/v1", "api_key": "sk-x", "model": "m-a"}


async def collect(agen):
    out = []
    async for line in agen:
        out.append(json.loads(line[6:].strip()))
    return out


async def t_direct_flow():
    FakeLLM.calls = 0
    events = await collect(runner.run_research_direct(
        task_id="direct-1", code="600519.SH", template="equity_deep", llm_config=LLM, profile={}))
    kinds = [e["type"] for e in events]
    calls = [(e["name"], e["status"]) for e in events if e["type"] == "tool_call"]
    check(calls == [("prefetch_basics", "start"), ("prefetch_basics", "ok"),
                    ("compose_report", "start"), ("compose_report", "ok")],
          f"确定性编排：取数→撰写，各一次 start/ok（实际 {calls}）")
    check(FakeLLM.calls == 4, f"只有四段撰写调用模型，零规划往返（实际 {FakeLLM.calls} 次）")
    check(kinds.index("summary") < kinds.index("section_queued"), "摘要先于分段排队到达")
    check(kinds.count("section_queued") == 4 and kinds.count("section_start") == 4, "四段 queued/start 各一条")
    thinking = [e for e in events if e["type"] == "section_thinking"]
    check(bool(thinking) and all("事实分析" not in e["text"] for e in thinking), "思维链以 section_thinking 透出")
    bodies = {}
    for e in events:
        if e["type"] == "section_token":
            bodies[e["title"]] = bodies.get(e["title"], "") + e["text"]
    leaked = [t for t, b in bodies.items() if "思考" in b or "think" in b]
    check(not leaked and all(b == "事实分析。" for b in bodies.values()),
          f"思维链（两种形态）不进入正文 token（实际 {bodies}）")
    finals = {e["title"]: e for e in events if e["type"] == "section"}
    check(len(finals) == 4 and all(e["source"] == "llm" and e["body"] == "事实分析。" for e in finals.values()),
          "四段权威正文均为模型正文（思维链已剥离）")
    draft = rt.get_stored("direct-1", "draft_report")
    check(bool(draft) and len(draft.get("sections") or []) == 5, "报告草稿落入工具层存储（四段 + 证据分层）")
    rt.clear_task("direct-1")


async def t_direct_close_cancels_writing():
    FakeLLM.cancelled = 0
    FakeLLM.delay = 0.5
    agen = runner.run_research_direct(task_id="direct-2", code="600519.SH", template="equity_deep",
                                      llm_config=LLM, profile={})
    async for line in agen:
        if '"section_start"' in line:
            break
    await agen.aclose()
    await asyncio.sleep(0.05)
    check(FakeLLM.cancelled >= 1, f"提前关闭编排 → 在飞的分段撰写被取消（取消 {FakeLLM.cancelled} 段）")
    FakeLLM.delay = 0.0
    rt.clear_task("direct-2")


async def t_industry_failure_reason():
    orig = rt._timed_call

    async def fake_timed(fn, *a, **kw):
        return {}, 0, "timeout"

    rt._timed_call = fake_timed
    try:
        events = await collect(runner.run_research_direct(
            task_id="ind-1", code="光伏", template="industry", llm_config=LLM, profile={}))
    finally:
        rt._timed_call = orig
    calls = [(e["name"], e["status"]) for e in events if e["type"] == "tool_call"]
    check(calls == [("prefetch_industry", "start"), ("prefetch_industry", "error")],
          f"行业取数失败即停，不再调用撰写（实际 {calls}）")
    reason = rt.get_stored("ind-1", "failure") or ""
    check("数据源暂时不可用" in reason, f"失败原因如实保留（{reason!r}）")
    rt.clear_task("ind-1")


async def t_a04_reuse_rules():
    a = await routes.create_research(routes.ResearchRequest(code="600519.SH", template="risk", query="偿债能力"))
    b = await routes.create_research(routes.ResearchRequest(code="600519.SH", template="risk", query="海外收入"))
    check(a["task_id"] != b["task_id"], "A04：不同问题不复用任务")
    c = await routes.create_research(routes.ResearchRequest(code="600519.SH", template="risk", query=" 海外收入 "))
    check(c.get("reused") is True and c["task_id"] == b["task_id"], "真正重复的请求（同问题）仍在 30s 窗口内复用")
    d = await routes.create_research(routes.ResearchRequest(code="000002.SZ", template="risk", llm={**LLM, "model": "m-a"}))
    e = await routes.create_research(routes.ResearchRequest(code="000002.SZ", template="risk", llm={**LLM, "model": "m-b"}))
    check(d["task_id"] != e["task_id"], "A04：不同模型不复用任务")
    async with session_scope() as s:
        row = await s.get(ResearchTask, e["task_id"])
        cfg = routes.decrypt_config(row.llm_config)
    check(cfg.get("model") == "m-b", "第二个任务保存的是它自己的模型配置")
    f = await routes.create_research(routes.ResearchRequest(
        code="000002.SZ", template="risk", llm={**LLM, "model": "m-b", "api_key": "sk-rotated"}))
    check(f.get("reused") is True, "仅 api_key 不同（同供应商换 key）视为同一请求")


class _Req:
    async def is_disconnected(self):
        return False


async def _drain(resp):
    out = []
    async for frame in resp.body_iterator:
        out.append(json.loads(frame[6:].strip()))
    return out


async def t_a05_and_replay():
    async def none_cfg():
        return None

    orig = routes._active_llm_cfg
    original_llm = report.build_llm
    routes._active_llm_cfg = none_cfg
    report.build_llm = lambda **_kwargs: None
    try:
        task = await routes.create_research(routes.ResearchRequest(code="000001.SZ"))
        events = await _drain(await routes.research_stream(_Req(), task["task_id"]))
        await routes._RESEARCH_RUNS[task["task_id"]].task
        stored = await routes.get_research_task(task["task_id"])
    finally:
        routes._active_llm_cfg = orig
        report.build_llm = original_llm
    check(events[0]["type"] == "replay", "A06：SSE 连接首帧为 replay")
    check(events[-1]["type"] == "done", "无模型 → 完成事实模板报告")
    check(stored["status"] == "done" and stored.get("report_id"), "无模型报告与终态成功落库")
    again = await _drain(await routes.research_stream(_Req(), task["task_id"]))
    check(again[0]["type"] == "replay" and again[-1]["type"] == "done", "已归档报告回放终态")


async def t_full_pipeline_via_route():
    task = await routes.create_research(routes.ResearchRequest(code="600519.SH", llm=LLM))
    events = await _drain(await routes.research_stream(_Req(), task["task_id"]))
    await routes._RESEARCH_RUNS[task["task_id"]].task
    stored = await routes.get_research_task(task["task_id"])
    check(events[-1]["type"] == "done" and stored["status"] == "done" and stored["report_id"],
          "完整研究：done 帧在落库后发出，任务行 done 且关联报告")
    runtime = routes._RESEARCH_RUNS[task["task_id"]]
    replay = [routes._peek_event(f) for f in runtime.history]
    tokens = [e for e in replay if e and e["type"] == "section_token"]
    check(len(tokens) == 4 and all(e["text"] == "事实分析。" for e in tokens),
          f"回放历史按段压缩（{len(tokens)} 帧 section_token）")
    # 新连接回放：replay 首帧 + 压缩历史，且以 done 结束
    late = await _drain(await routes.research_stream(_Req(), task["task_id"]))
    check(late[0]["type"] == "replay" and late[-1]["type"] == "done", "任务结束后的新连接拿到 replay + 终态报告")


async def main():
    await init_db()
    await t_direct_flow()
    await t_direct_close_cancels_writing()
    await t_industry_failure_reason()
    await t_a04_reuse_rules()
    await t_a05_and_replay()
    await t_full_pipeline_via_route()
    if fails:
        print(f"\n{len(fails)} FAILURES")
        sys.exit(1)
    print("\nALL RESEARCH-V171 TESTS PASS")


if __name__ == "__main__":
    asyncio.run(main())
