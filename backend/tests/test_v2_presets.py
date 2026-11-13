"""Deterministic flow, evidence and subprocess regressions."""

import asyncio
import sys

import pytest

from finresearch.engine.flows import PRESET_FLOWS
from finresearch.engine.orchestrator import prepare_research
from finresearch.engine.router import ambiguous_query, choose_route
from finresearch.mcp.client import StdioMCPClient
from finresearch.tools.citation_verify import _public_target, verify_citation
from finresearch.tools.market_provider import _index_symbol
from finresearch.tools.risk_metrics import metrics_from_bars


def test_research_name_and_negative_scope():
    code, name, ambiguous = asyncio.run(prepare_research("贵州茅台", "equity_deep"))
    assert (code, name, ambiguous) == ("600519.SH", "贵州茅台", [])
    assert {item["code"] for item in ambiguous_query("帮我查平安的行情")} >= {"601318.SH", "000001.SZ"}
    route = choose_route("不用查新闻，直接看近三年 ROE")
    assert route.mode == "main" and "search_news" in route.blocked_tools and "web_search" in route.blocked_tools
    assert "get_financial_indicators" not in route.blocked_tools
    assert choose_route("做个同业对比").flow in PRESET_FLOWS
    assert _index_symbol("000001.SH") == "sh000001"


def test_risk_uses_aligned_daily_prices():
    bars = [
        {"date": f"2026-05-{i:02}", "close": close}
        for i, close in enumerate([100, 110, 99, 105, 115], 1)
    ]
    metrics = metrics_from_bars(bars, bars, 20)
    assert metrics["beta"] == pytest.approx(1)
    assert metrics["max_drawdown"] == pytest.approx(-0.1)
    assert metrics["drawdown_from"] == "2026-05-02"
    assert metrics["drawdown_to"] == "2026-05-03"
    assert metrics["cvar_95"] >= metrics["var_95"] > 0
    assert metrics_from_bars(bars, [], 20)["beta"] is None


def test_citation_anchors_and_rejects_loopback():
    source = "摘要显示：公司公告披露的现金流持续改善。"
    quote = "公司公告披露的现金流持续改善"
    result = verify_citation(quote, source)
    assert result["verified"] and source[result["start"]:result["end"]] == quote
    assert not verify_citation("完全不同的结论", source)["verified"]
    with pytest.raises(ValueError, match="非公网"):
        _public_target("http://127.0.0.1/")


def test_mcp_drains_more_than_a_pipe_of_stderr_before_replying():
    fixture = (
        "import json,sys\n"
        "sys.stderr.buffer.write(b'x'*131072);sys.stderr.buffer.flush()\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " if 'id' in r:\n"
        "  value={'protocolVersion':'2024-11-05'} if r['id']==1 else {'content':[{'type':'text','text':'fixture'}]}\n"
        "  print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':value}),flush=True)\n"
    )

    async def run():
        client = StdioMCPClient([sys.executable, "-u", "-c", fixture])
        return await client.call_tool("fixture", {}, timeout=3)

    assert asyncio.run(run())["content"][0]["text"] == "fixture"
