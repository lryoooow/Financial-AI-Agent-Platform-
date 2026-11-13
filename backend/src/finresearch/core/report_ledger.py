"""Immutable report facts and a time-aligned comparison with the latest observations."""

import asyncio
import datetime as dt
import re

from ..agents._utils import _timed_call
from ..tools import clean_for_json, now_cst, now_iso
from ..tools.batch_radar import valuation_snapshot
from ..tools.entity import stock_ticker
from ..tools.facts import financial_yoy, quote_usable, to_float
from ..tools.fundamentals import get_financial_indicators
from ..tools.market import get_kline, get_realtime_quote
from ..tools.market_breadth import index_quote
from ..tools.market_provider import tencent_kline
from ..tools.risk_metrics import price_series


def _stamp(quote: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(quote.get("as_of") or ""))
    except ValueError:
        return None


def _finance(financials: dict) -> dict:
    return {
        "fin_period": (financials.get("periods") or [""])[-1] or (financials.get("years") or [""])[-1] or None,
        "revenue_yoy": financial_yoy(financials, "营收同比", "营业收入"),
        "profit_yoy": financial_yoy(financials, "净利润同比", "净利润"),
    }


def _period_order(value: str | None) -> str:
    if not value:
        return ""
    match = re.fullmatch(r"(\d{4})(年报|中报|一季报|三季报)?", value)
    if match:
        month_day = {None: "12-31", "年报": "12-31", "中报": "06-30", "一季报": "03-31", "三季报": "09-30"}
        return match[1] + "-" + month_day[match[2]]
    return value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else ""


def _quote_fields(quote: dict) -> dict:
    usable = quote_usable(quote)
    return {
        "report_price": to_float(quote.get("price")) if usable else None,
        "report_pe_ttm": None,
        "report_pb": (to_float(quote.get("pb")) or None) if usable else None,
        "price_as_of": quote.get("as_of") if usable else None,
        "hs300_close": None,
        "hs300_as_of": None,
    }


def _same_session(left: dt.datetime | None, right: dt.datetime | None) -> bool:
    if left is None or right is None or left.date() != right.date():
        return False
    # Quote providers report local exchange time without offsets.
    return abs((left.replace(tzinfo=None) - right.replace(tzinfo=None)).total_seconds()) <= 300


async def _benchmark_close(day: str) -> dict:
    import akshare as ak

    def read():
        compact = day.replace("-", "")
        data = ak.stock_zh_index_daily_em(symbol="sh000300", start_date=compact, end_date=compact)
        exact = data[data["date"].astype(str).str[:10] == day] if not data.empty else data
        return {"price": to_float(exact.iloc[-1]["close"]), "as_of": day} if not exact.empty else {}

    value, _, _ = await _timed_call(read, timeout=5)
    return value or {}


def _within_days(left_iso: str | None, right_date: dt.date | None, max_days: int = 10) -> bool:
    if not left_iso or right_date is None:
        return False
    try:
        left_date = dt.date.fromisoformat(str(left_iso)[:10])
    except ValueError:
        return False
    return abs((right_date - left_date).days) <= max_days


async def capture_report_snapshot(symbol: str, doc: dict) -> dict:
    quote = doc.get("quote") or {}
    snapshot = {
        **_quote_fields(quote), **_finance(doc.get("financials") or {}),
        "snapshot_ts": now_iso(), "provenance": {"quote": quote.get("provenance")},
    }
    stamp = _stamp(quote)
    if stamp is None or not quote_usable(quote) or not quote.get("trading_date"):
        return clean_for_json(snapshot)
    try:
        symbol = stock_ticker(symbol)
    except ValueError:
        return clean_for_json(snapshot)
    raw_as_of = str(quote.get("as_of") or "").strip()
    is_close_stamp = len(raw_as_of) == 10 or stamp.hour >= 15

    async def benchmark():
        return await _benchmark_close(stamp.date().isoformat()) if is_close_stamp else {}

    (valuation, _, _), close = await asyncio.gather(_timed_call(valuation_snapshot, symbol, timeout=5), benchmark())
    if valuation and _within_days(valuation.get("as_of"), stamp.date(), max_days=10):
        snapshot["report_pe_ttm"] = valuation.get("pe_ttm")
        snapshot["valuation_as_of"] = valuation.get("as_of")
        snapshot["provenance"]["valuation"] = valuation.get("provenance")
    if close.get("price"):
        snapshot["hs300_close"] = close["price"]
        snapshot["hs300_as_of"] = close["as_of"]
        snapshot["provenance"]["benchmark"] = {"source": "eastmoney", "endpoint": "stock_zh_index_daily_em"}
    snapshot["snapshot_ts"] = now_iso()
    return clean_for_json(snapshot)


def _growth(old: float | None, new: float | None) -> float | None:
    return round((new / old - 1) * 100, 3) if old is not None and old > 0 and new is not None else None


async def read_report_ledger(symbol: str, stored: dict | None, doc: dict) -> dict:
    old = stored or {
        **_quote_fields(doc.get("quote") or {}), **_finance(doc.get("financials") or {}),
        "snapshot_ts": None, "provenance": {"quote": (doc.get("quote") or {}).get("provenance")},
    }
    try:
        code = stock_ticker(symbol)
    except ValueError:
        return {"snapshot": old, "legacy": not stored, "current": {}, "comparison": {},
                "message": "此报告没有可回溯的单一股票标的。"}

    try:
        first_day = dt.date.fromisoformat(str(old.get("price_as_of") or "")[:10])
    except ValueError:
        first_day = None
    today_cst = now_cst().date()
    sample_days = min(1000, max(90, (today_cst - first_day).days + 10)) if first_day else 90
    observations = await asyncio.gather(
        _timed_call(get_realtime_quote, code, timeout=8),
        _timed_call(get_financial_indicators, code, timeout=12),
        _timed_call(index_quote, "000300.SH", "沪深300", timeout=8),
        _timed_call(valuation_snapshot, code, timeout=9),
        _timed_call(get_kline, code, sample_days, "qfq", timeout=8),
        _timed_call(tencent_kline, "000300.SH", sample_days, "", index=True, timeout=8),
    )
    quote, finance, index, valuation, bars, index_bars = [value or {} for value, _, _ in observations]
    current = {
        **_quote_fields(quote), **_finance(finance.get("financials") or {}),
        "hs300_as_of": index.get("as_of"), "hs300_close": to_float(index.get("price")) if quote_usable(index) else None,
        "provenance": {
            "quote": quote.get("provenance"), "financial": finance.get("provenance"),
            "benchmark": index.get("provenance"), "valuation": valuation.get("provenance"),
            "drawdown": bars.get("provenance"),
        },
    }
    quote_day = None
    try:
        if quote.get("trading_date"):
            quote_day = dt.date.fromisoformat(str(quote.get("trading_date"))[:10])
    except ValueError:
        quote_day = None
    if valuation and quote_day and _within_days(valuation.get("as_of"), quote_day, max_days=10):
        current["report_pe_ttm"] = valuation.get("pe_ttm")
        current["valuation_as_of"] = valuation.get("as_of")
    original_period, latest_period = _period_order(old.get("fin_period")), _period_order(current["fin_period"])
    comparison = {
        "price_change_pct": _growth(old.get("report_price"), current["report_price"]),
        "pe_change_pct": _growth(old.get("report_pe_ttm"), current["report_pe_ttm"]),
        "pb_change_pct": _growth(old.get("report_pb"), current["report_pb"]),
        "new_financial_period": bool(original_period and latest_period and latest_period > original_period),
        "excess_pp": None, "max_drawdown_pct": None,
    }
    asset = price_series(bars.get("data") or []) if bars.get("adjust", "qfq") == "qfq" else {}
    reference = price_series(index_bars.get("data") or [])
    start = first_day.isoformat() if first_day else ""
    dates = [day for day in asset if day >= start] if start else []
    if dates and first_day and _within_days(dates[0], first_day, max_days=10):
        anchor = dates[0]
        peak = asset[anchor]
        drawdown = 0.0
        for day in dates:
            peak = max(peak, asset[day])
            drawdown = min(drawdown, (asset[day] / peak - 1) * 100)
        comparison["max_drawdown_pct"] = round(drawdown, 3)
        comparison["drawdown_as_of"] = dates[-1]
        common = [day for day in dates if day in reference]
        if len(common) > 1 and common[0] == anchor:
            end = common[-1]
            comparison["excess_pp"] = round((asset[end] / asset[anchor] - reference[end] / reference[anchor]) * 100, 3)
            comparison["return_start"] = anchor
            comparison["return_end"] = end
    # Expose the benchmark quote timestamp independently, even when an intraday excess comparison is unavailable.
    current["same_session"] = _same_session(_stamp(quote), _stamp(index))
    return clean_for_json({"snapshot": old, "current": current, "comparison": comparison, "legacy": not stored})
