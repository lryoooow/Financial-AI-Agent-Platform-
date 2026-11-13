"""market 节点：行情快照 + K 线 + 技术指标。

每个数据源调用单独 asyncio.wait_for 超时（默认 8s）：任一源超时/失败只记缺口、返回空，
绝不拖死整条研究任务（与 research 节点的 _timed_call 容忍模式一致）。
"""

from __future__ import annotations

import asyncio
import logging
import time

from .._utils import _timed_call
from ...tools.market import compute_indicators_from_kline, get_kline, get_realtime_quote
from ...tools.market_provider import tencent_kline
from ...tools.risk_metrics import metrics_from_bars
from ..state import ResearchState

logger = logging.getLogger(__name__)

_RISK_BARS = 251
_RISK_WINDOW = 250
_DISPLAY_BARS = 120


async def market_node(state: ResearchState) -> dict:
    t0 = time.perf_counter()
    code = state.get("normalized_ticker") or state.get("code", "")
    blocked = state.get("blocked_tools", {})

    if "get_kline" in blocked:
        quote, quote_ms, quote_err = await _timed_call(get_realtime_quote, code)
        kline, kline_ms, kline_err = {}, 0, "user_scope"
        benchmark = {}
    else:
        quote_result, kline_result, benchmark_result = await asyncio.gather(
            _timed_call(get_realtime_quote, code),
            _timed_call(get_kline, code, _RISK_BARS, "qfq"),
            _timed_call(tencent_kline, "000300.SH", _RISK_BARS, "", index=True, timeout=5),
        )
        quote, quote_ms, quote_err = quote_result
        kline, kline_ms, kline_err = kline_result
        benchmark = benchmark_result[0] or {}
    full_bars = (kline or {}).get("data", [])
    # 指标依赖 kline 数据；kline 缺口时传空，compute 自身也带超时
    indicators, indicators_ms, indicators_err = await _timed_call(
        compute_indicators_from_kline, code, full_bars, kline or {}
    )
    gaps = [{"source": s, "error": e} for s, e in (("quote", quote_err), ("kline", kline_err), ("indicators", indicators_err)) if e]

    findings = [
        {"key": "quote", "title": "实时快照", "quote": quote or {}, "provenance": (quote or {}).get("provenance")},
        {"key": "kline", "title": "K 线", "kline": full_bars[-_DISPLAY_BARS:], "provenance": (kline or {}).get("provenance")},
        {"key": "indicators", "title": "技术指标", "indicators": (indicators or {}).get("data", []), "provenance": (indicators or {}).get("provenance")},
    ]
    risk = metrics_from_bars(full_bars, benchmark.get("data", []), _RISK_WINDOW)
    if not risk.get("error"):
        findings.append({
            "key": "risk_metrics", "title": "确定性风险指标", "risk": risk,
            "provenance": {"asset": (kline or {}).get("provenance"), "benchmark": benchmark.get("provenance")},
        })

    risk_flags = []
    for ind in (indicators or {}).get("data", []):
        if ind.get("signal") in ("超买", "死叉"):
            risk_flags.append(f"{ind['name']} {ind['signal']}（值 {ind['value']}）")

    return {
        "findings": findings,
        "indicators": (indicators or {}).get("data", []),
        "risk_flags": risk_flags,
        "timings": {
            "market_ms": int((time.perf_counter() - t0) * 1000),
            "quote_ms": quote_ms,
            "kline_ms": kline_ms,
            "indicators_ms": indicators_ms,
            "gaps": gaps,
        },
    }
