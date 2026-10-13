"""行情数据：国内直连优先（Sina/Tencent/Eastmoney-direct），AkShare 最后兜底。

根因修复：之前 AkShare（东方财富，慢且常 RemoteDisconnected）被放在第一位，
单个请求能挂死事件循环；且每个 async 路由直接同步调用它 → 阻塞整个 asyncio loop → 全界面卡死。
现在直连免费源优先（秒级），AkShare 仅在直连全失败时兜底；耗时短的接口不会拖垮页面。

失败链路仍逐源记录 attempts（前端可显示"腾讯源成功 / AkShare 失败但已切换"），绝不伪造数据。
所有 provenance 携带 source / trading_date / attempts / elapsed_ms。

前端契约（A 股红涨绿跌）：
- quote：Stock 快照
- intraday：[{time, price, avg, volume}]（优先 1 分钟，备用源可能为 5 分钟）
- kline：[{date:YYYY-MM-DD, open, close, high, low, volume, ma5, ma10, ...}]
  （均线周期随区间自适应：只输出 <= 区间长度的周期，5日→ma5，10日→ma5/ma10，20日→ma5/ma10/ma20，
   60/120/250 日依次追加 ma60/ma120/ma250；并额外预取历史用于均线预热，保证线条覆盖整个窗口）
- book：{asks:[{label, price, vol}], bids:[...]}
- indicators：[{name, value(str), signal}]
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import pandas as pd

from ..cache import quote_cache
from . import make_provenance, market_provider, now_iso
from .entity import is_index, is_bj as _is_bj
from .entity import is_sh, normalize_ticker, to_market_ticker
from .market_provider import _fmt_amount, _fmt_volume

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


def _ensure_fresh(result: dict, *, kind: str) -> Optional[str]:
    """返回陈旧提示但保留最后有效数据，适配停牌和数据源延迟。"""
    trading_date = (
        result.get("trading_date")
        or (result.get("provenance") or {}).get("trading_date")
        or ""
    )
    got = _parse_date(trading_date)
    if not got:
        return None
    today = dt.datetime.now().date()
    if kind == "intraday" and _requires_today_intraday() and got < today:
        return f"分时数据停留在 {got.isoformat()}，当前交易日数据尚未更新"
    if kind == "kline" and _requires_today_kline() and got < today:
        return f"日K数据停留在 {got.isoformat()}，当前交易日数据尚未更新"
    if kind == "quote" and _requires_today_intraday() and got < today:
        return f"报价数据停留在 {got.isoformat()}，当前交易日数据尚未更新"
    return None


def _supports_eastmoney_direct(raw: str) -> bool:
    """920 新号段已实测支持 secid=0；旧北交所号段仍走备用链路。"""
    return raw.startswith("92") or not _is_bj(raw)


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


def _nullable_f(v) -> Optional[float]:
    """安全转为可空浮点数，指标尚未形成时保留 null。"""
    try:
        if v is None:
            return None
        value = float(v)
        return None if math.isnan(value) or math.isinf(value) else value
    except Exception:  # noqa: BLE001
        return None


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
        stale_reason = _ensure_fresh(result, kind="quote")
        attempts.append({"source": source, "ok": True})
        _record("quote", source, True)
        result["provenance"] = _enrich(
            result.get("provenance", {}),
            # 拿不到真实数据日期就留空，不伪造今天——伪造会击穿 _ensure_fresh 的陈旧检测
            trading_date=result.get("trading_date") or "",
            attempts=list(attempts),
        )
        if stale_reason:
            result["stale"] = True
            result["provenance"]["stale_reason"] = stale_reason
        result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return result

    # 1) Tencent 直连（优先：字段最全——PE/PB/总市值都有）
    #    实测 Sina hq 只有 34 个基础字段，无估值字段：sina 命中时行情页
    #    PE/PB/市值恒为"—"。腾讯字段全且速度同级，故提为首选。
    try:
        return finish(market_provider.tencent_realtime(raw), "tencent")
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "tencent", "ok": False, "error": str(e)})
        _record("quote", "tencent", False, str(e))
        logger.debug("tencent 实时失败，尝试 Sina：%s", e)

    # 2) Sina 直连（快，但无估值字段）
    try:
        return finish(market_provider.sina_realtime(raw), "sina")
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "sina", "ok": False, "error": str(e)})
        _record("quote", "sina", False, str(e))
        logger.debug("sina 实时失败，尝试 AkShare：%s", e)

    # 3) AkShare 兜底（最慢，字段最全：市值/行业）——指数无此兜底：
    #    spot 全市场表只含股票，指数去查只会在 3MB 表里白扫一遍然后失败。
    if is_index(code):
        chain = " | ".join(a.get("error", "")[:80] for a in attempts if a.get("error"))
        empty = _empty_stock(
            raw, code,
            f"指数行情暂不可用（tencent/sina 均失败）：{chain}",
            source="none", attempts=list(attempts),
        )
        empty["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return empty
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
            # em 全市场快照无日期列：留空让 _ensure_fresh 如实跳过，绝不伪装成今日数据
            "trading_date": "",
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
# 分时（直连优先：Eastmoney(1min) → Sina(5min) → AkShare(1min) 兜底）
# ────────────────────────────────────────────────────────────
def get_intraday(code: str) -> dict:
    """分时（09:30–15:00，优先 1 分钟，带累计均价线）。

    直连优先：Eastmoney(1min) → Sina(scale=5) → AkShare(1min) 兜底。
    """
    raw = normalize_ticker(code)
    attempts: list[dict] = []
    t0 = time.perf_counter()

    def finish(result: dict, source: str, trading_date: str = "") -> dict:
        stale_reason = _ensure_fresh(result, kind="intraday")
        attempts.append({"source": source, "ok": True})
        _record("intraday", source, True)
        result["provenance"] = _enrich(
            result.get("provenance", {}),
            trading_date=trading_date or result.get("trading_date", ""),
            attempts=list(attempts),
        )
        if stale_reason:
            result["stale"] = True
            result["provenance"]["stale_reason"] = stale_reason
        result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return result

    # 1) 东方财富直连（原生 1min；920 新号段可用，旧北交所号段跳过）
    if _supports_eastmoney_direct(raw):
        try:
            r = market_provider.eastmoney_intraday(raw)
            return finish(r, "eastmoney-direct", r.get("trading_date", ""))
        except Exception as e:  # noqa: BLE001
            attempts.append({"source": "eastmoney-direct", "ok": False, "error": str(e)})
            _record("intraday", "eastmoney-direct", False, str(e))
            logger.debug("东方财富直连分时失败，尝试 Sina：%s", e)

    # 2) Sina 直连（5 分钟备用）
    try:
        r = market_provider.sina_intraday(raw)
        return finish(r, "sina", r.get("trading_date", ""))
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "sina", "ok": False, "error": str(e)})
        _record("intraday", "sina", False, str(e))
        logger.debug("sina 分时失败，尝试 AkShare：%s", e)

    # 3) AkShare 兜底（保留原生 1min）
    try:
        import akshare as ak
        mkt = "sh" if is_sh(raw) else ("bj" if _is_bj(raw) else "sz")
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
        agg = df.dropna(subset=["close"])
        agg["price"] = agg["close"]
        agg["time"] = agg.index.strftime("%H:%M")
        cum_p = (agg["price"] * agg["volume"]).cumsum()
        cum_v = agg["volume"].cumsum().replace(0, 1)
        agg["avg"] = (cum_p / cum_v).round(2)
        out = [
            # volume /100：stock_zh_a_minute（新浪 getKLineData）返回股，其余分时路径均为手，
            # 不对齐会导致 summarize_intraday 的量能汇总随命中源跳变 100 倍
            {"time": str(r["time"]), "price": _f(r["price"]), "avg": _f(r["avg"]), "volume": int(_f(r["volume"]) / 100.0)}
            for _, r in agg.iterrows()
        ]
        prov = make_provenance(source="akshare", endpoint="stock_zh_a_minute", raw_value=raw)
        return finish(
            {"data": out, "interval": "1m", "trading_date": trading_date, "provenance": prov},
            "akshare",
            trading_date,
        )
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


def summarize_intraday(result: dict, *, max_points: int = 48) -> dict:
    """把逐分钟分时压成可放进模型上下文的摘要。

    为什么要压：1 分钟粒度一个交易日 241 个点，整段塞给模型要上万 token，而模型判断
    日内形势需要的是关键位与走势轮廓，不是每一分钟的值。这里只做「挑选」不做「加工」——
    统计量取自原始序列的极值/首末，降采样点原样保留，不平滑、不插值、不估算。

    prev_close 缺失时（Sina/AkShare 分支的源本身不返回昨收）不编造涨跌幅，
    相关字段留 None，由调用方如实说明。
    """
    rows = [r for r in (result.get("data") or []) if isinstance(r, dict)]
    base = {
        "trading_date": result.get("trading_date", ""),
        "interval": result.get("interval", ""),
        "point_count": len(rows),
        "provenance": result.get("provenance", {}),
    }
    if not rows:
        return {**base, "error": (result.get("provenance") or {}).get("error") or "分时数据为空"}

    prices = [_f(r.get("price")) for r in rows]
    high_index = max(range(len(rows)), key=lambda i: prices[i])
    low_index = min(range(len(rows)), key=lambda i: prices[i])
    prev_close = _nullable_f(result.get("prev_close"))
    last = prices[-1]

    summary = {
        **base,
        "prev_close": prev_close,
        "open": prices[0],
        "high": prices[high_index],
        "high_time": str(rows[high_index].get("time", "")),
        "low": prices[low_index],
        "low_time": str(rows[low_index].get("time", "")),
        "last": last,
        "last_time": str(rows[-1].get("time", "")),
        "avg": _nullable_f(rows[-1].get("avg")),
        "volume": sum(_f(r.get("volume")) for r in rows),
        "amount": sum(_f(r.get("amount")) for r in rows) or None,
        "change": None,
        "change_pct": None,
        "amplitude_pct": None,
    }
    if prev_close:
        summary["change"] = round(last - prev_close, 4)
        summary["change_pct"] = round((last - prev_close) / prev_close * 100, 2)
        summary["amplitude_pct"] = round(
            (prices[high_index] - prices[low_index]) / prev_close * 100, 2
        )

    # 均匀降采样 + 强制保留首/尾/最高/最低，保证形态判断不丢关键位。
    keep = {0, len(rows) - 1, high_index, low_index}
    if len(rows) > max_points:
        step = (len(rows) - 1) / (max_points - 1)
        keep.update(round(i * step) for i in range(max_points))
    else:
        keep.update(range(len(rows)))
    summary["points"] = [
        {"time": str(rows[i].get("time", "")), "price": prices[i], "avg": _nullable_f(rows[i].get("avg"))}
        for i in sorted(keep)
        if 0 <= i < len(rows)
    ]
    return summary


# ────────────────────────────────────────────────────────────
# 日 K 线（直连优先：Tencent(qfq) → Eastmoney(qfq,BJ跳过) → Sina(不复权) → AkShare 兜底）
# ────────────────────────────────────────────────────────────
MA_PERIODS: tuple[int, ...] = (5, 10, 20, 60, 120, 250)
KLINE_MIN_DAYS = MA_PERIODS[0]
KLINE_MAX_DAYS = MA_PERIODS[-1]
# 指标（MACD 26+9 / BOLL 20 / RSI 14 / KDJ 9）预热根数，保证首根可见 K 线的指标已成形
INDICATOR_WARMUP = 60
# 单次抓取上限，避免 250 日 + 均线预热 + 指标预热后向数据源请求过长历史
_MAX_FETCH = 900


def ma_periods_for(n: int) -> List[int]:
    """区间越长均线越多：只保留周期 <= 区间长度的均线。"""
    count = max(int(n or 0), KLINE_MIN_DAYS)
    return [period for period in MA_PERIODS if period <= count]


def _attach_moving_averages(rows: List[dict], periods: List[int]) -> List[dict]:
    """在 K 线行上补齐各周期均线；样本不足的位置保持 None，不伪造 0。"""
    if not rows:
        return []
    close = pd.to_numeric(pd.Series([row.get("close") for row in rows]), errors="coerce")
    averages = {f"ma{period}": close.rolling(period).mean() for period in periods}
    return [
        {**row, **{name: _nullable_f(series.iloc[index]) for name, series in averages.items()}}
        for index, row in enumerate(rows)
    ]


def get_kline(code: str, n: int = 60, adjust: str = "qfq", warmup: int = 0) -> dict:
    """日 K 线（均线周期随区间自适应）。直连优先，AkShare 兜底。

    n：需要展示的 K 线根数；warmup：额外返回的前置根数（供上层指标预热，默认 0）。
    内部会多抓 max(周期)-1 根用于均线预热，返回前再截断，保证均线覆盖整个展示窗口。
    """
    raw = normalize_ticker(code)
    adjust_map = {"qfq": "前复权", "hfq": "后复权", "": ""}
    attempts: list[dict] = []
    t0 = time.perf_counter()

    count = max(KLINE_MIN_DAYS, int(n or KLINE_MIN_DAYS))
    keep = count + max(0, int(warmup))
    periods = ma_periods_for(count)
    fetch = min(keep + max(periods) - 1, _MAX_FETCH)

    def finish(result: dict, source: str) -> dict:
        result["data"] = _attach_moving_averages(result.get("data") or [], periods)[-keep:]
        stale_reason = _ensure_fresh(result, kind="kline")
        attempts.append({"source": source, "ok": True})
        _record("kline", source, True)
        result["provenance"] = _enrich(
            result.get("provenance", {}),
            trading_date=result.get("trading_date", ""),
            attempts=list(attempts),
        )
        if stale_reason:
            result["stale"] = True
            result["provenance"]["stale_reason"] = stale_reason
        result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return result

    # 1) Tencent 直连（前复权，BJ 正确）
    try:
        return finish(market_provider.tencent_kline(raw, n=fetch, adjust=adjust), "tencent")
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "tencent", "ok": False, "error": str(e)})
        _record("kline", "tencent", False, str(e))
        logger.debug("tencent 日K 失败，尝试东方财富直连：%s", e)

    # 2) 东方财富直连（前复权；BJ 跳过）
    if _supports_eastmoney_direct(raw):
        try:
            return finish(market_provider.eastmoney_kline(raw, n=fetch, adjust=adjust), "eastmoney-direct")
        except Exception as e:  # noqa: BLE001
            attempts.append({"source": "eastmoney-direct", "ok": False, "error": str(e)})
            _record("kline", "eastmoney-direct", False, str(e))
            logger.debug("东方财富直连日K 失败，尝试 Sina：%s", e)

    # 3) Sina 直连（不复权，明确标注）
    try:
        r = market_provider.sina_kline(raw, n=fetch)
        r["provenance"] = r.get("provenance", {})
        r["provenance"]["note"] = "备用源 Sina 为不复权数据，技术信号仅供参考"
        return finish(r, "sina")
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "sina", "ok": False, "error": str(e)})
        _record("kline", "sina", False, str(e))
        logger.debug("sina 日K 失败，尝试 AkShare：%s", e)

    # 4) AkShare 兜底（前复权）——指数跳过：stock_zh_a_hist 只覆盖股票
    if not is_index(code):
        try:
            import akshare as ak
            end = dt.datetime.now().strftime("%Y%m%d")
            start = (dt.datetime.now() - dt.timedelta(days=fetch * 3 + 90)).strftime("%Y%m%d")
            df = ak.stock_zh_a_hist(symbol=raw, period="daily", start_date=start, end_date=end, adjust=adjust)
            if df is None or df.empty:
                raise ValueError("stock_zh_a_hist 空数据")
            df = df.copy().tail(fetch).reset_index(drop=True)
            dts = pd.to_datetime(df["日期"])
            out = []
            for i, row in df.iterrows():
                d = dts.iloc[i]
                out.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "open": _f(row["开盘"]), "close": _f(row["收盘"]),
                    "high": _f(row["最高"]), "low": _f(row["最低"]),
                    "volume": int(_f(row["成交量"])),
                })
            prov = make_provenance(
                source="akshare", endpoint="stock_zh_a_hist",
                raw_value={"code": raw, "n": count, "adjust": adjust_map.get(adjust, adjust)},
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
# 五档盘口（直连优先：Tencent qt 五档 → AkShare 兜底；失败明确标注）
# ────────────────────────────────────────────────────────────
def get_bid_ask(code: str) -> dict:
    """五档盘口（腾讯 qt 直连优先，akshare 兜底；失败明确标注，不伪造）。

    实测：腾讯 qt 字段 9-28 即买卖五档价量（单位手），收盘后仍保留当日快照；
    东财 push2 对无 UA 的连接直接掐断、非交易时段五档字段清空，akshare 包装
    恰好不带 UA——曾长期只有 akshare 一条路，盘口实际从未可用。
    """
    raw = normalize_ticker(code)
    attempts: list[dict] = []
    t0 = time.perf_counter()

    if is_index(code):
        # 指数没有五档盘口；不拦截的话 akshare 兜底会把 000001 当平安银行
        # 返回一只股票的五档——数据张冠李戴。
        return {
            "asks": [], "bids": [], "trading_date": "",
            "elapsed_ms": 0,
            "provenance": _enrich(
                make_provenance(source="none", endpoint="bid_ask", error="指数无五档盘口"),
                attempts=[],
            ),
        }

    def _wrap(result: dict, source: str) -> dict:
        attempts.append({"source": source, "ok": True})
        _record("book", source, True)
        result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        result["provenance"] = _enrich(
            result.get("provenance", {}),
            trading_date=result.get("trading_date", ""),
            attempts=list(attempts),
        )
        return result

    # 1) 腾讯直连（快、收盘后仍有当日快照、量纲就是手）
    try:
        return _wrap(market_provider.tencent_bid_ask(raw), "tencent")
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "tencent", "ok": False, "error": str(e)})
        _record("book", "tencent", False, str(e))
        logger.debug("tencent 五档失败，尝试 akshare：%s", e)

    # 2) AkShare 兜底（注意：stock_bid_ask_em 要求纯 6 位代码自己拼 secid，
    #    传 sh600519 会拼出非法 secid 被远端掐断）
    try:
        import akshare as ak
        df = ak.stock_bid_ask_em(symbol=raw)
        if df is None or df.empty:
            raise ValueError("空数据")
        # 两列 [item, value]：sell_5 / sell_5_vol / buy_1 / buy_1_vol ...
        # （量字段已 ×100 转为股，前端展示口径是手）
        rec = {str(k): v for k, v in zip(df.iloc[:, 0], df.iloc[:, 1])}

        def pick(price_key: str, vol_key: str, label: str):
            if price_key not in rec:
                return None
            vol = _f(rec.get(vol_key)) / 100.0  # 股 → 手
            return {"label": label, "price": _f(rec[price_key]), "vol": int(vol) if vol else 0}

        asks = [pick(f"sell_{i}", f"sell_{i}_vol", f"卖{cn}") for i, cn in zip(range(5, 0, -1), "五四三二一")]
        bids = [pick(f"buy_{i}", f"buy_{i}_vol", f"买{cn}") for i, cn in zip(range(1, 6), "一二三四五")]
        asks = [a for a in asks if a]
        bids = [b for b in bids if b]
        prov = make_provenance(source="akshare", endpoint="stock_bid_ask_em", raw_value=raw)
        return _wrap({"asks": asks, "bids": bids, "trading_date": "", "provenance": prov}, "akshare")
    except Exception as e:  # noqa: BLE001
        attempts.append({"source": "akshare", "ok": False, "error": str(e)})
        _record("book", "akshare", False, str(e))
        logger.debug("akshare 五档兜底也失败：%s", e)
        chain = " | ".join(a.get("error", "")[:80] for a in attempts if a.get("error"))
        return {
            "asks": [], "bids": [], "trading_date": "",
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "provenance": _enrich(
                make_provenance(source="none", endpoint="bid_ask",
                                error=f"五档盘口暂不可用（tencent/akshare 均失败）：{chain}"),
                attempts=list(attempts),
            ),
        }


# ────────────────────────────────────────────────────────────
# 技术指标
# ────────────────────────────────────────────────────────────
def _frame_ma_periods(df: pd.DataFrame) -> List[int]:
    """指标序列的均线周期与 K 线保持一致：K 线里有哪几条就算哪几条。"""
    present = [period for period in MA_PERIODS if f"ma{period}" in df.columns]
    return present or [5, 10, 20]


def _compute_indicator_frame(df: pd.DataFrame, periods: Optional[List[int]] = None) -> pd.DataFrame:
    """一次性计算前端绘图与摘要共用的完整指标序列。"""
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df.get("high", close), errors="coerce")
    low = pd.to_numeric(df.get("low", close), errors="coerce")
    frame = pd.DataFrame(index=df.index)
    for period in (periods if periods is not None else _frame_ma_periods(df)):
        frame[f"ma{period}"] = close.rolling(period).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    frame["dif"] = ema12 - ema26
    frame["dea"] = frame["dif"].ewm(span=9, adjust=False).mean()
    frame["macd"] = 2 * (frame["dif"] - frame["dea"])

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    relative_strength = gain / loss.where(loss != 0)
    frame["rsi14"] = 100 - 100 / (1 + relative_strength)
    frame.loc[(loss == 0) & gain.notna(), "rsi14"] = 100.0

    low9 = low.rolling(9).min()
    high9 = high.rolling(9).max()
    spread = (high9 - low9).where(high9 != low9)
    rsv = ((close - low9) / spread * 100).fillna(50.0)
    frame["k"] = rsv.ewm(com=2, adjust=False).mean()
    frame["d"] = frame["k"].ewm(com=2, adjust=False).mean()
    frame["j"] = 3 * frame["k"] - 2 * frame["d"]

    frame["bollMid"] = close.rolling(20).mean()
    standard_deviation = close.rolling(20).std()
    frame["bollUpper"] = frame["bollMid"] + 2 * standard_deviation
    frame["bollLower"] = frame["bollMid"] - 2 * standard_deviation
    return frame


def _indicator_series(df: pd.DataFrame) -> List[dict]:
    frame = _compute_indicator_frame(df)
    dates = df["date"] if "date" in df else df.index.astype(str)
    fields = tuple(frame.columns)
    return [
        {"date": str(dates.iloc[index])[:10], **{field: _nullable_f(row[field]) for field in fields}}
        for index, (_, row) in enumerate(frame.iterrows())
    ]


def _compute_indicators_df(df: pd.DataFrame) -> List[dict]:
    """从完整指标序列生成最新摘要，保证摘要与图表数值同源。"""
    frame = _compute_indicator_frame(df)
    if frame.empty:
        return []
    last = frame.iloc[-1]
    last_close = _f(pd.to_numeric(df["close"], errors="coerce").iloc[-1])
    # 短区间（如 5 日）不会计算 MA20，取值需容忍列缺失
    ma5 = _nullable_f(last["ma5"]) if "ma5" in frame.columns else None
    ma20 = _nullable_f(last["ma20"]) if "ma20" in frame.columns else None
    macd = _nullable_f(last["macd"])
    rsi = _nullable_f(last["rsi14"])
    kdj_j = _nullable_f(last["j"])
    boll_mid = _nullable_f(last["bollMid"])
    boll_lower = _nullable_f(last["bollLower"])

    return [
        {"name": "MA5", "value": f"{ma5:.2f}" if ma5 is not None else "—", "signal": "多头" if ma5 is not None and last_close > ma5 else "空头"},
        {"name": "MA20", "value": f"{ma20:.2f}" if ma20 is not None else "—", "signal": "多头" if ma5 is not None and ma20 is not None and ma5 > ma20 else ("空头" if ma20 is not None else "中性")},
        {"name": "MACD", "value": f"{macd:.3f}" if macd is not None else "—", "signal": "金叉" if macd is not None and macd > 0 else ("死叉" if macd is not None else "中性")},
        {"name": "RSI(14)", "value": f"{rsi:.1f}" if rsi is not None else "—", "signal": "超买" if rsi is not None and rsi >= 70 else ("超卖" if rsi is not None and rsi <= 30 else "中性")},
        {"name": "KDJ-J", "value": f"{kdj_j:.1f}" if kdj_j is not None else "—", "signal": "超买" if kdj_j is not None and kdj_j >= 90 else ("超卖" if kdj_j is not None and kdj_j <= 10 else "中性")},
        {"name": "BOLL", "value": f"{boll_mid:.2f}" if boll_mid is not None else "—", "signal": "多头" if boll_mid is not None and last_close > boll_mid else ("空头" if boll_lower is not None and last_close < boll_lower else "中性")},
    ]


def get_indicators(code: str) -> dict:
    """技术指标汇总（基于近 120 日 K 线）。K 线来源在 provenance 透传。"""
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
        series = _indicator_series(df)
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
            "series": series,
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
            "series": [],
            "trading_date": (kline_result or {}).get("trading_date", ""),
            "elapsed_ms": (kline_result or {}).get("elapsed_ms"),
            "provenance": _enrich(
                make_provenance(source="pandas-ta-classic", endpoint="ta", error=str(e)),
                attempts=[{"source": "kline", "ok": False, "error": str(e)}],
            ),
        }


def get_chart_bundle(code: str, n: int = 120, adjust: str = "qfq") -> dict:
    """返回行情分析页所需数据；K 线只抓取一次并复用于指标计算。"""
    count = max(KLINE_MIN_DAYS, min(int(n or 120), KLINE_MAX_DAYS))
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="chart-bundle") as executor:
        quote_future = executor.submit(get_realtime_quote, code)
        intraday_future = executor.submit(get_intraday, code)
        # 多取 INDICATOR_WARMUP 根用于 MACD/BOLL/RSI 预热，算完指标后再裁到展示区间
        kline_future = executor.submit(get_kline, code, count, adjust, INDICATOR_WARMUP)
        book_future = executor.submit(get_bid_ask, code)
        quote = quote_future.result()
        intraday = intraday_future.result()
        kline = kline_future.result()
        book = book_future.result()
    warmed = kline.get("data") or []
    indicators = compute_indicators_from_kline(code, warmed, kline)
    if indicators.get("series"):
        indicators = {**indicators, "series": indicators["series"][-count:]}
    return {
        "code": to_market_ticker(code),
        "quote": quote,
        "intraday": intraday,
        "kline": {**kline, "data": warmed[-count:]},
        "book": book,
        "indicators": indicators,
    }
