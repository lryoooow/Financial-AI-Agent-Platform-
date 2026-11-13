"""Deterministic downside and benchmark-relative metrics from daily bars."""

import math

import numpy as np

from . import clean_for_json, make_provenance
from .entity import detect_index_code, stock_ticker
from .market import get_kline
from .market_provider import tencent_kline


def price_series(bars: list[dict]) -> dict[str, float]:
    prices = {}
    for bar in bars:
        value = bar.get("close")
        if isinstance(value, (int, float)) and value > 0 and math.isfinite(value) and bar.get("date"):
            prices[str(bar["date"])[:10]] = float(value)
    return dict(sorted(prices.items()))


def _daily_returns_with_prev(prices: dict[str, float]) -> dict[str, tuple[float, str]]:
    ordered = list(prices.items())
    out: dict[str, tuple[float, str]] = {}
    for idx in range(1, len(ordered)):
        prev_day, prev_close = ordered[idx - 1]
        cur_day, cur_close = ordered[idx]
        if prev_close > 0:
            out[cur_day] = (cur_close / prev_close - 1.0, prev_day)
    return out


def metrics_from_bars(bars: list[dict], benchmark_bars: list[dict], window_days: int) -> dict:
    prices = price_series(bars)
    days = list(prices)[-(window_days + 1):]
    if len(days) < 3:
        return {"error": "有效收盘价不足，无法计算风险指标。"}
    window_prices = {day: prices[day] for day in days}
    close = np.asarray([window_prices[day] for day in days])
    returns = close[1:] / close[:-1] - 1
    volatility = float(returns.std(ddof=1) * np.sqrt(252))
    latest = returns[-20:]
    drawdown = close / np.maximum.accumulate(close) - 1
    trough = int(drawdown.argmin())
    peak_day = int(close[:trough + 1].argmax())
    cutoff = float(np.percentile(returns, 5))
    tail = returns[returns <= cutoff]

    stock_rets = _daily_returns_with_prev(window_prices)
    bench_rets = _daily_returns_with_prev(price_series(benchmark_bars))
    paired = [
        (stock_rets[day][0], bench_rets[day][0])
        for day in days[1:]
        if day in stock_rets and day in bench_rets and stock_rets[day][1] == bench_rets[day][1]
    ]
    beta = None
    if len(paired) >= 3:
        xr = np.asarray([item[0] for item in paired])
        yr = np.asarray([item[1] for item in paired])
        variance = float(yr.var(ddof=1))
        if variance > 1e-12:
            beta = float(np.cov(xr, yr, ddof=1)[0, 1] / variance)
    sharpe = float(returns.mean() * 252 / volatility) if volatility > 1e-12 else None
    max_dd = float(drawdown[trough])
    var_95 = max(0.0, -cutoff)
    cvar_95 = max(0.0, -float(tail.mean())) if len(tail) else var_95
    return clean_for_json({
        "samples": len(returns),
        "sample_days": len(returns),
        "window_days": window_days,
        "as_of": days[-1],
        "annualized_volatility": volatility,
        "volatility_20d": float(latest.std(ddof=1) * np.sqrt(252)) if len(latest) >= 20 else None,
        "beta": beta,
        "sharpe": sharpe,
        "sharpe_ratio": sharpe,
        "risk_free_annual": 0.0,
        "max_drawdown": max_dd,
        "drawdown_from": days[peak_day],
        "drawdown_to": days[trough],
        "var_95": var_95,
        "cvar_95": cvar_95,
    })


def compute_risk_metrics(symbol: str, benchmark: str = "000300.SH", window_days: int = 250) -> dict:
    canonical = stock_ticker(symbol)
    if not 20 <= window_days <= 500:
        raise ValueError("风险窗口需为 20 至 500 个交易日。")
    bench = detect_index_code(benchmark)
    if bench is None:
        raise ValueError("基准需为带市场后缀的宽基指数。")
    source = get_kline(canonical, window_days + 1, "qfq")
    if source.get("adjust", "qfq") != "qfq":
        return {
            "symbol": canonical,
            "benchmark": bench[0],
            "error": "当前仅获取到不复权日线，为避免除权缺口扭曲波动率与回撤，已跳过风险指标计算。",
            "provenance": make_provenance(source="local", endpoint="risk", error="unadjusted_kline_rejected"),
        }
    bars = source.get("data") or []
    if len(bars) < 15:
        return {
            "symbol": canonical,
            "benchmark": bench[0],
            "error": "前复权日线有效样本不足 15 个交易日，无法稳健估计风险指标。",
            "provenance": {**(source.get("provenance") or {}), "error": "insufficient_samples"},
        }
    try:
        reference = tencent_kline(bench[0], window_days + 1, "", index=True)
    except Exception:
        reference = {"data": [], "provenance": make_provenance(
            source="tencent", endpoint="index-kline", error="基准行情不可用",
        )}
    result = metrics_from_bars(bars, reference.get("data") or [], window_days)
    result["symbol"] = canonical
    result["benchmark"] = bench[0]
    result["provenance"] = {
        **(source.get("provenance") or {}),
        "method": "daily_simple_returns_252",
        "benchmark": reference.get("provenance") or {},
    }
    return clean_for_json(result)
