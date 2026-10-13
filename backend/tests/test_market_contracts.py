import datetime as real_dt
import re
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


def _intraday_fixture(count: int, *, prev_close=None) -> dict:
    """构造一段可控的分时：价格先跌到谷底再涨到峰顶，谷/峰都不在首尾。"""
    rows = []
    for i in range(count):
        minute = 9 * 60 + 30 + i
        price = 10.0 + (0.5 if i == count - 2 else -0.5 if i == 1 else 0.0)
        rows.append(
            {
                "time": f"{minute // 60:02d}:{minute % 60:02d}",
                "price": price,
                "avg": 10.0,
                "volume": 100,
                "amount": 1000.0,
            }
        )
    return {
        "data": rows,
        "interval": "1m",
        "prev_close": prev_close,
        "trading_date": "2026-07-08",
        "provenance": {"source": "eastmoney-direct", "error": None},
    }


def test_summarize_intraday_keeps_extremes_and_caps_points():
    """降采样不能把最高/最低点抹掉——它们正是日内形态判断的关键位。"""
    from finresearch.tools import market

    summary = market.summarize_intraday(_intraday_fixture(240, prev_close=10.0), max_points=48)

    assert summary["point_count"] == 240
    assert len(summary["points"]) <= 48 + 4  # 均匀采样 + 强制保留首/尾/高/低
    assert summary["high"] == 10.5 and summary["high_time"] == "13:28"
    assert summary["low"] == 9.5 and summary["low_time"] == "09:31"
    # 极值必须出现在降采样后的走势点里，否则模型看到的形态是平的
    times = {p["time"] for p in summary["points"]}
    assert summary["high_time"] in times and summary["low_time"] in times
    assert summary["points"][0]["time"] == "09:30"
    assert summary["points"][-1]["time"] == summary["last_time"]


def test_summarize_intraday_computes_change_only_with_prev_close():
    """昨收拿不到时不许编涨跌幅——Sina/AkShare 分支的源本身不返回 preClose。"""
    from finresearch.tools import market

    with_prev = market.summarize_intraday(_intraday_fixture(10, prev_close=10.0))
    assert with_prev["change_pct"] == 0.0
    assert with_prev["amplitude_pct"] == 10.0

    without_prev = market.summarize_intraday(_intraday_fixture(10))
    assert without_prev["prev_close"] is None
    assert without_prev["change"] is None
    assert without_prev["change_pct"] is None
    assert without_prev["amplitude_pct"] is None


def test_summarize_intraday_reports_empty_as_error():
    """空分时要显式报 error，不能返回一个字段齐全但全是 0 的摘要让模型当成真数据。"""
    from finresearch.tools import market

    summary = market.summarize_intraday(
        {"data": [], "provenance": {"source": "none", "error": "分时数据暂不可用：源全挂"}}
    )
    assert summary["error"] == "分时数据暂不可用：源全挂"
    assert summary["point_count"] == 0
    assert "points" not in summary


def test_intraday_keeps_previous_trading_day_with_stale_metadata(monkeypatch):
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

    assert result["data"] == stale["data"]
    assert result["stale"] is True
    assert "2026-07-07" in result["provenance"]["stale_reason"]


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


def _ma_keys(row: dict) -> set[str]:
    """行里出现的均线字段（ma5/ma10/...），排除 macd 等同前缀指标。"""
    return {key for key in row if re.fullmatch(r"ma\d+", key)}


def _fake_daily_rows(n: int) -> list[dict]:
    """生成 n 根连续日 K（收盘价单调变化，便于校验均线是否真的成形）。"""
    base = real_dt.date(2025, 1, 1)
    return [
        {
            "date": (base + real_dt.timedelta(days=index)).isoformat(),
            "open": 10 + index * 0.01,
            "close": 10.05 + index * 0.01,
            "high": 10.2 + index * 0.01,
            "low": 9.8 + index * 0.01,
            "volume": 1000 + index,
        }
        for index in range(n)
    ]


def test_ma_periods_grow_with_range():
    from finresearch.tools.market import ma_periods_for

    assert ma_periods_for(5) == [5]
    assert ma_periods_for(10) == [5, 10]
    assert ma_periods_for(20) == [5, 10, 20]
    assert ma_periods_for(60) == [5, 10, 20, 60]
    assert ma_periods_for(120) == [5, 10, 20, 60, 120]
    assert ma_periods_for(250) == [5, 10, 20, 60, 120, 250]


def test_kline_ma_count_follows_range_and_covers_whole_window(monkeypatch):
    """区间越长均线越多；且因预取历史，可见窗口的第一根 K 线均线也已成形。"""
    from finresearch.tools import market, market_provider

    requested: dict[str, int] = {}

    def fake_tencent(raw, n=60, adjust="qfq"):
        requested["n"] = int(n)
        rows = _fake_daily_rows(int(n))
        return {"data": rows, "trading_date": rows[-1]["date"], "provenance": {"source": "tencent"}}

    monkeypatch.setattr(market_provider, "tencent_kline", fake_tencent)

    short = market.get_kline("600519.SH", n=5)["data"]
    assert len(short) == 5
    assert requested["n"] > 5  # 预取了均线预热所需的历史
    assert _ma_keys(short[0]) == {"ma5"}
    assert all(row["ma5"] is not None for row in short)

    mid = market.get_kline("600519.SH", n=20)["data"]
    assert len(mid) == 20
    assert _ma_keys(mid[0]) == {"ma5", "ma10", "ma20"}
    assert all(row["ma20"] is not None for row in mid)

    long = market.get_kline("600519.SH", n=250)["data"]
    assert len(long) == 250
    assert _ma_keys(long[0]) == {"ma5", "ma10", "ma20", "ma60", "ma120", "ma250"}
    assert all(row["ma250"] is not None for row in long)


def test_kline_warmup_returns_extra_leading_rows(monkeypatch):
    """warmup 只影响返回的前置根数，不影响均线周期集合。"""
    from finresearch.tools import market, market_provider

    def fake_tencent(raw, n=60, adjust="qfq"):
        rows = _fake_daily_rows(int(n))
        return {"data": rows, "trading_date": rows[-1]["date"], "provenance": {"source": "tencent"}}

    monkeypatch.setattr(market_provider, "tencent_kline", fake_tencent)

    data = market.get_kline("600519.SH", n=20, warmup=30)["data"]

    assert len(data) == 50
    assert _ma_keys(data[0]) == {"ma5", "ma10", "ma20"}


def test_kline_keeps_previous_trading_day_with_stale_metadata(monkeypatch):
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

    # 均线由 get_kline 统一补齐；样本不足时保持 None，OHLC 原样透传
    assert [{key: row[key] for key in ("date", "open", "close", "high", "low", "volume")} for row in result["data"]] == [
        {"date": "2026-07-07", "open": 10, "close": 11, "high": 12, "low": 9, "volume": 100}
    ]
    assert result["data"][0]["ma5"] is None
    assert result["stale"] is True
    assert "2026-07-07" in result["provenance"]["stale_reason"]


def test_realtime_quote_falls_back_to_akshare(monkeypatch):
    """直连优先重排后：Sina/Tencent 直连失败 → AkShare 兜底，attempts 链如实记录。"""
    from finresearch.cache import quote_cache
    from finresearch.tools import market, market_provider

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
    from finresearch.cache import slow_cache
    from finresearch.tools import entity

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


def test_beijing_920_prefix_is_normalized_consistently():
    from finresearch.tools import entity, market, market_provider

    assert entity.to_market_ticker("920001") == "920001.BJ"
    assert entity._classify_exchange("920001") == "北证"
    assert market._is_bj("920001") is True
    assert market_provider._sina_symbol("920001") == "bj920001"


def test_shanghai_etf_prefixes_are_routed_to_sh():
    """588200 等上交所 ETF（50/52/58 号段）曾被误判为深市，导致行情/走势图取不到数据。"""
    from finresearch.tools import entity, market_provider

    for code in ("588200", "588000", "510300", "512880", "560010", "501000"):
        assert entity.to_market_ticker(code) == f"{code}.SH", code
        assert entity._classify_exchange(code) == "上证", code
        assert market_provider._sina_symbol(code) == f"sh{code}", code
        assert market_provider._eastmoney_secid(code) == f"1.{code}", code

    # 深市基金号段不受影响
    for code in ("159915", "160119", "184801"):
        assert entity.to_market_ticker(code) == f"{code}.SZ", code
        assert market_provider._eastmoney_secid(code) == f"0.{code}", code


def test_intraday_prefers_one_minute_source_for_standard_and_920(monkeypatch):
    from finresearch.tools import market, market_provider

    one_minute = {
        "data": [{"time": "09:30", "price": 10.0, "avg": 10.0, "volume": 100}],
        "interval": "1m",
        "trading_date": "2026-07-08",
        "provenance": {"source": "eastmoney-direct"},
    }
    monkeypatch.setattr(market_provider, "eastmoney_intraday", lambda raw: one_minute)
    monkeypatch.setattr(
        market_provider,
        "sina_intraday",
        lambda raw: (_ for _ in ()).throw(AssertionError("5m fallback should not run")),
    )
    monkeypatch.setattr(market.dt, "datetime", FixedDateTime)

    assert market.get_intraday("300750.SZ")["interval"] == "1m"
    assert market.get_intraday("920001.BJ")["interval"] == "1m"


def test_indicator_series_keeps_unformed_values_null():
    from finresearch.tools.market import compute_indicators_from_kline

    data = [
        {
            "date": f"2026-06-{index:02d}",
            "open": 10 + index / 10,
            "close": 10.05 + index / 10,
            "high": 10.2 + index / 10,
            "low": 9.8 + index / 10,
            "volume": 1000 + index,
        }
        for index in range(1, 31)
    ]
    result = compute_indicators_from_kline(
        "600519.SH",
        data,
        {"trading_date": "2026-06-30", "provenance": {"source": "test"}},
    )

    assert len(result["series"]) == len(data)
    assert result["series"][0]["ma5"] is None
    assert result["series"][-1]["ma20"] is not None
    assert {item["name"] for item in result["data"]} >= {"MACD", "RSI(14)", "KDJ-J", "BOLL"}


def test_chart_bundle_fetches_kline_once(monkeypatch):
    from finresearch.tools import market

    calls = {"kline": 0}
    candles = [
        {"date": f"2026-06-{index:02d}", "open": 10, "close": 10 + index / 100, "high": 11, "low": 9, "volume": 100}
        for index in range(1, 31)
    ]

    def fake_kline(code, n=120, adjust="qfq", warmup=0):
        calls["kline"] += 1
        calls["warmup"] = warmup
        return {"data": candles, "trading_date": "2026-06-30", "provenance": {"source": "test"}}

    monkeypatch.setattr(market, "get_kline", fake_kline)
    monkeypatch.setattr(market, "get_realtime_quote", lambda code: {"code": code, "price": 10})
    monkeypatch.setattr(market, "get_intraday", lambda code: {"data": []})
    monkeypatch.setattr(market, "get_bid_ask", lambda code: {"asks": [], "bids": []})

    result = market.get_chart_bundle("600519.SH")

    assert calls["kline"] == 1
    assert calls["warmup"] > 0  # 指标预热由 bundle 统一申请，避免二次抓取
    assert len(result["indicators"]["series"]) == len(candles)


def test_chart_bundle_trims_warmup_and_keeps_short_range_indicators(monkeypatch):
    """选 5 日区间：只返回 5 根 K 线与 5 个指标点，但 MACD 等已用预热数据算成形。"""
    from finresearch.tools import market, market_provider

    def fake_tencent(raw, n=60, adjust="qfq"):
        rows = _fake_daily_rows(int(n))
        return {"data": rows, "trading_date": rows[-1]["date"], "provenance": {"source": "tencent"}}

    monkeypatch.setattr(market_provider, "tencent_kline", fake_tencent)
    monkeypatch.setattr(market, "get_realtime_quote", lambda code: {"code": code, "price": 10})
    monkeypatch.setattr(market, "get_intraday", lambda code: {"data": []})
    monkeypatch.setattr(market, "get_bid_ask", lambda code: {"asks": [], "bids": []})

    result = market.get_chart_bundle("600519.SH", n=5)
    kline = result["kline"]["data"]
    series = result["indicators"]["series"]

    assert len(kline) == 5
    assert len(series) == 5
    assert _ma_keys(kline[0]) == {"ma5"}
    assert _ma_keys(series[0]) == {"ma5"}
    assert series[0]["macd"] is not None and series[0]["rsi14"] is not None


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


def test_bid_ask_parses_akshare_item_value_layout(monkeypatch):
    """五档盘口：akshare stock_bid_ask_em 返回两列 [item, value]（价格/量为分开的行），
    量字段已 ×100 转股。解析必须产出 label/price/vol（手），且卖档自上而下 卖五→卖一。"""
    from finresearch.tools import market, market_provider

    rows = []
    for i, cn in zip(range(5, 0, -1), "五四三二一"):
        rows.append({"item": f"sell_{i}", "value": 10.0 + i})
        rows.append({"item": f"sell_{i}_vol", "value": (i + 1) * 200})  # 股
    for i, cn in zip(range(1, 6), "一二三四五"):
        rows.append({"item": f"buy_{i}", "value": 9.0 + i})
        rows.append({"item": f"buy_{i}_vol", "value": i * 300})  # 股
    rows += [{"item": "最新", "value": 10.5}, {"item": "昨收", "value": 10.2}]

    captured = {}
    fake_ak = types.SimpleNamespace()

    def fake_em(**kwargs):
        captured.update(kwargs)
        return pd.DataFrame(rows)

    fake_ak.stock_bid_ask_em = fake_em
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    # 关掉直连优先源，强制走被 mock 的 akshare 兜底
    monkeypatch.setattr(market_provider, "tencent_bid_ask", _boom)

    data = market.get_bid_ask("600519.SH")

    # stock_bid_ask_em 要求纯 6 位代码（自己拼 secid）：传 sh600519 会拼出非法 secid 被远端掐断
    assert captured.get("symbol") == "600519", captured
    assert [a["label"] for a in data["asks"]] == ["卖五", "卖四", "卖三", "卖二", "卖一"]
    assert [b["label"] for b in data["bids"]] == ["买一", "买二", "买三", "买四", "买五"]
    assert data["asks"][-1]["price"] == 11.0  # 卖一
    assert data["asks"][-1]["vol"] == 4  # sell_1_vol=400 股 → 4 手
    assert data["bids"][0]["price"] == 10.0  # 买一
    assert data["bids"][0]["vol"] == 3  # buy_1_vol=300 股 → 3 手
    assert data["provenance"]["source"] == "akshare"


def test_bid_ask_prefers_tencent_direct(monkeypatch):
    """腾讯 qt 直连是第一优先源：收盘后仍保留当日五档快照，量纲就是手。"""
    from finresearch.tools import market, market_provider

    def fake_tencent(raw):
        return {
            "asks": [{"label": "卖五", "price": 12.0, "vol": 7}, {"label": "卖一", "price": 11.0, "vol": 4}],
            "bids": [{"label": "买一", "price": 10.0, "vol": 3}],
            "trading_date": "2026-09-28",
            "provenance": {"source": "tencent", "endpoint": "qt.gtimg.cn/q", "raw_value": f"sh{raw}", "error": None},
        }

    monkeypatch.setattr(market_provider, "tencent_bid_ask", fake_tencent)

    data = market.get_bid_ask("600519.SH")

    assert data["provenance"]["source"] == "tencent"
    assert data["trading_date"] == "2026-09-28"  # 来自数据快照时间戳，非伪造
    assert data["asks"][0]["label"] == "卖五" and data["asks"][-1]["label"] == "卖一"
    assert data["bids"][0]["label"] == "买一"
    assert data["provenance"]["attempts"] == [{"source": "tencent", "ok": True}]


def test_tencent_kline_hfq_requests_hfqday_not_qfq(monkeypatch):
    """hfq 后复权不得被静默降级成前复权。

    曾经 adjust="hfq" 时向腾讯请求 fq=qfq、按 qfqday 键解析——返回的是前复权
    数据，provenance 却记录 hfq，跨除权日 K 线与全部派生指标系统性失真。
    """
    import json

    from finresearch.tools import market_provider

    captured = {}

    def fake_http_get(url, headers=None, timeout=None):
        captured["url"] = url
        return json.dumps({"code": 0, "data": {"sh600519": {
            "hfqday": [["2026-07-07", 1500.0, 1510.0, 1520.0, 1495.0, 30000]],
            "qfqday": [["2026-07-07", 15.0, 15.1, 15.2, 14.9, 30000]],
            "day": [["2026-07-07", 1490.0, 1499.0, 1505.0, 1488.0, 30000]],
        }}})

    monkeypatch.setattr(market_provider, "_http_get", fake_http_get)

    out = market_provider.tencent_kline("600519.SH", n=10, adjust="hfq")

    assert captured["url"].endswith(",hfq"), captured["url"]
    assert ",qfq" not in captured["url"]
    assert out["data"][0]["close"] == 1510.0  # 取自 hfqday，不是 qfqday 的 15.1
    assert out["provenance"]["raw_value"]["adjust"] == "hfq"

    # qfq 与不复权路径保持原行为
    out_qfq = market_provider.tencent_kline("600519.SH", n=10, adjust="qfq")
    assert out_qfq["data"][0]["close"] == 15.1
    out_raw = market_provider.tencent_kline("600519.SH", n=10, adjust="")
    assert out_raw["data"][0]["close"] == 1499.0  # day 键，不复权原始价


def test_intraday_akshare_volume_converted_to_lots(monkeypatch):
    """akshare 分时兜底的成交量单位是股，必须 /100 对齐其余路径的"手"口径。

    曾经不对齐，summarize_intraday 的量能汇总随命中源跳变 100 倍，
    LLM 基于日内量能的结论随源不可比。
    """
    from finresearch.tools import market, market_provider

    fake_ak = types.SimpleNamespace()
    fake_ak.stock_zh_a_minute = lambda **_: pd.DataFrame(
        [
          {"day": "2026-07-08 09:30:00", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 50000},
          {"day": "2026-07-08 10:25:00", "open": 11, "high": 11, "low": 11, "close": 11, "volume": 30000},
        ]
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    monkeypatch.setattr(market_provider, "sina_intraday", _boom)
    monkeypatch.setattr(market_provider, "eastmoney_intraday", _boom)
    monkeypatch.setattr(market.dt, "datetime", FixedDateTime)

    data = market.get_intraday("300750.SZ")["data"]

    assert data[0]["volume"] == 500  # 50000 股 → 500 手
    assert data[1]["volume"] == 300


def test_quote_akshare_fallback_does_not_fabricate_trading_date(monkeypatch):
    """em 全市场快照无日期列，兜底路径不得把 trading_date 伪造成今天。

    伪造会击穿 _ensure_fresh 的陈旧检测——陈旧数据被当成今日新鲜数据，
    正是"显示的涨跌幅是昨天收盘"的直接成因。
    """
    from finresearch.cache import quote_cache
    from finresearch.tools import market, market_provider

    fake_ak = types.SimpleNamespace()
    fake_ak.stock_zh_a_spot_em = lambda: pd.DataFrame([{
        "代码": "300750", "名称": "宁德时代", "最新价": 255.0, "昨收": 250.0,
        "涨跌额": 5.0, "涨跌幅": 2.0, "今开": 251.0, "最高": 256.0, "最低": 250.5,
        "成交量": 123456, "成交额": 3.1e9, "总市值": 1.1e12,
        "市盈率-动态": 22.0, "市净率": 5.0, "行业": "电池",
    }])
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    monkeypatch.setattr(market_provider, "sina_realtime", _boom)
    monkeypatch.setattr(market_provider, "tencent_realtime", _boom)
    quote_cache.clear()

    result = market.get_realtime_quote("300750.SZ")

    assert result["provenance"]["source"] == "akshare"
    assert result["price"] == 255.0
    assert result["trading_date"] == ""
    assert result["provenance"]["trading_date"] == ""


def test_index_identity_never_confused_with_same_code_stock():
    """指数身份契约：000001.SH 是上证指数，裸 000001 仍是平安银行。

    曾经指数无目录：上证指数会被标成 sz000001——数据源返回的是平安银行行情，
    名称/价格全部张冠李戴且无任何提示。
    """
    from finresearch.tools.entity import (
        INDEX_CATALOG, is_index, normalize_ticker, resolve_entity, to_market_ticker,
    )

    assert is_index("000001.SH") and is_index("sh000001") and is_index("399001.SZ")
    assert not is_index("000001") and not is_index("600519.SH")
    assert normalize_ticker("000001.SH") == "000001.SH"      # 指数保留完整形式
    assert to_market_ticker("000001.SH") == "000001.SH"
    assert to_market_ticker("600519") == "600519.SH"

    r = resolve_entity("上证指数", 5)
    assert r["results"] and r["results"][0]["code"] == "000001.SH"
    assert resolve_entity("大盘", 5)["results"][0]["code"] == "000001.SH"
    # 裸 000001 不因目录误变指数：仍是股票解析路径
    bare = resolve_entity("000001", 5)
    assert bare["results"] and bare["results"][0]["code"] == "000001.SZ"
    assert len(INDEX_CATALOG) == 5


def test_tencent_kline_forces_no_adjust_for_index(monkeypatch):
    """指数强制不复权：qfq 请求对指数返回 0 行 qfqday，数据在 day 键里。"""
    import json

    from finresearch.tools import market_provider

    captured = {}

    def fake_http_get(url, headers=None, timeout=None):
        captured["url"] = url
        return json.dumps({"code": 0, "data": {"sh000001": {
            "day": [["2026-09-28", 3800.0, 3823.6, 3830.0, 3790.0, 200000000]],
            "qfqday": [],
        }}})

    monkeypatch.setattr(market_provider, "_http_get", fake_http_get)
    out = market_provider.tencent_kline("000001.SH", n=10, adjust="qfq")
    assert captured["url"].endswith(",day,,,10,")  # fq 参数为空
    assert out["data"][0]["close"] == 3823.6


def test_bid_ask_intercepts_index_without_stock_fallback():
    """指数盘口必须拦截：akshare 兜底会把 000001 查成平安银行的五档。"""
    from finresearch.tools import market

    b = market.get_bid_ask("000001.SH")  # 无网络路径：指数分支在最前
    assert b["asks"] == [] and b["bids"] == []
    assert "指数" in (b["provenance"].get("error") or "")
