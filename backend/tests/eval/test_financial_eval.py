"""Deterministic planner accuracy, tool-scope enforcement and Golden Span retrieval."""

import asyncio

from finresearch.agents.workers import research
from finresearch.core.guardrails.number_evidence import NumberEvidence
from finresearch.engine.router import choose_route
from finresearch.engine.turn_context import TurnToolState
from finresearch.knowledge.store import search, upsert_document


def test_planner_first_tool_accuracy():
    cases = [
        ("今天收盘复盘", "", "", "market_overview"),
        ("复盘大盘成交额", "", "", "market_overview"),
        ("巡检自选里的标的", "", "", "batch_radar"),
        ("批量体检一下", "", "", "batch_radar"),
        ("做贵州茅台和五粮液同业对比", "", "", "batch_radar"),
        ("横向对比公司", "", "", "batch_radar"),
        ("回看之前生成的研报", "", "", "search_knowledge"),
        ("在知识库里找毛利率", "", "600519.SH", "search_knowledge"),
        ("请查现价", "", "600519.SH", "get_realtime_quote"),
        ("今天的估值", "", "600519.SH", "get_realtime_quote"),
        ("给我前复权 K线", "", "600519.SH", "get_kline"),
        ("近一个月均线", "", "600519.SH", "get_kline"),
        ("看近三年 ROE", "", "600519.SH", "get_financial_indicators"),
        ("最近一次年报现金流", "", "600519.SH", "get_financial_indicators"),
        ("近60日最大回撤和 Beta", "", "600519.SH", "compute_risk_metrics"),
        ("最近有哪些公告", "", "600519.SH", "search_news"),
        ("帮我看宁德时代的股价", "", "", "resolve_entity"),
        ("看平安的行情", "", "", "resolve_entity"),
        ("半导体行业", "industry", "", "prefetch_industry"),
        ("贵州茅台", "equity_deep", "600519.SH", "prefetch_basics"),
    ]
    correct = sum(choose_route(query, template, active_symbol=symbol).first_tool == target for query, template, symbol, target in cases)
    assert correct / len(cases) >= 0.95, (correct, len(cases))


def test_negative_scope_blocks_before_provider_calls(monkeypatch):
    called = []

    def use(name, payload):
        def call(*_args, **_kwargs):
            called.append(name)
            return payload
        return call

    for name in ("search_news", "web_search"):
        monkeypatch.setattr(research, name, use(name, {"items": []}))
    monkeypatch.setattr(research, "get_company_profile", use("profile", {"name": "样本公司"}))
    monkeypatch.setattr(research, "get_financial_indicators", use("financials", {"financials": {"years": [], "rows": []}}))
    for query in ("不用查新闻直接看近三年 ROE", "别再查询新闻，只看财报", "跳过新闻，查净利润", "排除新闻，直接核对 ROE"):
        scope = choose_route(query, active_symbol="600519.SH")
        assert scope.first_tool == "get_financial_indicators"
        state = TurnToolState(precondition_blocks=scope.blocked_tools)
        assert all(not state.try_reserve(tool)[0] for tool in ("search_news", "web_search"))
        asyncio.run(research.research_node({"code": "600519.SH", "blocked_tools": scope.blocked_tools}))
    assert set(called) == {"profile", "financials"}


def test_multi_hop_golden_span_recall_and_number_fidelity(monkeypatch, tmp_path):
    monkeypatch.setenv("FINRESEARCH_KNOWLEDGE_DB", str(tmp_path / "golden.db"))
    cases = [
        ("明信电池", "经营现金流", "资本开支", "28.4亿", "12.7亿"),
        ("东越精工", "营收同比", "利润同比", "15.6%", "8.3%"),
        ("海川材料", "资产负债率", "净资产收益率", "42.8%", "11.2%"),
        ("双谷设备", "估值市盈率", "研发费用", "18.9倍", "7.1亿"),
    ]
    hits_count = 0
    for name, metric_a, metric_b, value_a, value_b in cases:
        for heading, value in ((metric_a, value_a), (metric_b, value_b)):
            assert upsert_document(kind="external", ref_id=name + heading, title=name + heading + "披露资料", name=name,
                                   chunks=[(heading, f"{name}{heading}为 {value}。对应报告期已经核实。"),
                                           ("可比口径", f"{name}披露数据来自原始报告。")])
        for other in ("生产基地", "董事会治理", "海外渠道"):
            assert upsert_document(kind="external", ref_id=name + other, title=name + other,
                                   chunks=[(other, f"{name}{other}报告，无相关财务口径。")])
    for name, metric_a, metric_b, value_a, value_b in cases:
        hits = search(f"{name} {metric_a} {metric_b}", limit=5)
        facts = "\n".join(hit["text"] for hit in hits)
        hits_count += sum(f"{name}{metric}为 {value}" in facts for metric, value in ((metric_a, value_a), (metric_b, value_b)))
        assert any(hit["adjacent"] for hit in hits if hit["heading"] in (metric_a, metric_b))
        guard = NumberEvidence(facts)
        assert not guard.check(f"{metric_a} {value_a}，{metric_b} {value_b}。")
        assert guard.check(f"{metric_a} 99.9999%。")
        wrong_unit = "倍" if "%" in value_a else "%"
        assert guard.check(f"{metric_a} {value_a[:-1]}{wrong_unit}。")
    assert hits_count / (len(cases) * 2) >= 0.90, hits_count
