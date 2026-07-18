"""行情数据：国内直连优先（Sina/Tencent/Eastmoney-direct），AkShare 最后兜底。

根因修复：之前 AkShare（东方财富，慢且常 RemoteDisconnected）被放在第一位，
单个请求能挂死事件循环；且每个 async 路由直接同步调用它 → 阻塞整个 asyncio loop → 全界面卡死。
现在直连免费源优先（秒级），AkShare 仅在直连全失败时兜底；耗时短的接口不会拖垮页面。

失败链路仍逐源记录 attempts（前端可显示"腾讯源成功 / AkShare 失败但已切换"），绝不伪造数据。
所有 provenance 携带 source / trading_date / attempts / elapsed_ms。

前端契约（A 股红涨绿跌）：
- quote：Stock 快照
- intraday：[{time, price, avg, volume}]（5 分钟一点）
- kline：[{date:YYYY-MM-DD, open, close, high, low, volume, ma5, ma10, ma20}]
- book：{asks:[{label, price, vol}], bids:[...]}
- indicators：[{name, value(str), signal}]
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import time
from typing import List, Optional

import pandas as pd

from ..cache import quote_cache
from . import make_provenance, now_iso
from . import market_provider
from .entity import normalize_ticker, to_market_ticker
from .market_provider import _fmt_volume, _fmt_amount

logger = logging.getLogger(__name__)

# 诊断记录便捷别名（最近 N 次行情尝试，供 /api/market/diagnostics 与 /api/evidence/status 暴露）
_record = market_provider.record


def _today() -> str:
    return dt.datetime.now().date().isoformat()


def _parse_date(value: str) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except Exception:  # noqa: BLE001
        return None


def _requires_today_intraday(now: Optional[dt.datetime] = None) -> bool:
    """交易日上午开盘后，分时不能继续展示上一交易日。

    必须排除法定节假日（工作日休市）：否则春节/国庆等法定假日（工作日调休），
    Sina/Tencent/东财返回的是上一交易日数据，此处若仍判定"需要今日数据"，
    _ensure_fresh 会把所有源全部判为"陈旧"而拒绝 → 节假日行情全挂。
    """
    from .context import is_trading_day
    n = now or dt.datetime.now()
    return is_trading_day(n) and n.time() >= dt.time(9, 35)


def _requires_today_kline(now: Optional[dt.datetime] = None) -> bool:
    """收盘后一段时间，日 K 不能继续停留在上一交易日（同样排除节假日）。"""
    from .context import is_trading_day
    n = now or dt.datetime.now()
    return is_trading_day(n) and n.time() >= dt.time(15, 10)


def _ensure_fresh(result: dict, *, kind: str) -> None:
    """拒绝明显陈旧的行情数据，避免明日仍画前一日线。"""
    trading_date = (
        result.get("trading_date")
        or (result.get("provenance") or {}).get("trading_date")
        or ""
    )
    got = _parse_date(trading_date)
    if not got:
        return
    today = dt.datetime.now().date()
    if kind == "intraday" and _requires_today_intraday() and got < today:
        raise ValueError(f"分时数据仍停留在上一交易日 {got.isoformat()}，当前交易日 {today.isoformat()} 尚未更新")
    if kind == "kline" and _requires_today_kline() and got < today:
        raise ValueError(f"日K数据仍停留在上一交易日 {got.isoformat()}，当前交易日 {today.isoformat()} 尚未更新")
    if kind == "quote" and _requires_today_intraday() and got < today:
        raise ValueError(f"报价数据仍停留在上一交易日 {got.isoformat()}，当前交易日 {today.isoformat()} 尚未更新")


def _is_bj(raw: str) -> bool:
    """北交所代码（东方财富直连 secid 对 BJ 不可靠，需跳过 eastmoney-direct）。"""
    return raw.startswith(("43", "83", "87", "88"))


def _enrich(prov: dict, *, trading_date: str = "", attempts: Optional[list] = None) -> dict:
    """给 provenance 补 trading_date / attempts（不改 make_provenance 公共签名）。"""
    prov["trading_date"] = trading_date or ""
    if attempts is not None:
        prov["attempts"] = attempts
    return prov


# ────────────────────────────────────────────────────────────
# 数值/字符串格式化
# ────────────────────────────────────────────────────────────
def _f(v, default=0.0) -> float:
    """安全转 float。None/NaN/'-' → default。"""
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:  # noqa: BLE001
        return default


def _fmt_market_cap(yuan: float) -> str:
    v = _f(yuan)
    if v >= 1e8:
        return f"{v/1e8:.2f}亿"
    return f"{v:.0f}"


# ────────────────────────────────────────────────────────────
# 实时快照（直连优先：Sina → Tencent → AkShare 兜底）
# ────────────────────────────────────────────────────────────
def _get_spot_em_df():
    """拉全市场快照 DataFrame（带 3s 缓存）。多个 code 共享同一次全市场拉取。

    旧实现按 spot_em:{code} 缓存单行，导致不同 code 各自触发一次全市场 3MB 拉取，
    缓存形同虚设。此处缓存整个 DataFrame，行查找复用同一份。
    """
    cached = quote_cache.get("spot_em_full")
    if cached is not None:
        return cached
    import akshare as ak

    df = ak.stock_zh_a_spot_em()
    quote_cache.set("spot_em_full", df, ttl=3)
    return df


def _get_spot_row(code: str) -> Optional[dict]:
    """从全市场快照中取单股行（带单行级缓存）。

    全市场 DataFrame 由 _get_spot_em_df 提供（多 code 共享一次拉取）；
    单行结果再按 spot_em:{code} 缓存 3s，避免重复 dict 转换。
    """
    raw = normalize_ticker(code)
    cache_key = f"spot_em:{raw}"
    cached = quote_cache.get(cache_key)
    if cached is not None:
        return cached
    df = _get_spot_em_df()
    row_df = df[df["代码"].astype(str) == raw]
    if row_df.empty:
        return None
    row = row_df.iloc[0].to_dict()
    quote_cache.set(cache_key, row, ttl=3)
    return row


def get_realtime_quote(code: str) -> dict:
    """实时快照（前端 Stock 形状）。Sina 直连 → Tencent 直连 → AkShare 兜底。"""
    raw = normalize_ticker(code)
    attempts: list[dict] = []
    t0 = time.perf_counter()

    def finish(result: dict, source: str) -> dict:
        _ensure_fresh(result, kind="quote")
        attempts.append({"source": source, "ok": True})
        _record("quote", source, True)
        result["provenance"] = _enrich(
            result.get("provenance", {}),
            trading_date=result.get("trading_date") or _today(),
            attempts=list(attempts),
        )
        result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return result

    # 1) Sina 直连（最快）
    try:
        return finish(market_provider.sina_realtime(raw), "sina")
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "sina", "ok": False, "error": str(e)})
        _record("quote", "sina", False, str(e))
        logger.debug("sina 实时失败，尝试 Tencent：%s", e)

    # 2) Tencent 直连
    try:
        return finish(market_provider.tencent_realtime(raw), "tencent")
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "tencent", "ok": False, "error": str(e)})
        _record("quote", "tencent", False, str(e))
        logger.debug("tencent 实时失败，尝试 AkShare：%s", e)

    # 3) AkShare 兜底（最慢，字段最全：市值/行业）
    try:
        row = _get_spot_row(raw)
        if not row:
            raise ValueError(f"akshare 未找到 {raw}")
        price = _f(row.get("最新价"))
        prev_close = _f(row.get("昨收", price))
        result = {
            "code": to_market_ticker(raw),
            "name": str(row.get("名称", "") or ""),
            "exchange": _classify(raw),
            "price": price,
            "change": round(_f(row.get("涨跌额", price - prev_close)), 3),
            "changePct": round(_f(row.get("涨跌幅")), 2),
            "open": _f(row.get("今开")),
            "high": _f(row.get("最高")),
            "low": _f(row.get("最低")),
            "prevClose": prev_close,
            "volume": _fmt_volume(_f(row.get("成交量"))),  # akshare 返回手
            "turnover": _fmt_amount(_f(row.get("成交额"))),
            "marketCap": _fmt_market_cap(_f(row.get("总市值"))),
            "pe": _f(row.get("市盈率-动态")),
            "pb": _f(row.get("市净率")),
            "roe": 0.0,  # 快照无 ROE，由基本面接口补
            "industry": str(row.get("行业", "") or ""),
            "as_of": now_iso(),
            "trading_date": _today(),
            "provenance": make_provenance(source="akshare", endpoint="stock_zh_a_spot_em", raw_value=raw),
        }
        return finish(result, "akshare")
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "akshare", "ok": False, "error": str(e)})
        _record("quote", "akshare", False, str(e))
        logger.debug("akshare 实时兜底也失败：%s", e)

    # 全部失败：明确标注，绝不返回假价格
    chain = " | ".join(a.get("error", "")[:80] for a in attempts if a.get("error"))
    empty = _empty_stock(
        raw, code,
        f"真实行情暂不可用（Sina/Tencent/AkShare 均失败）：{chain}",
        source="none", attempts=list(attempts),
    )
    empty["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    return empty


def _empty_stock(
    raw: str, code: str, err: str, *,
    source: str = "akshare", endpoint: str = "stock_zh_a_spot_em",
    attempts: Optional[list] = None,
) -> dict:
    prov = make_provenance(source=source, endpoint=endpoint, error=err)
    _enrich(prov, trading_date="", attempts=attempts)
    return {
        "code": to_market_ticker(raw) or code,
        "name": "",
        "exchange": "A股",
        "price": 0.0,
        "change": 0.0,
        "changePct": 0.0,
        "open": 0.0,
        "high": 0.0,
        "low": 0.0,
        "prevClose": 0.0,
        "volume": "—",
        "turnover": "—",
        "marketCap": "—",
        "pe": 0.0,
        "pb": 0.0,
        "roe": 0.0,
        "industry": "",
        "as_of": now_iso(),
        "trading_date": "",
        "provenance": prov,
    }


def _classify(code: str) -> str:
    from .entity import _classify_exchange
    return _classify_exchange(normalize_ticker(code))


# ────────────────────────────────────────────────────────────
# 分时（直连优先：Sina(5min) → Eastmoney(重采样5min) → AkShare 兜底）
# ────────────────────────────────────────────────────────────
def get_intraday(code: str) -> dict:
    """分时（09:30–15:00，5 分钟一点聚合，带均价线）。

    直连优先：Sina(scale=5) → Eastmoney(1min 重采样 5min，BJ 跳过) → AkShare 兜底。
    """
    raw = normalize_ticker(code)
    attempts: list[dict] = []
    t0 = time.perf_counter()

    def finish(result: dict, source: str, trading_date: str = "") -> dict:
        _ensure_fresh(result, kind="intraday")
        attempts.append({"source": source, "ok": True})
        _record("intraday", source, True)
        result["provenance"] = _enrich(
            result.get("provenance", {}),
            trading_date=trading_date or result.get("trading_date", ""),
            attempts=list(attempts),
        )
        result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return result

    # 1) Sina 直连（5 分钟）
    try:
        r = market_provider.sina_intraday(raw)
        return finish(r, "sina", r.get("trading_date", ""))
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "sina", "ok": False, "error": str(e)})
        _record("intraday", "sina", False, str(e))
        logger.debug("sina 分时失败，尝试东方财富直连：%s", e)

    # 2) 东方财富直连（1min 重采样 5min；BJ 跳过：secid 不可靠）
    if not _is_bj(raw):
        try:
            r = market_provider.eastmoney_intraday(raw)
            return finish(r, "eastmoney-direct", r.get("trading_date", ""))
        except Exception as e:  # noqa: BLE001
            attempts.append({"source": "eastmoney-direct", "ok": False, "error": str(e)})
            _record("intraday", "eastmoney-direct", False, str(e))
            logger.debug("东方财富直连分时失败，尝试 AkShare：%s", e)

    # 3) AkShare 兜底（1min → 5min 桶）
    try:
        import akshare as ak
        mkt = "sh" if raw.startswith(("60", "68", "90", "11", "51", "56")) else (
            "bj" if _is_bj(raw) else "sz"
        )
        df = ak.stock_zh_a_minute(symbol=f"{mkt}{raw}", period="1", adjust="")
        if df is None or df.empty:
            raise ValueError("stock_zh_a_minute 空数据")
        df = df.copy()
        df["dt"] = pd.to_datetime(df["day"] if "day" in df else df.iloc[:, 0])
        for col in ("open", "high", "low", "close", "volume"):
            if col in df:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.set_index("dt")
        hhmm = df.index.strftime("%H:%M")
        df = df[(hhmm >= "09:30") & (hhmm <= "15:00")]
        trading_date = ""
        if not df.empty:
            latest_trade_date = df.index.max().date()
            df = df[df.index.date == latest_trade_date]
            trading_date = str(latest_trade_date)
            today = dt.datetime.now().date()
            if latest_trade_date == today:
                now = dt.datetime.now()
                df = df[df.index <= now]
        bucket = df.resample("5min", label="left", closed="left")
        agg = bucket.agg(close=("close", "last"), volume=("volume", "sum")).dropna()
        agg["price"] = agg["close"]
        agg["time"] = agg.index.strftime("%H:%M")
        cum_p = (agg["price"] * agg["volume"]).cumsum()
        cum_v = agg["volume"].cumsum().replace(0, 1)
        agg["avg"] = (cum_p / cum_v).round(2)
        out = [
            {"time": str(r["time"]), "price": _f(r["price"]), "avg": _f(r["avg"]), "volume": int(_f(r["volume"]))}
            for _, r in agg.iterrows()
        ]
        prov = make_provenance(source="akshare", endpoint="stock_zh_a_minute", raw_value=raw)
        return finish({"data": out, "trading_date": trading_date, "provenance": prov}, "akshare", trading_date)
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "akshare", "ok": False, "error": str(e)})
        _record("intraday", "akshare", False, str(e))
        logger.debug("akshare 分时兜底也失败：%s", e)

    chain = " | ".join(a.get("error", "")[:80] for a in attempts if a.get("error"))
    return {
        "data": [],
        "trading_date": "",
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "provenance": _enrich(
            make_provenance(source="none", endpoint="intraday", error=f"分时数据暂不可用：{chain}"),
            attempts=list(attempts),
        ),
    }


# ────────────────────────────────────────────────────────────
# 日 K 线（直连优先：Tencent(qfq) → Eastmoney(qfq,BJ跳过) → Sina(不复权) → AkShare 兜底）
# ────────────────────────────────────────────────────────────
def get_kline(code: str, n: int = 60, adjust: str = "qfq") -> dict:
    """日 K 线（带 ma5/ma10/ma20）。直连优先，AkShare 兜底。"""
    raw = normalize_ticker(code)
    adjust_map = {"qfq": "前复权", "hfq": "后复权", "": ""}
    attempts: list[dict] = []
    t0 = time.perf_counter()

    def finish(result: dict, source: str) -> dict:
        _ensure_fresh(result, kind="kline")
        attempts.append({"source": source, "ok": True})
        _record("kline", source, True)
        result["provenance"] = _enrich(
            result.get("provenance", {}),
            trading_date=result.get("trading_date", ""),
            attempts=list(attempts),
        )
        result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return result

    # 1) Tencent 直连（前复权，BJ 正确）
    try:
        return finish(market_provider.tencent_kline(raw, n=n, adjust=adjust), "tencent")
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "tencent", "ok": False, "error": str(e)})
        _record("kline", "tencent", False, str(e))
        logger.debug("tencent 日K 失败，尝试东方财富直连：%s", e)

    # 2) 东方财富直连（前复权；BJ 跳过）
    if not _is_bj(raw):
        try:
            return finish(market_provider.eastmoney_kline(raw, n=n, adjust=adjust), "eastmoney-direct")
        except Exception as e:  # noqa: BLE001
            attempts.append({"source": "eastmoney-direct", "ok": False, "error": str(e)})
            _record("kline", "eastmoney-direct", False, str(e))
            logger.debug("东方财富直连日K 失败，尝试 Sina：%s", e)

    # 3) Sina 直连（不复权，明确标注）
    try:
        r = market_provider.sina_kline(raw, n=n)
        r["provenance"] = r.get("provenance", {})
        r["provenance"]["note"] = "备用源 Sina 为不复权数据，技术信号仅供参考"
        return finish(r, "sina")
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "sina", "ok": False, "error": str(e)})
        _record("kline", "sina", False, str(e))
        logger.debug("sina 日K 失败，尝试 AkShare：%s", e)

    # 4) AkShare 兜底（前复权）
    try:
        import akshare as ak
        end = dt.datetime.now().strftime("%Y%m%d")
        start = (dt.datetime.now() - dt.timedelta(days=int(n) * 3 + 90)).strftime("%Y%m%d")
        df = ak.stock_zh_a_hist(symbol=raw, period="daily", start_date=start, end_date=end, adjust=adjust)
        if df is None or df.empty:
            raise ValueError("stock_zh_a_hist 空数据")
        df = df.copy().tail(int(n)).reset_index(drop=True)
        df["ma5"] = df["收盘"].rolling(5).mean()
        df["ma10"] = df["收盘"].rolling(10).mean()
        df["ma20"] = df["收盘"].rolling(20).mean()
        dts = pd.to_datetime(df["日期"])
        out = []
        for i, row in df.iterrows():
            d = dts.iloc[i]
            out.append({
                "date": d.strftime("%Y-%m-%d"),
                "open": _f(row["开盘"]), "close": _f(row["收盘"]),
                "high": _f(row["最高"]), "low": _f(row["最低"]),
                "volume": int(_f(row["成交量"])),
                "ma5": _f(row["ma5"]), "ma10": _f(row["ma10"]), "ma20": _f(row["ma20"]),
            })
        prov = make_provenance(
            source="akshare", endpoint="stock_zh_a_hist",
            raw_value={"code": raw, "n": n, "adjust": adjust_map.get(adjust, adjust)},
        )
        return finish({"data": out, "trading_date": out[-1]["date"] if out else "", "provenance": prov}, "akshare")
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "akshare", "ok": False, "error": str(e)})
        _record("kline", "akshare", False, str(e))
        logger.debug("akshare 日K 兜底也失败：%s", e)

    chain = " | ".join(a.get("error", "")[:80] for a in attempts if a.get("error"))
    return {
        "data": [],
        "trading_date": "",
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "provenance": _enrich(
            make_provenance(source="none", endpoint="kline", error=f"日K 数据暂不可用：{chain}"),
            attempts=list(attempts),
        ),
    }


# ────────────────────────────────────────────────────────────
# 五档盘口（仅 AkShare；无可靠免费直连 5 档源，失败明确标注）
# ────────────────────────────────────────────────────────────
def get_bid_ask(code: str) -> dict:
    """五档盘口（akshare，无可靠免费备用源；失败明确标注，不伪造）。"""
    raw = normalize_ticker(code)
    attempts: list[dict] = []
    t0 = time.perf_counter()
    try:
        import akshare as ak
        mkt = "sh" if raw.startswith(("60", "68", "90", "11", "51", "56")) else (
            "bj" if _is_bj(raw) else "sz"
        )
        df = ak.stock_bid_ask_em(symbol=f"{mkt}{raw}")
        if df is None or df.empty:
            raise ValueError("空数据")
        rec = {str(r.iloc[0]): (r.iloc[1], r.iloc[2] if df.shape[1] > 2 else 0) for r in df.to_dict("index").values()}

        def pick(labels):
            for lab in labels:
                if lab in rec:
                    p, v = rec[lab]
                    return {"label": lab, "price": _f(p), "vol": int(_f(v))}
            return None

        asks = [pick(x) for x in (["卖五", "卖5"], ["卖四", "卖4"], ["卖三", "卖3"], ["卖二", "卖2"], ["卖一", "卖1"])]
        bids = [pick(x) for x in (["买一", "买1"], ["买二", "买2"], ["买三", "买3"], ["买四", "买4"], ["买五", "买5"])]
        asks = [a for a in asks if a]
        bids = [b for b in bids if b]
        attempts.append({"source": "akshare", "ok": True})
        _record("book", "akshare", True)
        prov = make_provenance(source="akshare", endpoint="stock_bid_ask_em", raw_value=f"{mkt}{raw}")
        return {
            "asks": asks, "bids": bids, "trading_date": _today(),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "provenance": _enrich(prov, trading_date=_today(), attempts=list(attempts)),
        }
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "akshare", "ok": False, "error": str(e)})
        _record("book", "akshare", False, str(e))
        logger.debug("get_bid_ask 失败（无免费直连源）：%s", e)
        return {
            "asks": [], "bids": [], "trading_date": "",
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "provenance": _enrich(
                make_provenance(source="akshare", endpoint="stock_bid_ask_em",
                                error=f"盘口暂不可用（akshare 失败，无免费直连源）：{e}"),
                attempts=list(attempts),
            ),
        }


# ────────────────────────────────────────────────────────────
# 技术指标
# ────────────────────────────────────────────────────────────
def _compute_indicators_df(df: pd.DataFrame) -> List[dict]:
    """对日 K DataFrame 计算 MA/MACD/RSI/KDJ/BOLL，返回指标列表。

    df 需有列：close, high, low（中文或英文均可）。
    pandas-ta-classic 优先；缺失则纯 pandas 兜底。
    """
    out: List[dict] = []
    close = df["close"]
    high = df.get("high", close)
    low = df.get("low", close)

    def _ma(n: int) -> float:
        s = close.tail(n)
        return _f(s.mean()) if len(s) else 0.0

    ma5, ma20 = _ma(5), _ma(20)
    last_close = _f(close.iloc[-1]) if len(close) else 0.0

    # MA
    out.append({"name": "MA5", "value": f"{ma5:.2f}", "signal": "多头" if last_close > ma5 > 0 else "空头"})
    out.append({"name": "MA20", "value": f"{ma20:.2f}", "signal": ("多头" if ma5 > ma20 else "空头") if ma5 and ma20 else "中性"})

    macd_hist = rsi_v = kdj_j = None
    try:
        import pandas_ta_classic as ta  # noqa: F401
        macd_df = close.ta.macd() if hasattr(close, "ta") else None
        if macd_df is not None and not macd_df.empty:
            macd_hist = _f(macd_df.iloc[-1].get(f"MACDh_12_26_9"))
        rsi_s = close.ta.rsi()
        if rsi_s is not None and len(rsi_s):
            rsi_v = _f(rsi_s.iloc[-1])
        hldf = pd.DataFrame({"high": high, "low": low, "close": close})
        stoch = hldf.ta.stoch()
        if stoch is not None and not stoch.empty:
            _k = _f(stoch.iloc[-1].get("STOCHk_14_3_3"))
            _d = _f(stoch.iloc[-1].get("STOCHd_14_3_3"))
            kdj_j = 3 * _k - 2 * _d  # J = 3K - 2D
    except Exception as e:  # noqa: BLE001
        logger.debug("pandas-ta-classic 不可用，回退纯 pandas：%s", e)

    if macd_hist is None:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd_hist = _f((dif - dea).iloc[-1])
    if rsi_v is None:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean().replace(0, 1e-9)
        rs = gain / loss
        rsi_v = _f((100 - 100 / (1 + rs)).iloc[-1]) if len(rs) else 50.0
    if kdj_j is None:
        low9 = low.rolling(9).min()
        high9 = high.rolling(9).max().replace(0, 1e-9)
        rsv = (close - low9) / (high9 - low9) * 100
        rsv = rsv.fillna(50.0)
        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        kdj_j = _f((3 * k - 2 * d).iloc[-1]) if len(d) else 50.0

    out.append({"name": "MACD", "value": f"{macd_hist:.2f}", "signal": "金叉" if macd_hist > 0 else "死叉"})
    out.append({"name": "RSI(14)", "value": f"{rsi_v:.1f}", "signal": "超买" if rsi_v >= 70 else ("超卖" if rsi_v <= 30 else "中性")})
    out.append({"name": "KDJ-J", "value": f"{kdj_j:.1f}", "signal": "超买" if kdj_j >= 90 else ("超卖" if kdj_j <= 10 else "中性")})

    try:
        mid = close.rolling(20).mean()
        std = close.rolling(20).std()
        up = mid + 2 * std
        lo = mid - 2 * std
        lc = last_close
        if len(mid) and not math.isnan(mid.iloc[-1]):
            mid_v = _f(mid.iloc[-1])
            lo_v = _f(lo.iloc[-1])
            sig = "多头" if lc > mid_v else ("空头" if lc < lo_v else "中性")
            out.append({"name": "BOLL", "value": f"{mid_v:.2f}", "signal": sig})
    except Exception:  # noqa: BLE001
        out.append({"name": "BOLL", "value": "—", "signal": "中性"})

    return out


def get_indicators(code: str) -> dict:
    """技术指标汇总（基于近 120 日 K 线）。K 线来源在 provenance 透传。"""
    raw = normalize_ticker(code)
    try:
        kl = get_kline(code, n=120, adjust="qfq")
        data = kl.get("data") or []
        return compute_indicators_from_kline(code, data, kl)
    except Exception as e:  # noqa: BLE001
        logger.exception("get_indicators 失败")
        return {
            "data": [],
            "trading_date": "",
            "elapsed_ms": None,
            "provenance": _enrich(
                make_provenance(source="pandas-ta-classic", endpoint="ta", error=str(e)),
                attempts=[{"source": "kline", "ok": False, "error": str(e)}],
            ),
        }


def compute_indicators_from_kline(code: str, data: list[dict], kline_result: Optional[dict] = None) -> dict:
    """基于已取得的 K 线计算技术指标，避免研究链路重复抓取 K 线。"""
    raw = normalize_ticker(code)
    try:
        if len(data or []) < 20:
            raise ValueError("K 线数据不足")
        df = pd.DataFrame(data)
        out = _compute_indicators_df(df)
        kl = kline_result or {}
        kl_prov = kl.get("provenance", {}) or {}
        src = kl_prov.get("source", "local-kline")
        prov = make_provenance(
            source=f"pandas-ta-classic·{src}",
            endpoint="stock_zh_a_hist+ta",
            raw_value=raw,
            error=kl_prov.get("error"),
        )
        if kl_prov.get("note"):
            prov["note"] = kl_prov["note"]
        return {
            "data": out,
            "trading_date": kl.get("trading_date", ""),
            "elapsed_ms": kl.get("elapsed_ms"),
            "provenance": _enrich(
                prov,
                trading_date=kl.get("trading_date", ""),
                attempts=kl_prov.get("attempts"),
            ),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("compute_indicators_from_kline 失败：%s", e)
        return {
            "data": [],
            "trading_date": (kline_result or {}).get("trading_date", ""),
            "elapsed_ms": (kline_result or {}).get("elapsed_ms"),
            "provenance": _enrich(
                make_provenance(source="pandas-ta-classic", endpoint="ta", error=str(e)),
                attempts=[{"source": "kline", "ok": False, "error": str(e)}],
            ),
        }
