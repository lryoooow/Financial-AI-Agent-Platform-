"""v2.0.0 Deep Research & Multi-Turn Numeric Evidence Audit Regressions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finresearch.agents.workers import market as market_worker
from finresearch.agents.workers import report as rep
from finresearch.agents.workers import research as research_worker
from finresearch.api import routes, sse
from finresearch.core.guardrails.number_evidence import NumberEvidence, metric_values
from finresearch.db import session
from finresearch.db.models import Base, Report, ResearchTask
from finresearch.knowledge import service as kb_service
from finresearch.knowledge import store as kb_store
from finresearch.llm import ThinkTagSplitter
from finresearch.main import app


class _Chunk:
    def __init__(self, content: str = "", rc: str = "") -> None:
        self.content = content
        self.additional_kwargs = {"reasoning_content": rc} if rc else {}


class _FakeStreamLLM:
    def __init__(self, pieces: list) -> None:
        self.pieces = pieces
        self.calls = 0

    async def astream(self, _messages):
        self.calls += 1
        for p in self.pieces:
            yield p if isinstance(p, _Chunk) else _Chunk(str(p))


def test_post_parenthetical_metric_label_does_not_leak_across_slash():
    """复现 688266.SH 多轮对话表格行：`88.26（日 MA5）/ 88.32（日 MA10）` 中 MA5 不得污染 88.32。"""
    ind_art = {
        "data": [
            {"name": "MA5", "value": 88.26, "signal": "空头"},
            {"name": "MA10", "value": 88.32, "signal": "空头"},
            {"name": "MA20", "value": 92.44, "signal": "空头"},
        ]
    }
    fields = metric_values(ind_art)
    ev = NumberEvidence("MA5=88.26 MA10=88.32 MA20=92.44 现价 86.80 元", fields, strict_fields=True)
    table = (
        "| 位置 | 数值 |\n"
        "| --- | --- |\n"
        "| 第一压力 | 88.26（日 MA5）/ 88.32（日 MA10） |\n"
        "| 第二压力 | 92.44（日 MA20） |\n"
    )
    assert ev.locate(table) == []


def test_number_evidence_trillion_scaling_rsi_bound_and_fact_date_mask():
    ev = NumberEvidence("总市值 1.85 万亿元，RSI(14) 72.4，合作潜在总额 2 亿美元，数据时点 2026-11-13 09:41:23，股票代码 688266.SH。")
    assert ev.locate("总市值约 18500 亿元，当前 RSI 高于 70，意味着任何 1-2 元的点位差别都会被波动率淹没。") == []
    # RSI 72.4 不满足「高于 80」
    assert len(ev.locate("当前 RSI 高于 80。")) == 1
    # 事实块日期 2026-11-13 中的 13 不得放行无依据的「下跌 13%」
    assert len(ev.locate("该股今日下跌 13%。")) == 1


def test_multi_turn_chat_reads_persisted_artifact_key_without_false_redactions(monkeypatch, tmp_path):
    """复现多轮对话：历史轮次 ChatDelta.artifacts_json 键名为 `artifact`，后续轮次引用前序行业估值与联网搜索数字零误杀。"""
    turn_idx = 0

    async def fake_stream(**_kwargs):
        nonlocal turn_idx
        turn_idx += 1
        if turn_idx == 1:
            yield sse.sse({
                "type": "tool_artifact",
                "name": "get_technical_indicators",
                "artifact": {
                    "data": [
                        {"name": "MA5", "value": 88.26, "signal": "空头"},
                        {"name": "MA10", "value": 88.32, "signal": "空头"},
                    ]
                },
            }), ""
            yield sse.sse({
                "type": "tool_artifact",
                "name": "search_industry",
                "artifact": {"items": [{"name": "化学制药", "pe_ttm": 32.05, "pb": 3.04}]},
            }), ""
            yield sse.sse({
                "type": "tool_artifact",
                "name": "web_search",
                "artifact": {"items": [{"title": "科创板主力资金单日净流出 43.75 亿元", "snippet": "商业化营收增长 44.3%"}]},
            }), ""
            msg1 = "首轮取数完成：行业 PE(TTM) 32.05 倍，主力净流出 43.75 亿元。"
            yield sse.ev_token(msg1), msg1
        else:
            # 第二轮不再调 search_industry / web_search，直接汇总首轮证据 + 表格压力位
            msg2 = (
                "| 第一压力 | 88.26（日 MA5）/ 88.32（日 MA10） |\n"
                "化学制药行业 PE(TTM) 32.05 倍、PB 3.04 倍；科创板主力资金单日净流出 43.75 亿元。"
            )
            for ch in msg2:
                yield sse.ev_token(ch), ch

    monkeypatch.setattr(routes, "_deepagent_chat_stream", fake_stream)

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "multiturn.db").as_posix())
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(session, "_engine", engine)
        monkeypatch.setattr(session, "_maker", async_sessionmaker(engine, expire_on_commit=False))
        monkeypatch.setattr(session, "_tried", True)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                c_res = await client.post("/api/chat/conversations", json={})
                conv_url = "/api/chat/conversations/" + c_res.json()["conversation"]["id"]
                llm = {"base_url": "http://127.0.0.1:8999/v1", "api_key": "k", "model": "m"}
                r1 = await client.post(conv_url + "/messages", json={"message": "分析泽璟制药", "llm": llm})
                assert r1.is_success and "〔数值待核〕" not in r1.text
                r2 = await client.post(conv_url + "/messages", json={"message": "继续总结关键位与行业估值", "llm": llm})
                assert r2.is_success
                assert "〔数值待核〕" not in r2.text
                assert "88.32" in r2.text and "32.05" in r2.text and "43.75" in r2.text
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_think_tag_splitter_orphan_close_never_leaks_cot_chunk():
    sp = ThinkTagSplitter()
    c1, r1 = sp.feed("先看财务数据，")
    assert c1 == "先看财务数据，" and r1 == ""
    c2, r2 = sp.feed("再核对 ## 技术面分析 标题</think>\n## 投资摘要\n正式摘要正文。")
    c3, r3 = sp.flush()
    assert sp.orphan_think_closed is True
    assert r2 == "再核对 ## 技术面分析 标题"
    assert (c2 + c3) == "## 投资摘要\n正式摘要正文。"


def test_report_stream_orphan_think_with_insufficient_section_does_not_crash(monkeypatch):
    """P0 回归：网关预填 <think> + 某章节数据不足（insufficient）时，裸 </think> 重置不得抛 KeyError 且不泄漏 CoT。"""
    fv = {
        "quote": {
            "name": "泽璟制药", "price": 87.98, "changePct": -1.2,
            "pe": 40.95, "pb": 13.55, "marketCap": 232e8,
            "provenance": {"source": "sina"},
        },
        "profile": {"name": "泽璟制药", "industry": "化学制药"},
        "fins": {"years": [], "rows": []},  # 基本面数据不足 -> insufficient
        "indicators": [{"name": "MA20", "value": "92.44", "signal": "空头"}],
        "kline": [{"close": 87.98, "ma5": 88.26, "ma20": 92.44}],
        "news": [{"title": "公司推进新药商业化", "source": "公告"}],
        "web": [],
    }
    pieces = [
        "先在思考链里列提纲：\n## 技术面分析\n草稿思考内容",
        "继续思考估值</think>\n",
        "## 投资摘要\n股价 87.98 元，短期位于均线下方。\n",
        "## 技术面分析\n现价 87.98 元低于 MA20（92.44 元）。\n",
        "## 估值分析\n当前 PE 40.95 倍、PB 13.55 倍。\n",
        "## 行业与公司分析\n化学制药赛道商业化推进中。\n",
    ]
    llm = _FakeStreamLLM(pieces)
    monkeypatch.setattr(rep, "build_llm", lambda **_kw: llm)
    pushed: list = []

    async def main():
        tok = rep.PUSH_SINK.set(pushed.append)
        try:
            specs = rep._section_specs("equity_deep")
            return await rep._llm_report_stream(
                {"template": "equity_deep", "normalized_ticker": "688266.SH", "llm_config": {"model": "deepseek-r1"}},
                specs,
                fv,
            )
        finally:
            rep.PUSH_SINK.reset(tok)

    (summary, sum_src), results = asyncio.run(main())
    assert sum_src == "llm"
    assert "草稿思考内容" not in summary and "继续思考估值" not in summary
    assert results[0][1] == "llm" and "92.44" in results[0][0]
    assert results[1][1] == "insufficient" and "数据不足" in results[1][0]
    assert results[2][1] == "llm" and "40.95" in results[2][0]
    assert results[3][1] == "llm"


def test_user_query_numbers_do_not_pollute_report_evidence_and_redaction_persists(monkeypatch):
    """P1 回归：用户 query 中的假设数字不得成为事实证据；单处无依据数字局部隐去并落入最终章节正文。"""
    fv = {
        "quote": {
            "name": "泽璟制药", "price": 87.98, "changePct": -1.2,
            "pe": 40.95, "pb": 13.55, "marketCap": 232e8,
            "provenance": {"source": "sina"},
        },
        "profile": {"name": "泽璟制药", "industry": "化学制药"},
        "fins": {"years": ["2025"], "rows": [{"name": "ROE", "values": ["12.5%"]}]},
        "indicators": [{"name": "MA20", "value": "92.44", "signal": "空头"}],
        "kline": [{"close": 87.98, "ma5": 88.26, "ma20": 92.44}],
        "news": [{"title": "泽璟制药披露经营进展", "source": "东方财富"}],
        "web": [],
    }
    pieces = [
        "## 投资摘要\n现价 87.98 元，ROE 为 12.5%。\n",
        "## 技术面分析\n现价 87.98 元，低于 92.44 元。\n",
        "## 基本面分析\n公司营收达到 999.8 亿元，ROE 为 12.5%。\n",
        "## 估值分析\n当前 PE 40.95 倍。\n",
        "## 行业与公司分析\n所处化学制药行业。\n",
    ]
    monkeypatch.setattr(rep, "build_llm", lambda **_kw: _FakeStreamLLM(pieces))
    state = {
        "template": "equity_deep",
        "normalized_ticker": "688266.SH",
        "query": "公司营收能达到 999.8 亿元吗？",
        "facts": fv,
        "findings": [],
        "write_mode": "stream",
    }
    out = asyncio.run(rep.report_node(state))
    doc = out["draft_report"]
    fund_sec = next(s for s in doc["sections"] if s["title"] == "基本面分析")
    assert "999.8" not in fund_sec["body"]
    assert "〔数值待核〕" in fund_sec["body"]
    assert "12.5%" in fund_sec["body"]
    ev_sec = next(s for s in doc["sections"] if s["title"] == "证据分层与数据来源")
    assert "数值核验" in ev_sec["body"] and "基本面分析" in ev_sec["body"]


def test_bound_generated_text_never_splits_redaction_marker():
    prefix = "现价 87.98 元，" * 5
    raw = prefix + "目标空间〔数值待核〕，后续观察量能。"
    cut_at = len(prefix) + len("目标空间〔数值")
    bounded, truncated = rep._bound_generated_text(raw, cut_at)
    assert truncated is True
    assert "〔数值…" not in bounded
    assert bounded.count("〔") == bounded.count("〕")


def test_market_node_250d_risk_window_and_unsigned_risk_section(monkeypatch):
    def fake_quote(_code):
        return {"name": "泽璟制药", "price": 87.98, "provenance": {"source": "sina"}}

    def fake_kline(_code, n=251, adjust="qfq"):
        assert n == 251
        bars = [
            {
                "date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                "open": 100.0,
                "close": 100.0 * (1.0 + 0.002 * ((i % 7) - 3)),
                "high": 102.0,
                "low": 98.0,
                "volume": 1000,
                "ma5": 100.0,
                "ma10": 100.0,
                "ma20": 100.0,
            }
            for i in range(251)
        ]
        return {"data": bars, "provenance": {"source": "tencent"}}

    def fake_bench(_code, n=251, *_a, **_kw):
        assert n == 251
        bars = [
            {"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "close": 4000.0 + (i % 5)}
            for i in range(251)
        ]
        return {"data": bars, "provenance": {"source": "tencent"}}

    monkeypatch.setattr(market_worker, "get_realtime_quote", fake_quote)
    monkeypatch.setattr(market_worker, "get_kline", fake_kline)
    monkeypatch.setattr(market_worker, "tencent_kline", fake_bench)

    m_out = asyncio.run(market_worker.market_node({"normalized_ticker": "688266.SH"}))
    kline_finding = next(f for f in m_out["findings"] if f["key"] == "kline")
    risk_finding = next(f for f in m_out["findings"] if f["key"] == "risk_metrics")
    assert len(kline_finding["kline"]) == 120
    assert risk_finding["risk"]["sample_days"] == 250

    monkeypatch.setattr(rep, "build_llm", lambda **_kw: None)
    r_out = asyncio.run(rep.report_node({
        "template": "equity_deep",
        "normalized_ticker": "688266.SH",
        "findings": m_out["findings"],
    }))
    risk_sec = next(s for s in r_out["draft_report"]["sections"] if s["title"] == "客观风险指标")
    assert "年化波动率 +" not in risk_sec["body"]
    assert "VaR +" not in risk_sec["body"]
    assert "CVaR +" not in risk_sec["body"]
    assert "近 250 个交易日" in risk_sec["body"]


def test_earnings_facts_include_news_and_failed_fins_not_cited(monkeypatch):
    fv = {
        "fins": {"years": ["2026Q3"], "rows": [{"name": "营业收入", "values": ["15.36亿"]}]},
        "news": [{"title": "泽璟制药收到艾伯维1亿美元首付款", "source": "公告"}],
        "web": [],
    }
    eb = rep._facts_earnings(fv)
    assert "15.36亿" in eb and "艾伯维1亿美元首付款" in eb

    monkeypatch.setattr(research_worker, "get_company_profile", lambda _c: {"name": "泽璟制药", "provenance": {"source": "akshare"}})
    monkeypatch.setattr(research_worker, "get_financial_indicators", lambda _c: (_ for _ in ()).throw(RuntimeError("timeout")))
    monkeypatch.setattr(research_worker, "search_news", lambda _c, limit=12: {"items": [], "provenance": {"source": "akshare"}})
    monkeypatch.setattr(research_worker, "_web_search_with_domestic_fallback", lambda _q, limit=8: {"items": [], "provenance": {"source": "eastmoney_search"}})
    res = asyncio.run(research_worker.research_node({"normalized_ticker": "688266.SH"}))
    assert not any(c.get("kind") == "fundamentals" for c in res["citations"])


def test_pump_never_emits_error_after_done_and_kb_delete_tombstone(monkeypatch, tmp_path):
    async def run():
        # 1) pump 在已发出 done 后若尾部抛异常，绝不再补发 error 帧
        async def factory():
            yield sse.sse({"type": "done", "report_id": "r-ok", "report": {}})
            raise RuntimeError("post-done cleanup boom")

        routes._RESEARCH_RUNS.pop("task-done-first", None)
        rt = routes._start_research_runtime("task-done-first", factory)
        await rt.task
        types = [routes._peek_event(f).get("type") for f in rt.history if routes._peek_event(f)]
        assert types == ["done"]
        routes._RESEARCH_RUNS.pop("task-done-first", None)

        # 2) 报告已删除墓碑：滞后的 on_report_saved 不得把已删报告重新入知识库
        monkeypatch.setattr(kb_store, "_DB_PATH", tmp_path / "kb.sqlite")
        kb_store.init()
        await kb_service.on_report_deleted("rep-deleted-race")
        await kb_service.on_report_saved(
            "rep-deleted-race",
            {"ticker": "688266.SH", "name": "泽璟制药", "summary": "不应被复活的摘要文本", "sections": []},
            code="688266.SH",
            template="equity_deep",
        )
        hits = await kb_service.search("不应被复活的摘要文本")
        assert not any(h.get("report_id") == "rep-deleted-race" for h in hits)

    asyncio.run(run())
