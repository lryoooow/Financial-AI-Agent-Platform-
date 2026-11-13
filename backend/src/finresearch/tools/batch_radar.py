"""Five deterministic observations for each validated watchlist symbol."""

import asyncio
import datetime as dt
import time

import pandas as pd

from ..agents._utils import _timed_call
from ..cache import slow_cache
from . import clean_for_json, make_provenance, now_cst
from .entity import stock_ticker
from .facts import financial_yoy, latest_is_interim, row_vals, to_float
from .fundamentals import get_financial_indicators
from .market import compute_indicators_from_kline, get_kline, get_realtime_quote
from .market_provider import tencent_kline
from .news import search_news
from .risk_metrics import metrics_from_bars

_SLOTS = asyncio.Semaphore(4)
_VALUATION_FAIL_AT = 0.0
_VALUATION_BREAKER_S = 300.0


def valuation_snapshot(code: str) -> dict:
    global _VALUATION_FAIL_AT
    import akshare as ak

    key = f"radar-value:{code}"
    if (cached := slow_cache.get(key)) is not None:
        return cached
    if time.time() - _VALUATION_FAIL_AT < _VALUATION_BREAKER_S:
        return {}
    try:
        frame = ak.stock_value_em(symbol=code.split(".")[0])
    except Exception:
        _VALUATION_FAIL_AT = time.time()
        return {}
    if frame.empty:
        return {}
    dates = pd.to_datetime(frame["数据日期"], errors="coerce")
    cutoff = pd.Timestamp(now_cst().date()) - pd.DateOffset(years=3)
    frame = frame.loc[dates >= cutoff].sort_values("数据日期")
    if frame.empty:
        return {}
    result = {"as_of": str(frame.iloc[-1]["数据日期"]), "provenance": make_provenance(
        source="eastmoney", endpoint="stock_value_em",
    )}
    for column, key_name in (("PE(TTM)", "pe_ttm"), ("市净率", "pb")):
        values = pd.to_numeric(frame[column], errors="coerce")
        point = values.iloc[-1]
        positive = values[values > 0]
        result[key_name] = float(point) if pd.notna(point) and point != 0 else None
        result[f"{key_name}_percentile"] = round(float((positive <= point).mean()) * 100, 2) if point > 0 and len(positive) >= 20 else None
    slow_cache.set(key, result, ttl=3600)
    return result


def _cashflow(code: str, period: str) -> dict:
    import akshare as ak

    raw, market = code.split(".")
    key = f"radar-cashflow:{code}:{period}"
    if (cached := slow_cache.get(key)) is not None:
        return cached
    frame = ak.stock_financial_report_sina(stock=market.lower() + raw, symbol="现金流量表")
    for date_column in ("报告日", "报告日期", "报告期"):
        if date_column in frame:
            dates = pd.to_datetime(frame[date_column].astype(str), errors="coerce").dt.strftime("%Y-%m-%d")
            frame = frame.loc[dates == period]
            break
    else:
        return {}
    if frame.empty:
        return {}
    for column in ("经营活动产生的现金流量净额", "经营活动产生的现金流量净额(元)", "经营活动现金流量净额"):
        if column in frame:
            value = to_float(frame.iloc[0][column])
            if value is not None:
                result = {"value": value, "provenance": make_provenance(
                    source="sina", endpoint="stock_financial_report_sina/现金流量表", raw_value=period,
                )}
                slow_cache.set(key, result, ttl=3600)
                return result
    return {}


def _latest(fins: dict, *names: str):
    values = row_vals(fins, names)
    return to_float(values[-1]) if values else None


def _within_hours(value: str, now: dt.datetime, hours: int = 72) -> bool:
    try:
        timestamp = dt.datetime.fromisoformat(value.replace("/", "-"))
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)
        return now - dt.timedelta(hours=hours) <= timestamp <= now
    except (ValueError, AttributeError):
        return False


async def _omitted() -> tuple[None, int, str]:
    return None, 0, "user_scope"


async def _scan(code: str, benchmark: dict, blocked: set[str]) -> dict:
    async with _SLOTS:
        results = await asyncio.gather(
            _timed_call(get_realtime_quote, code, timeout=9),
            _omitted() if "get_financial_indicators" in blocked else _timed_call(get_financial_indicators, code, timeout=15),
            _omitted() if "get_kline" in blocked else _timed_call(get_kline, code, 100, "qfq", timeout=9),
            _omitted() if "search_news" in blocked else _timed_call(search_news, code, limit=20, timeout=7),
            _timed_call(valuation_snapshot, code, timeout=9),
        )
        quote, financials, kline, news, valuation = [(result or {}) for result, _, _ in results]
        if not valuation and quote.get("price"):
            q_pe = quote.get("pe")
            q_pb = quote.get("pb")
            if (isinstance(q_pe, (int, float)) and q_pe != 0) or (isinstance(q_pb, (int, float)) and q_pb > 0):
                valuation = {
                    "as_of": quote.get("trading_date") or quote.get("as_of"),
                    "pe_ttm": float(q_pe) if isinstance(q_pe, (int, float)) and q_pe != 0 else None,
                    "pb": float(q_pb) if isinstance(q_pb, (int, float)) and q_pb > 0 else None,
                    "pe_ttm_percentile": None,
                    "pb_percentile": None,
                    "provenance": quote.get("provenance"),
                }
        fins = financials.get("financials") or financials
        risk = metrics_from_bars(kline.get("data") or [], benchmark.get("data") or [], 60) if kline.get("adjust", "qfq") == "qfq" else {}
        if risk.get("samples", 0) < 60:
            risk["max_drawdown"] = None
        bars = kline.get("data") or []
        latest = bars[-1] if bars else {}
        prev_volumes = [point.get("volume") for point in bars[-6:-1]]
        ratio = None
        if len(prev_volumes) == 5 and all(isinstance(value, (int, float)) and value > 0 for value in prev_volumes):
            if isinstance(latest.get("volume"), (int, float)) and latest["volume"] >= 0:
                ratio = round(latest["volume"] / (sum(prev_volumes) / 5), 3)
        period = (fins.get("periods") or [""])[-1]
        indicators_job = _timed_call(compute_indicators_from_kline, code, bars, kline, timeout=6)
        cashflow_job = _timed_call(_cashflow, code, period, timeout=6) if period else _omitted()
        (indicators, _, _), (cashflow, _, _) = await asyncio.gather(indicators_job, cashflow_job)
        cashflow = cashflow or {}
        indicator_rows = (indicators or {}).get("data", [])
        rsi_row = next((row for row in indicator_rows if "RSI" in row.get("name", "")), {})
        rsi = to_float(rsi_row.get("value"))
        now = now_cst().replace(tzinfo=None)
        items = [item for item in news.get("items", []) if _within_hours(item.get("time") or "", now)]
        groups = []
        if items:
            groups.append("财报/快讯更新")
        if ratio is not None and ratio >= 1.5 and abs(quote.get("changePct") or 0) >= 3:
            groups.append("放量大幅波动")
        if rsi is not None and (rsi >= 70 or rsi <= 30):
            groups.append("指标超买超卖")
        if not groups:
            groups.append("平稳运行" if ratio is not None and rsi is not None else "数据不足")
        return clean_for_json({
            "symbol": code, "name": quote.get("name") or code, "quote": quote,
            "groups": groups, "valuation": valuation,
            "earnings": {
                "period": (fins.get("years") or [""])[-1], "interim": latest_is_interim(fins),
                "revenue_yoy": financial_yoy(fins, "营收同比", "营业收入"),
                "profit_yoy": financial_yoy(fins, "净利润同比", "净利润"),
                "roe": _latest(fins, "ROE", "净资产收益率"),
                "operating_cashflow": cashflow.get("value"),
                "cashflow_provenance": cashflow.get("provenance"),
                "provenance": financials.get("provenance"),
            },
            "price_volume": {
                "close": latest.get("close"), "ma20": latest.get("ma20"), "volume_ratio": ratio,
                "rsi": rsi, "as_of": latest.get("date"), "provenance": kline.get("provenance"),
            },
            "risk": {**risk, "provenance": {"asset": kline.get("provenance"), "benchmark": benchmark.get("provenance")}},
            "events": {"hours": 72, "count": len(items) if news and not (news.get("provenance") or {}).get("error") else None,
                       "items": items, "provenance": news.get("provenance")},
        })


async def batch_radar(symbols: list[str], *, blocked: set[str] | None = None) -> dict:
    codes = list(dict.fromkeys(stock_ticker(symbol) for symbol in symbols))
    if len(codes) > 50:
        raise ValueError("每次最多体检 50 只标的。")
    if not codes:
        return {"rows": []}
    blocked = blocked or set()
    benchmark, _, err = await (
        _omitted() if "get_kline" in blocked else _timed_call(tencent_kline, "000300.SH", 100, "", index=True, timeout=7)
    )
    if benchmark is None:
        benchmark = {"provenance": make_provenance(source="tencent", endpoint="index-kline", error=err)}
    rows = await asyncio.gather(*(_scan(code, benchmark, blocked) for code in codes))
    return {"rows": rows}
