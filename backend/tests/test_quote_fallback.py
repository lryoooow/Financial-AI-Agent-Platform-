"""审计 A03：新浪返回上一交易日报价时必须降级到腾讯，不能让 /api/quote 500。

旧实现在 try/else 的 else 里调用 finish()，新鲜度校验抛出的异常没人接。
"""
import datetime as dt

from finresearch.tools import market


def _base(source, day):
    return {"code": "600519.SH", "price": 100.0, "pe": 10.0, "pb": 2.0, "marketCap": "100亿",
            "industry": "测试行业", "trading_date": str(day), "provenance": {"source": source}}


def test_stale_sina_falls_back_to_tencent(monkeypatch):
    today = market._now_cst().date()
    calls = []

    def tencent(raw):
        calls.append("tencent")
        return _base("tencent", today)

    monkeypatch.setattr(market, "_requires_today_intraday", lambda *a: True)
    monkeypatch.setattr(market.market_provider, "sina_realtime", lambda raw: _base("sina", today - dt.timedelta(days=1)))
    monkeypatch.setattr(market.market_provider, "tencent_realtime", tencent)
    q = market.get_realtime_quote("600519.SH")
    assert q["provenance"]["source"] == "tencent"
    assert calls == ["tencent"]
    attempts = q["provenance"].get("attempts") or []
    assert attempts and attempts[0]["source"] == "sina" and attempts[0]["ok"] is False


def test_all_sources_stale_returns_structured_failure(monkeypatch):
    today = market._now_cst().date()
    stale = today - dt.timedelta(days=1)
    monkeypatch.setattr(market, "_requires_today_intraday", lambda *a: True)
    monkeypatch.setattr(market.market_provider, "sina_realtime", lambda raw: _base("sina", stale))
    monkeypatch.setattr(market.market_provider, "tencent_realtime", lambda raw: _base("tencent", stale))
    monkeypatch.setattr(market, "_get_spot_row", lambda raw: None)
    q = market.get_realtime_quote("600519.SH")
    assert q["price"] == 0.0 and q["provenance"].get("error"), "全部源失效：结构化失败，绝不返回过期价格"


def test_fresh_sina_still_wins(monkeypatch):
    today = market._now_cst().date()
    monkeypatch.setattr(market, "_requires_today_intraday", lambda *a: True)
    monkeypatch.setattr(market.market_provider, "sina_realtime", lambda raw: _base("sina", today))
    monkeypatch.setattr(market.market_provider, "tencent_realtime", lambda raw: (_ for _ in ()).throw(AssertionError("不应调用")))
    assert market.get_realtime_quote("600519.SH")["provenance"]["source"] == "sina"
