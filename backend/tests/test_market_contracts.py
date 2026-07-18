import datetime as real_dt
import sys
import types
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class FixedDateTime(real_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 8, 10, 25, 0, tzinfo=tz)


class AfterCloseDateTime(real_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 8, 15, 30, 0, tzinfo=tz)


def _boom(*a, **k):
    """把直连源（sina/tencent/eastmoney）在测试里关掉，强制走被 mock 的 akshare 兜底。

    直连优先重排后，get_intraday/get_kline 会先试直连源；测试只 mock 了 akshare，
    若不关掉直连源，联网机器上会返回真实数据导致 fixture 断言失败。
    """
    raise RuntimeError("mocked direct source off")


def test_intraday_does_not_include_future_points(monkeypatch):
    from finresearch.tools import market, market_provider

    fake_ak = types.SimpleNamespace()
    fake_ak.stock_zh_a_minute = lambda **_: pd.DataFrame(
        [
          {"day": "2026-07-08 09:30:00", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 100},
          {"day": "2026-07-08 10:25:00", "open": 11, "high": 11, "low": 11, "close": 11, "volume": 100},
          {"day": "2026-07-08 13:00:00", "open": 12, "high": 12, "low": 12, "close": 12, "volume": 100},
        ]
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    # 直连优先源全部 mock 失败 → akshare（已 mock 成功）兜底，测试不依赖真实网络
    monkeypatch.setattr(market_provider, "sina_intraday", _boom)
    monkeypatch.setattr(market_provider, "eastmoney_intraday", _boom)
    monkeypatch.setattr(market.dt, "datetime", FixedDateTime)

    data = market.get_intraday("300750.SZ")["data"]

    assert data
    assert data[-1]["time"] <= "10:25"
    assert all(p["time"] < "13:00" for p in data)


def test_intraday_rejects_previous_trading_day_after_open(monkeypatch):
    from finresearch.tools import market, market_provider

    fake_ak = types.SimpleNamespace()
    fake_ak.stock_zh_a_minute = lambda **_: (_ for _ in ()).throw(RuntimeError("ak down"))
    stale = {
        "data": [{"time": "14:55", "price": 10.0, "avg": 10.0, "volume": 100}],
        "trading_date": "2026-07-07",
        "provenance": {"source": "test"},
    }
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    monkeypatch.setattr(market_provider, "sina_intraday", lambda raw: stale)
    monkeypatch.setattr(market_provider, "eastmoney_intraday", lambda raw: stale)
    monkeypatch.setattr(market.dt, "datetime", FixedDateTime)

    result = market.get_intraday("300750.SZ")

    assert result["data"] == []
    assert result["provenance"]["source"] == "none"
    assert any("2026-07-07" in a.get("error", "") for a in result["provenance"]["attempts"])


def test_kline_uses_full_iso_date(monkeypatch):
    from finresearch.tools import market, market_provider

    fake_ak = types.SimpleNamespace()
    fake_ak.stock_zh_a_hist = lambda **_: pd.DataFrame(
        [
          {"日期": "2026-07-06", "开盘": 10, "收盘": 11, "最高": 12, "最低": 9, "成交量": 100},
          {"日期": "2026-07-07", "开盘": 11, "收盘": 12, "最高": 13, "最低": 10, "成交量": 110},
          {"日期": "2026-07-08", "开盘": 12, "收盘": 13, "最高": 14, "最低": 11, "成交量": 120},
        ]
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    # 直连优先源全部 mock 失败 → akshare 兜底
    monkeypatch.setattr(market_provider, "tencent_kline", _boom)
    monkeypatch.setattr(market_provider, "eastmoney_kline", _boom)
    monkeypatch.setattr(market_provider, "sina_kline", _boom)
    monkeypatch.setattr(market.dt, "datetime", FixedDateTime)

    data = market.get_kline("300750.SZ", n=3)["data"]

    assert [p["date"] for p in data] == ["2026-07-06", "2026-07-07", "2026-07-08"]


def test_kline_rejects_previous_trading_day_after_close(monkeypatch):
    from finresearch.tools import market, market_provider

    fake_ak = types.SimpleNamespace()
    fake_ak.stock_zh_a_hist = lambda **_: (_ for _ in ()).throw(RuntimeError("ak down"))
    stale = {
        "data": [{"date": "2026-07-07", "open": 10, "close": 11, "high": 12, "low": 9, "volume": 100}],
        "trading_date": "2026-07-07",
        "provenance": {"source": "test"},
    }
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    monkeypatch.setattr(market_provider, "tencent_kline", lambda raw, n=60, adjust="qfq": stale)
    monkeypatch.setattr(market_provider, "eastmoney_kline", lambda raw, n=60, adjust="qfq": stale)
    monkeypatch.setattr(market_provider, "sina_kline", lambda raw, n=60: stale)
    monkeypatch.setattr(market.dt, "datetime", AfterCloseDateTime)

    result = market.get_kline("300750.SZ", n=3)

    assert result["data"] == []
    assert result["provenance"]["source"] == "none"
    assert any("2026-07-07" in a.get("error", "") for a in result["provenance"]["attempts"])


def test_realtime_quote_falls_back_to_akshare(monkeypatch):
    """直连优先重排后：Sina/Tencent 直连失败 → AkShare 兜底，attempts 链如实记录。"""
    from finresearch.tools import market, market_provider
    from finresearch.cache import quote_cache

    fake_ak = types.SimpleNamespace()
    fake_ak.stock_zh_a_spot_em = lambda: pd.DataFrame([{
        "代码": "600519", "名称": "贵州茅台", "最新价": 1689.0, "昨收": 1680.0,
        "涨跌额": 9.0, "涨跌幅": 0.54, "今开": 1685.0, "最高": 1695.0, "最低": 1682.0,
        "成交量": 12345.0, "成交额": 2.0e9, "总市值": 2.1e12, "市盈率-动态": 25.0,
        "市净率": 8.0, "行业": "白酒",
    }])
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    monkeypatch.setattr(market_provider, "sina_realtime", _boom)
    monkeypatch.setattr(market_provider, "tencent_realtime", _boom)
    quote_cache.clear()

    q = market.get_realtime_quote("600519.SH")

    assert q["price"] == 1689.0
    assert q["provenance"]["source"] == "akshare"
    attempts = q["provenance"]["attempts"]
    assert any(a["source"] == "sina" and not a["ok"] for a in attempts)
    assert any(a["source"] == "tencent" and not a["ok"] for a in attempts)
    assert attempts[-1]["source"] == "akshare" and attempts[-1]["ok"] is True


def test_fallback_summaries_are_template_specific_and_do_not_cross_contaminate():
    from finresearch.agents.workers.report import _compose_summary_block

    # 1.2.1：_compose_summary_block(state, fv)，fv 为 facts.normalize_findings 的稳定形状
    fv = {
        "quote": {
            "name": "宁德时代",
            "industry": "电池",
            "price": 268.9,
            "pe": 18.2,
            "pb": 4.1,
            "changePct": -1.59,
            "provenance": {"source": "eastmoney-direct"},
        },
        "profile": {},
        "fins": {
            "rows": [
                {"name": "营业收入", "values": ["100.0亿", "120.0亿"]},
                {"name": "净利润", "values": ["10.0亿", "9.0亿"]},
                {"name": "ROE", "values": ["15.0%", "14.0%"]},
            ]
        },
        "indicators": [{"name": "MACD", "value": "-0.2", "signal": "死叉"}],
    }

    summaries = {
        template: _compose_summary_block({"template": template}, fv)
        for template in ("equity_deep", "industry", "earnings", "risk")
    }

    assert len(set(summaries.values())) == 4
    assert summaries["equity_deep"].startswith("个股深度摘要")
    assert summaries["industry"].startswith("行业研究摘要")
    assert summaries["earnings"].startswith("财报点评摘要")
    assert summaries["risk"].startswith("风险评估摘要")
    assert "乐观情形" not in summaries["equity_deep"]
    assert "技术状态" not in summaries["industry"]
    assert "股价走势" in summaries["earnings"]
    assert "风险情景" in summaries["risk"]
    assert "268.90 元" in summaries["equity_deep"] and "18.2 倍" in summaries["equity_deep"]
    assert "盈利端承压" in summaries["risk"]
    assert all("不构成投资建议" in value for value in summaries.values())


def test_resolve_accepts_ticker_with_exchange_suffix(monkeypatch):
    from finresearch.tools import entity
    from finresearch.cache import slow_cache

    fake_ak = types.SimpleNamespace()
    fake_ak.stock_info_a_code_name = lambda: pd.DataFrame(
        [{"code": "300750", "name": "宁德时代"}]
    )
    fake_ak.stock_zh_a_spot_em = lambda: (_ for _ in ()).throw(AssertionError("resolve must not fetch full spot snapshot"))
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    slow_cache.clear()

    results = entity.resolve_entity("300750.SZ")["results"]

    assert results[0]["code"] == "300750.SZ"
    assert results[0]["name"] == "宁德时代"
    assert results[0]["price"] == 0.0


def test_market_node_reuses_kline_for_indicators(monkeypatch):
    import asyncio
    from finresearch.agents.workers import market as worker

    calls = {"kline": 0}

    def fake_quote(code):
        return {"price": 10, "provenance": {"source": "test"}}

    def fake_kline(code, n=120, adjust="qfq"):
        calls["kline"] += 1
        data = [
            {"date": f"2026-06-{i:02d}", "open": 10, "close": 10 + i / 100, "high": 11, "low": 9, "volume": 100, "ma5": 10, "ma10": 10, "ma20": 10}
            for i in range(1, 31)
        ]
        return {"data": data, "provenance": {"source": "test"}, "trading_date": "2026-06-30"}

    def fake_compute(code, data, kline_result):
        assert len(data) == 30
        return {"data": [{"name": "MA5", "value": "10.00", "signal": "多头"}], "provenance": {"source": "test"}}

    monkeypatch.setattr(worker, "get_realtime_quote", fake_quote)
    monkeypatch.setattr(worker, "get_kline", fake_kline)
    monkeypatch.setattr(worker, "compute_indicators_from_kline", fake_compute)

    result = asyncio.run(worker.market_node({"normalized_ticker": "600519"}))

    assert calls["kline"] == 1
    assert result["indicators"][0]["name"] == "MA5"
    assert "market_ms" in result["timings"]


def test_unconfigured_serper_is_recorded(monkeypatch):
    from finresearch.config import settings
    from finresearch.tools import market_provider
    from finresearch.tools.search import web_search

    monkeypatch.setattr(settings, "enable_serper_search", True)
    monkeypatch.setattr(settings, "serper_api_key", None)
    market_provider.clear_diagnostics()

    result = web_search("贵州茅台 最新动态")
    recent = market_provider.recent_diagnostics()

    assert result["items"] == []
    assert result["provenance"]["error"] == "serper_unconfigured"
    assert recent[-1]["kind"] == "search"
    assert recent[-1]["source"] == "serper"
    assert recent[-1]["ok"] is False


def test_serper_disabled_by_default_domestic_first(monkeypatch):
    from finresearch.config import settings
    from finresearch.tools import market_provider
    from finresearch.tools.search import web_search

    monkeypatch.setattr(settings, "enable_serper_search", False)
    monkeypatch.setattr(settings, "serper_api_key", "fake-key")
    market_provider.clear_diagnostics()

    result = web_search("宁德时代 最新动态")
    recent = market_provider.recent_diagnostics()

    assert result["items"] == []
    assert result["provenance"]["error"] == "serper_disabled_domestic_first"
    assert recent[-1]["ok"] is False


def test_news_success_is_recorded(monkeypatch):
    from finresearch.cache import slow_cache
    from finresearch.tools import market_provider
    from finresearch.tools import news as news_module
    from finresearch.tools.news import search_news

    fake_ak = types.SimpleNamespace()
    fake_ak.stock_news_em = lambda symbol: pd.DataFrame(
        [
            {
                "新闻标题": "宁德时代发布新进展",
                "新闻内容": "内容",
                "发布时间": "2026-07-08",
                "新闻链接": "https://example.com/news",
                "文章来源": "东方财富",
            }
        ]
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    monkeypatch.setattr(news_module, "_news_direct", lambda _raw, _limit: [])
    slow_cache.clear()
    market_provider.clear_diagnostics()

    result = search_news("300750.SZ")
    recent = market_provider.recent_diagnostics()

    assert result["items"][0]["title"] == "宁德时代发布新进展"
    assert recent[-1]["kind"] == "news"
    assert recent[-1]["source"] == "akshare"
    assert recent[-1]["ok"] is True


def test_diagnostics_news_probe_reports_failure(monkeypatch):
    from finresearch.tools.diagnostics import get_market_diagnostics

    fake_ak = types.SimpleNamespace()
    fake_ak.stock_individual_info_em = lambda symbol: pd.DataFrame([{"item": "name", "value": "贵州茅台"}])

    def boom(symbol):
        raise RuntimeError("news down")

    fake_ak.stock_news_em = boom
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    diag = get_market_diagnostics()

    assert diag["news"]["available"] is False
    assert "news down" in diag["news"]["error"]
