"""Market overview with explicit index identities and source-specific timestamps."""

import asyncio
import datetime as dt
from typing import Any

import pandas as pd

from ..agents._utils import _timed_call
from ..cache import quote_cache
from . import clean_for_json, make_provenance, now_cst
from . import market_provider as providers
from .market import _ensure_fresh, _get_spot_em_df

INDICES = (
    ("000001.SH", "上证指数"), ("399001.SZ", "深证成指"), ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"), ("000300.SH", "沪深300"),
)
_LOCK = asyncio.Lock()


def index_quote(code: str, name: str) -> dict:
    for label, provider in (("sina", providers.sina_realtime), ("tencent", providers.tencent_realtime)):
        try:
            result = provider(code, index=True)
            _ensure_fresh(result, kind="quote")
            return {**result, "name": name, "code": code, "kind": "index"}
        except Exception:
            continue
    return {
        "code": code, "name": name, "kind": "index", "price": None, "changePct": None,
        "provenance": make_provenance(source="none", endpoint="index-quote", error="指数行情暂不可用"),
    }


def _latest_pool_date() -> dt.date:
    from .context import is_trading_day

    now = now_cst()
    day = now.date()
    if now.hour < 9 or (now.hour == 9 and now.minute < 30) or not is_trading_day(now):
        day -= dt.timedelta(days=1)
        for _ in range(15):
            if is_trading_day(dt.datetime.combine(day, dt.time(15, 0))):
                break
            day -= dt.timedelta(days=1)
    return day


_TURNOVER_EM_FAIL_AT = 0.0
_TURNOVER_EM_BREAKER_S = 300.0


def _market_totals() -> dict:
    import akshare as ak

    # 优先走乐咕乐股市场赚钱效应聚合接口（单请求 ~0.2s，含涨跌家数与涨跌停家数），
    # 避免 82.push2.eastmoney.com 全市场 80 页分页拉取在本机网络下 14s 超时并占满线程池。
    if hasattr(ak, "stock_market_activity_legu"):
        try:
            providers.configure_market_network()
            df_legu = ak.stock_market_activity_legu()
            if df_legu is not None and not df_legu.empty:
                kv = {str(r.get("item") or "").strip(): r.get("value") for _, r in df_legu.iterrows()}
                up = int(float(kv.get("上涨") or 0))
                down = int(float(kv.get("下跌") or 0))
                flat = int(float(kv.get("平盘") or 0))
                limit_up = int(float(kv.get("涨停") or 0)) if kv.get("涨停") is not None else None
                limit_down = int(float(kv.get("跌停") or 0)) if kv.get("跌停") is not None else None
                total = up + down + flat
                if total > 0:
                    prov = make_provenance(source="legulegu", endpoint="stock_market_activity_legu")
                    return {
                        "up": up,
                        "down": down,
                        "flat": flat,
                        "count": total,
                        "limit_up": limit_up,
                        "limit_down": limit_down,
                        "turnover_yuan": None,
                        "previous_turnover_yuan": None,
                        "turnover_change_yuan": None,
                        "provenance": prov,
                        "limit_up_provenance": prov,
                        "limit_down_provenance": prov,
                    }
        except Exception:
            pass

    frame = _get_spot_em_df()
    if frame is None or frame.empty:
        raise ValueError("全市场行情未返回数据")
    change = pd.to_numeric(frame["涨跌幅"], errors="coerce").dropna()
    days = _latest_pool_date()
    payload = {
        "up": int((change > 0).sum()), "down": int((change < 0).sum()), "flat": int((change == 0).sum()),
        "count": int(change.size), "limit_up": None, "limit_down": None,
        "turnover_yuan": None, "previous_turnover_yuan": None, "turnover_change_yuan": None,
        "provenance": make_provenance(source="eastmoney", endpoint="stock_zh_a_spot_em"),
    }
    for key, endpoint in (("limit_up", ak.stock_zt_pool_em), ("limit_down", ak.stock_zt_pool_dtgc_em)):
        try:
            pool = endpoint(date=days.strftime("%Y%m%d"))
            if pool is not None:
                payload[key] = len(pool)
                payload[f"{key}_provenance"] = make_provenance(
                    source="eastmoney", endpoint=endpoint.__name__, raw_value=days.isoformat(),
                )
        except Exception:
            continue
    return payload


def _exchange_turnover() -> dict:
    import time
    import akshare as ak

    from .context import is_trading_day

    global _TURNOVER_EM_FAIL_AT
    if getattr(ak, "__file__", None) and (time.time() - _TURNOVER_EM_FAIL_AT < _TURNOVER_EM_BREAKER_S):
        return {}

    now = now_cst()
    start = (now - dt.timedelta(days=25)).strftime("%Y%m%d")
    end = now.strftime("%Y%m%d")
    try:
        first = ak.stock_zh_index_daily_em(symbol="sh000001", start_date=start, end_date=end)
        second = ak.stock_zh_index_daily_em(symbol="sz399106", start_date=start, end_date=end)
    except Exception:
        _TURNOVER_EM_FAIL_AT = time.time()
        return {}
    if first.empty or second.empty:
        return {}
    common = first[["date", "amount"]].merge(second[["date", "amount"]], on="date").sort_values("date")
    common = common.dropna().drop_duplicates("date", keep="last")
    if common.empty:
        return {}
    current = common.iloc[-1]
    day = dt.date.fromisoformat(str(current["date"])[:10])
    partial = day == now.date() and now.hour < 15
    current_total = float(current["amount_x"] + current["amount_y"])
    previous_day = day - dt.timedelta(days=1)
    while not is_trading_day(dt.datetime.combine(previous_day, dt.time())):
        previous_day -= dt.timedelta(days=1)
    historical = common[common["date"].astype(str).str[:10] == previous_day.isoformat()]
    previous = historical.iloc[-1] if not historical.empty else None
    previous_total = float(previous["amount_x"] + previous["amount_y"]) if previous is not None else None
    return {
        "trading_date": day.isoformat(), "comparison_date": previous_day.isoformat() if previous_total is not None else None,
        "turnover_yuan": current_total, "previous_turnover_yuan": previous_total,
        "turnover_change_yuan": current_total - previous_total if previous_total is not None and not partial else None,
        "turnover_partial": partial,
        "turnover_provenance": {
            **make_provenance(source="eastmoney", endpoint="stock_zh_index_daily_em", raw_value=["sh000001", "sz399106"]),
            "note": "上证综指与深证综指对应市场成交额",
        },
    }


def _industry_heat() -> list[dict]:
    import akshare as ak

    frame = ak.index_realtime_sw(symbol="一级行业")
    flows = {}
    try:
        reference = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        for row in reference.to_dict(orient="records"):
            value = pd.to_numeric(row.get("今日主力净流入-净额"), errors="coerce")
            if pd.notna(value):
                flows[str(row.get("名称") or "")] = float(value)
    except Exception:
        pass
    result = []
    for row in frame.to_dict(orient="records"):
        prev, price = pd.to_numeric(row.get("昨收盘"), errors="coerce"), pd.to_numeric(row.get("最新价"), errors="coerce")
        if pd.isna(prev) or pd.isna(price) or prev <= 0 or price <= 0:
            continue
        name = str(row.get("指数名称") or "")
        result.append({
            "code": f"{row['指数代码']}.SI", "name": name, "changePct": round((price / prev - 1) * 100, 2),
            "net_flow_yuan": flows.get(name),
            "provenance": make_provenance(source="shenwan", endpoint="index_realtime_sw"),
            "flow_provenance": make_provenance(source="eastmoney", endpoint="stock_sector_fund_flow_rank") if name in flows else None,
        })
    return sorted(result, key=lambda row: row["changePct"], reverse=True)


async def market_overview(refresh: bool = False) -> dict:
    async with _LOCK:
        if not refresh and (cached := quote_cache.get("market-overview")) is not None:
            return cached
        jobs = [*(_timed_call(index_quote, code, name, timeout=7) for code, name in INDICES)]
        jobs.extend((_timed_call(_market_totals, timeout=14), _timed_call(_industry_heat, timeout=14),
                     _timed_call(_exchange_turnover, timeout=8)))
        results = await asyncio.gather(*jobs)
        indices = [
            result or {
                "code": code, "name": name, "price": None, "changePct": None,
                "provenance": make_provenance(source="none", endpoint="index-quote", error=err),
            }
            for (code, name), (result, _, err) in zip(INDICES, results[:5])
        ]
        breadth, _, breadth_error = results[5]
        industries, _, industry_error = results[6]
        turnover, _, _ = results[7]
        turnover = dict(turnover or {})
        if not turnover.get("turnover_yuan"):
            sh_idx = next((it for it in indices if it.get("code") == "000001.SH"), {})
            sz_idx = next((it for it in indices if it.get("code") == "399001.SZ"), {})
            sh_amt, sz_amt = sh_idx.get("amount"), sz_idx.get("amount")
            if isinstance(sh_amt, (int, float)) and isinstance(sz_amt, (int, float)) and (sh_amt + sz_amt) > 0:
                t_day = str(sh_idx.get("trading_date") or sz_idx.get("trading_date") or "")[:10]
                turnover = {
                    "trading_date": t_day or None,
                    "comparison_date": None,
                    "turnover_yuan": float(sh_amt + sz_amt),
                    "previous_turnover_yuan": None,
                    "turnover_change_yuan": None,
                    "turnover_partial": False,
                    "turnover_provenance": {
                        **make_provenance(source=str((sh_idx.get("provenance") or {}).get("source") or "direct"), endpoint="index-quote-amount"),
                        "note": "沪深指数快照成交额合计兜底",
                    },
                }
        result = clean_for_json({
            "indices": indices, "breadth": {**(breadth or {"provenance": make_provenance(
                source="none", endpoint="market-breadth", error=breadth_error or "全市场数据不可用",
            )}), **turnover},
            "turnover_dates": {key: turnover.get(key) for key in ("trading_date", "comparison_date")},
            "industries": industries or [], "industry_error": industry_error,
            "leaders": (industries or [])[:6], "laggards": list(reversed((industries or [])[-6:])),
        })
        quote_cache.set("market-overview", result, ttl=30)
        return result
