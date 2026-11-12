"""统一行情 provider 层 + 网络代理策略 + 失败诊断记录。

设计（对应根因 1：akshare→东方财富继承系统/环境代理，远端断开）：
- 主源 akshare（东方财富），备用免费源 Sina（hq.sinajs.cn）兜底；绝不伪造数据。
- 代理策略：默认把东方财富/Sina/Tencent 行情域名写入 NO_PROXY，绕过坏的系统/环境代理；
  MARKET_PROXY 显式设置时则让行情走该代理。configure_market_network() 只动 os.environ，
  不导入任何重型库，因此在 uvicorn/akshare 首次请求前调用即可生效。
- backup fetcher 全部用 stdlib urllib（零新依赖）；只取字段映射确定的值，拿不到就抛错，
  由上层 market.py 捕获后降级——绝不在本层返回半真半假的数据。
- 诊断环形缓冲：record() 记录最近 N 次 {kind, source, ok, error}，供 /api/market/diagnostics 暴露。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from threading import Lock
from typing import Any, List, Optional
from urllib.parse import urlencode

from . import now_cst, now_iso
from .entity import BJ_PREFIXES, SH_PREFIXES, _classify_exchange, normalize_ticker, to_market_ticker

logger = logging.getLogger(__name__)
_NET_LOCK = Lock()

# 行情域名白名单：默认对这些主机绕过代理（requests/urllib 读 NO_PROXY）。
# 东方财富（akshare 主源）、Sina（备用源）、腾讯（预留）、乐咕乐股（申万估值）。
MARKET_HOSTS = [
    # 东方财富（akshare push 接口）
    "push2.eastmoney.com",
    "push2his.eastmoney.com",
    "82.push2.eastmoney.com",
    "push2delay.eastmoney.com",
    "datacenter-web.eastmoney.com",
    "datacenter.eastmoney.com",
    "quote.eastmoney.com",
    "search-api-web.eastmoney.com",
    "so.eastmoney.com",
    # Sina（备用源）
    "hq.sinajs.cn",
    "hq.sinajs.com.cn",
    "quotes.sina.cn",
    "money.finance.sina.com.cn",
    "finance.sina.com.cn",
    "vip.stock.finance.sina.com.cn",
    # 腾讯
    "qt.gtimg.cn",
    "web.ifzq.gtimg.cn",
    "web.sqt.gtimg.cn",
    "stock.gtimg.cn",
    # 乐咕乐股（申万行业估值底层源）
    "legulegu.com",
]

# 诊断环形缓冲（进程内单例，家用单进程足够）
_DIAG: List[dict] = []
_DIAG_LOCK = Lock()
_DIAG_CAP = 30


def record(kind: str, source: str, ok: bool, error: Optional[str] = None) -> None:
    """记录一次行情/新闻/搜索尝试，供诊断接口暴露最近失败链路。"""
    entry = {
        "kind": kind,        # quote / intraday / kline / book / news / search
        "source": source,    # akshare / sina / serper / ...
        "ok": bool(ok),
        "error": (str(error)[:200]) if error else "",
        "ts": now_iso(),
    }
    with _DIAG_LOCK:
        _DIAG.append(entry)
        if len(_DIAG) > _DIAG_CAP:
            del _DIAG[: len(_DIAG) - _DIAG_CAP]


def recent_diagnostics(limit: int = 20) -> List[dict]:
    with _DIAG_LOCK:
        return list(_DIAG[-limit:])


def proxy_status() -> dict:
    """只读返回当前行情代理策略（脱敏：不直接回显用户原始 NO_PROXY 环境变量内容）。"""
    import os

    from ..config import settings

    raw_np = os.environ.get("NO_PROXY", "") or os.environ.get("no_proxy", "")
    items = [h.strip() for h in raw_np.split(",") if h.strip()]
    bypass_on = bool(settings.market_bypass_proxy)
    return {
        "bypass_enabled": bypass_on,
        "market_proxy_set": bool(settings.market_proxy),
        "no_proxy": f"已启用（覆盖 {len(MARKET_HOSTS)} 个行情直连源，共 {len(items)} 项）" if bypass_on else "(未启用)",
        "no_proxy_count": len(items),
        "monitored_hosts": list(MARKET_HOSTS),
    }


def clear_diagnostics() -> None:
    with _DIAG_LOCK:
        _DIAG.clear()


def configure_market_network() -> dict:
    """在进程启动早期（uvicorn/akshare 首次请求前）配置行情网络代理策略。"""
    import os
    import sys

    from ..config import settings

    with _NET_LOCK:
        summary: dict[str, Any] = {
            "bypass_proxy": bool(settings.market_bypass_proxy),
            "market_proxy_set": bool(settings.market_proxy),
        }

        # ① 注册表代理物化（仅 Windows 且 env 未显式配置代理时）
        if sys.platform == "win32" and not (os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")):
            try:
                import urllib.request

                reg = urllib.request.getproxies_registry()
                for scheme in ("http", "https"):
                    p = reg.get(scheme) or (reg.get("https") if scheme == "http" else None)
                    if p:
                        os.environ.setdefault(scheme.upper() + "_PROXY", p)
                        summary.setdefault("materialized_registry_proxy", p)
            except Exception:  # noqa: BLE001
                pass

        if settings.market_bypass_proxy:
            existing = ",".join([os.environ.get("NO_PROXY", ""), os.environ.get("no_proxy", "")])
            parts = [h.strip() for h in existing.split(",") if h.strip()]
            parts = [h[2:] if h.startswith("*.") else h for h in parts]
            parts += [h for h in MARKET_HOSTS if h not in parts]
            val = ",".join(dict.fromkeys(parts))  # 去重保序
            os.environ["NO_PROXY"] = val
            os.environ["no_proxy"] = val
            summary["no_proxy"] = val
        else:
            summary["no_proxy"] = "(bypass disabled)"

        if settings.market_proxy:
            os.environ["HTTP_PROXY"] = settings.market_proxy
            os.environ["HTTPS_PROXY"] = settings.market_proxy
            summary["applied_proxy"] = settings.market_proxy

        return summary


# ────────────────────────────────────────────────────────────
# 市场前缀（Sina/Sina kline 用 sh/sz/bj 前缀）
# ────────────────────────────────────────────────────────────
def _sina_symbol(raw: str) -> str:
    """纯 6 位代码 → Sina 前缀（sh/sz/bj）。"""
    if raw.startswith(SH_PREFIXES):
        return f"sh{raw}"
    if raw.startswith(BJ_PREFIXES):
        return f"bj{raw}"
    return f"sz{raw}"


def _eastmoney_secid(raw: str) -> str:
    """Eastmoney secid: 1.SH, 0.SZ/BJ."""
    return f"1.{raw}" if raw.startswith(SH_PREFIXES) else f"0.{raw}"


def _http_get(url: str, headers: Optional[dict] = None, timeout: float = 3.0) -> str:
    """stdlib GET，返回响应体文本。网络/HTTP 错一律抛异常给上层捕获。"""
    import urllib.request as _u

    req = _u.Request(url, headers=headers or {})
    with _u.urlopen(req, timeout=timeout) as resp:  # socket.setdefaulttimeout 已兜底
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def eastmoney_kline(code: str, n: int = 60, adjust: str = "qfq") -> dict:
    """东方财富直连日 K，绕过 AkShare 封装层，国内免费源。"""
    raw = normalize_ticker(code)
    fqt = {"": "0", "qfq": "1", "hfq": "2"}.get(adjust, "1")
    now = now_cst()
    fetch_n = max(1, int(n)) + 25
    end = now.strftime("%Y%m%d")
    start = (now - dt.timedelta(days=fetch_n * 3 + 90)).strftime("%Y%m%d")
    qs = urlencode(
        {
            "secid": _eastmoney_secid(raw),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": "101",
            "fqt": fqt,
            "beg": start,
            "end": end,
        }
    )
    url = f"http://push2his.eastmoney.com/api/qt/stock/kline/get?{qs}"
    body = _http_get(url, headers={"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0 JingYuan/1.0"}, timeout=3)
    payload = json.loads(body)
    rows = ((payload.get("data") or {}).get("klines") or [])[-fetch_n:]
    if not rows:
        raise ValueError("Eastmoney 日K 空数据")

    import pandas as pd

    parsed = []
    for line in rows:
        p = str(line).split(",")
        if len(p) < 6:
            continue
        parsed.append({"date": p[0], "open": _n(p[1]), "close": _n(p[2]), "high": _n(p[3]), "low": _n(p[4]), "volume": int(_n(p[5]))})
    if not parsed:
        raise ValueError("Eastmoney 日K 字段异常")
    df = pd.DataFrame(parsed)
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df = df.tail(int(n)).reset_index(drop=True)
    out = []
    for _, r in df.iterrows():
        out.append(
            {
                "date": str(r["date"])[:10],
                "open": _n(r["open"]),
                "close": _n(r["close"]),
                "high": _n(r["high"]),
                "low": _n(r["low"]),
                "volume": int(_n(r["volume"])),
                "ma5": _opt_n(r["ma5"]),
                "ma10": _opt_n(r["ma10"]),
                "ma20": _opt_n(r["ma20"]),
            }
        )
    return {
        "data": out,
        "trading_date": out[-1]["date"],
        "provenance": {
            "source": "eastmoney-direct",
            "endpoint": "push2his.eastmoney.com/api/qt/stock/kline/get",
            "fetched_at": now_iso(),
            "raw_value": {"secid": _eastmoney_secid(raw), "n": n, "adjust": adjust},
            "error": None,
        },
    }


def eastmoney_intraday(code: str) -> dict:
    """东方财富直连 1 分钟分时，国内免费源。"""
    raw = normalize_ticker(code)
    qs = urlencode(
        {
            "secid": _eastmoney_secid(raw),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "ndays": "1",
            "iscr": "0",
            "iscca": "0",
        }
    )
    url = f"http://push2his.eastmoney.com/api/qt/stock/trends2/get?{qs}"
    body = _http_get(url, headers={"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0 JingYuan/1.0"}, timeout=3)
    payload = json.loads(body)
    rows = (payload.get("data") or {}).get("trends") or []
    if not rows:
        raise ValueError("Eastmoney 分时空数据")

    import pandas as pd

    parsed = []
    for line in rows:
        p = str(line).split(",")
        if len(p) < 7:
            continue
        parsed.append({"dt": p[0], "price": _n(p[2]), "volume": _n(p[5]), "amount": _n(p[6])})
    df = pd.DataFrame(parsed)
    if df.empty:
        raise ValueError("Eastmoney 分时字段异常")
    df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["dt"]).sort_values("dt")
    if df.empty:
        raise ValueError("Eastmoney 分时无有效时间字段")
    latest = df["dt"].iloc[-1].date()
    df = df[df["dt"].dt.date == latest]
    now = now_cst()
    if latest == now.date():
        df = df[df["dt"] <= now]
    hhmm = df["dt"].dt.strftime("%H:%M")
    df = df[(hhmm >= "09:30") & (hhmm <= "15:00")]
    if df.empty:
        raise ValueError("Eastmoney 分时当前尚无交易时段有效点")
    # 1 分钟 → 5 分钟桶（与 akshare/sina 分时 5 分钟契约一致；avg 用累计均价，零成交量回落现价）
    df = df.set_index("dt")
    bucket = df.resample("5min", label="left", closed="left")
    agg = bucket.agg(price=("price", "last"), volume=("volume", "sum")).dropna(subset=["price"])
    agg["time"] = agg.index.strftime("%H:%M")
    cum_p5 = (agg["price"] * agg["volume"]).cumsum()
    cum_v5 = agg["volume"].cumsum()
    agg["avg"] = [
        round(float(cp / cv), 2) if cv > 0 else round(float(pr), 2)
        for cp, cv, pr in zip(cum_p5, cum_v5, agg["price"])
    ]
    out = [
        {"time": str(r["time"]), "price": _n(r["price"]), "avg": _n(r["avg"]), "volume": int(_n(r["volume"]))}
        for _, r in agg.iterrows()
    ]
    if not out:
        raise ValueError("Eastmoney 分时重采样为空")
    return {
        "data": out,
        "trading_date": str(latest),
        "provenance": {
            "source": "eastmoney-direct",
            "endpoint": "push2his.eastmoney.com/api/qt/stock/trends2/get",
            "fetched_at": now_iso(),
            "raw_value": _eastmoney_secid(raw),
            "error": None,
        },
    }


# ────────────────────────────────────────────────────────────
# 备用源 1：Sina 实时快照（hq.sinajs.cn，GBK 编码）
# ────────────────────────────────────────────────────────────
_HQ_RE = re.compile(r'="([^"]*)"')


def sina_realtime(code: str) -> dict:
    """Sina 实时快照（前端 Stock 形状，source='sina'）。

    hq.sinajs.cn 返回 GBK 文本：var hq_str_sh600519="名称,今开,昨收,最新,最高,最低,...,成交量(股),成交额(元),...,日期,时间,...";
    逗号字段索引（0 起）：[0]名 [1]今开 [2]昨收 [3]最新价 [4]最高 [5]最低
                        [8]成交量(股,需/100 得手) [9]成交额(元) [30]日期 [31]时间
    需要 Referer + User-Agent，否则 403/空。只取映射确定的字段，绝不臆造。
    """
    raw = normalize_ticker(code)
    sym = _sina_symbol(raw)
    import urllib.request as _u

    url = f"http://hq.sinajs.cn/list={sym}"
    req = _u.Request(
        url,
        headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JingYuan/1.0",
        },
    )
    with _u.urlopen(req, timeout=3) as resp:
        body = resp.read().decode("gbk", errors="replace")
    m = _HQ_RE.search(body)
    if not m:
        raise ValueError(f"Sina 未返回行情（{sym}）：{body[:60]}")
    payload = m.group(1)
    if not payload.strip():
        raise ValueError(f"Sina 行情为空（{sym}，可能代码无效或停牌）")
    parts = payload.split(",")
    if len(parts) < 32:
        raise ValueError(f"Sina 字段数异常（{sym}）：{len(parts)}")

    def _f(v, d=0.0):
        try:
            x = float(v)
            return d if (x != x or x in (float("inf"), float("-inf"))) else x  # NaN/inf guard
        except Exception:
            return d

    name = parts[0].strip()
    price = _f(parts[3])
    prev_close = _f(parts[2])
    open_p = _f(parts[1])
    high = _f(parts[4])
    low = _f(parts[5])
    if price <= 0:
        raise ValueError(f"Sina 最新价无效（{sym}，price={parts[3]}，可能停牌）")

    change = price - prev_close
    change_pct = (change / prev_close * 100.0) if prev_close else 0.0
    vol_shares = _f(parts[8])
    amount = _f(parts[9])
    trading_date = parts[30].strip()
    trading_time = parts[31].strip()

    return {
        "code": to_market_ticker(raw),
        "name": name,
        "exchange": _classify_exchange(raw),
        "price": round(price, 3),
        "change": round(change, 3),
        "changePct": round(change_pct, 2),
        "open": open_p,
        "high": high,
        "low": low,
        "prevClose": prev_close,
        "volume": _fmt_volume(vol_shares / 100.0),  # 股 → 手
        "turnover": _fmt_amount(amount),
        "marketCap": "—",  # Sina hq 无市值字段，诚实留空
        "pe": 0.0,
        "pb": 0.0,
        "roe": 0.0,
        "industry": "",
        "as_of": f"{trading_date} {trading_time}".strip(),
        "trading_date": trading_date,
        "provenance": {
            "source": "sina",
            "endpoint": "hq.sinajs.cn",
            "fetched_at": now_iso(),
            "raw_value": sym,
            "error": None,
        },
    }


def tencent_realtime(code: str) -> dict:
    """Tencent qt.gtimg.cn 实时快照，国内免费兜底源。"""
    raw = normalize_ticker(code)
    sym = _sina_symbol(raw)
    import urllib.request as _u

    req = _u.Request(
        f"http://qt.gtimg.cn/q={sym}",
        headers={"Referer": "https://stockapp.finance.qq.com/", "User-Agent": "Mozilla/5.0 JingYuan/1.0"},
    )
    with _u.urlopen(req, timeout=3) as resp:
        body = resp.read().decode("gbk", errors="replace")
    if "~" not in body:
        raise ValueError(f"Tencent 未返回行情（{sym}）：{body[:60]}")
    parts = body.split("~")
    if len(parts) < 40:
        raise ValueError(f"Tencent 字段数异常（{sym}）：{len(parts)}")

    price = _n(parts[3])
    prev_close = _n(parts[4])
    if price <= 0:
        raise ValueError(f"Tencent 最新价无效（{sym}，price={parts[3]}）")
    ts = parts[30]
    trading_date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else ""
    trading_time = f"{ts[8:10]}:{ts[10:12]}:{ts[12:14]}" if len(ts) >= 14 else ""
    amount_yuan = _n(parts[37]) * 10000.0

    return {
        "code": to_market_ticker(raw),
        "name": parts[1].strip(),
        "exchange": _classify_exchange(raw),
        "price": price,
        "change": _n(parts[31], price - prev_close),
        "changePct": _n(parts[32]),
        "open": _n(parts[5]),
        "high": _n(parts[33]),
        "low": _n(parts[34]),
        "prevClose": prev_close,
        "volume": _fmt_volume(_n(parts[36])),
        "turnover": _fmt_amount(amount_yuan),
        "marketCap": _fmt_amount(_n(parts[45]) * 1e8) if (len(parts) > 45 and _n(parts[45]) > 0) else "—",
        "pe": _n(parts[39]) if len(parts) > 39 else 0.0,
        "pb": _n(parts[46]) if len(parts) > 46 else 0.0,
        "roe": 0.0,
        "industry": "",
        "as_of": f"{trading_date} {trading_time}".strip(),
        "trading_date": trading_date,
        "provenance": {
            "source": "tencent",
            "endpoint": "qt.gtimg.cn",
            "fetched_at": now_iso(),
            "raw_value": sym,
            "error": None,
        },
    }


def tencent_kline(code: str, n: int = 60, adjust: str = "qfq") -> dict:
    """Tencent qfq/hfq/day K line, domestic free fallback."""
    raw = normalize_ticker(code)
    sym = _sina_symbol(raw)
    key, fq = {"qfq": ("qfqday", "qfq"), "hfq": ("hfqday", "hfq"), "": ("day", "")}.get(adjust, ("qfqday", "qfq"))
    fetch_n = max(1, int(n)) + 25
    url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{fetch_n},{fq}"
    body = _http_get(
        url,
        headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0 JingYuan/1.0"},
        timeout=3,
    )
    payload = json.loads(body)
    if payload.get("code") != 0:
        raise ValueError(f"Tencent K线响应异常：{payload.get('msg')}")
    rows = (((payload.get("data") or {}).get(sym) or {}).get(key) or [])[-fetch_n:]
    # v1.6.3（B3）：北交所符号腾讯只回 'day' 键（不复权），即便请求了 qfq/hfq。
    bj_unadjusted = False
    if not rows and key != "day" and raw.startswith(BJ_PREFIXES):
        rows = (((payload.get("data") or {}).get(sym) or {}).get("day") or [])[-fetch_n:]
        bj_unadjusted = True
    if not rows:
        raise ValueError("Tencent 日K 空数据")

    import pandas as pd

    parsed = []
    for r in rows:
        if len(r) < 6:
            continue
        parsed.append(
            {
                "date": str(r[0])[:10],
                "open": _n(r[1]),
                "close": _n(r[2]),
                "high": _n(r[3]),
                "low": _n(r[4]),
                "volume": int(_n(r[5])),
            }
        )
    if not parsed:
        raise ValueError("Tencent 日K 字段异常")
    df = pd.DataFrame(parsed)
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df = df.tail(int(n)).reset_index(drop=True)
    out = []
    for _, r in df.iterrows():
        out.append(
            {
                "date": str(r["date"]),
                "open": _n(r["open"]),
                "close": _n(r["close"]),
                "high": _n(r["high"]),
                "low": _n(r["low"]),
                "volume": int(_n(r["volume"])),
                "ma5": _opt_n(r["ma5"]),
                "ma10": _opt_n(r["ma10"]),
                "ma20": _opt_n(r["ma20"]),
            }
        )
    return {
        "data": out,
        "trading_date": out[-1]["date"],
        "provenance": {
            "source": "tencent",
            "endpoint": "web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            "fetched_at": now_iso(),
            "raw_value": {"symbol": sym, "n": n,
                          "adjust": "none" if bj_unadjusted else adjust},
            "error": None,
            **({"note": "北交所腾讯源仅提供不复权日K（已如实降级标注），技术指标基于不复权价计算"}
               if bj_unadjusted else {}),
        },
    }


# ────────────────────────────────────────────────────────────
# 备用源 2：Sina 日 K（getKLineData，scale=240）
# ────────────────────────────────────────────────────────────
def sina_kline(code: str, n: int = 60) -> dict:
    """Sina 日 K（不复权）。返回与 akshare kline 同构的 {data:[...], provenance}。"""
    raw = normalize_ticker(code)
    sym = _sina_symbol(raw)
    fetch_n = max(1, int(n)) + 25
    qs = urlencode({"symbol": sym, "scale": 240, "ma": "no", "datalen": fetch_n})
    url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?{qs}"
    headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0 JingYuan/1.0"}
    body = _http_get(url, headers=headers)
    rows = json.loads(body)
    if not isinstance(rows, list) or not rows:
        raise ValueError("Sina 日K 空数据")

    import pandas as pd

    df = pd.DataFrame(rows)
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    df["volume"] = pd.to_numeric(df.get("volume"), errors="coerce").fillna(0) / 100.0  # 股→手
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df = df.tail(int(n)).reset_index(drop=True)
    out = []
    for _, r in df.iterrows():
        out.append(
            {
                "date": str(r.get("day", ""))[:10],
                "open": _n(r.get("open")),
                "close": _n(r.get("close")),
                "high": _n(r.get("high")),
                "low": _n(r.get("low")),
                "volume": int(r.get("volume") or 0),
                "ma5": _opt_n(r.get("ma5")),
                "ma10": _opt_n(r.get("ma10")),
                "ma20": _opt_n(r.get("ma20")),
            }
        )
    return {
        "data": out,
        "trading_date": (out[-1]["date"] if out else ""),
        "provenance": {
            "source": "sina",
            "endpoint": "CN_MarketData.getKLineData",
            "fetched_at": now_iso(),
            "raw_value": {"symbol": sym, "n": n, "adjust": "none"},
            "error": None,
        },
    }


# ────────────────────────────────────────────────────────────
# 备用源 3：Sina 5 分钟分时（getKLineData，scale=5）
# ────────────────────────────────────────────────────────────
def sina_intraday(code: str) -> dict:
    """Sina 5 分钟分时（仅最新交易日，带累计均价线）。"""
    raw = normalize_ticker(code)
    sym = _sina_symbol(raw)
    qs = urlencode({"symbol": sym, "scale": 5, "ma": "no", "datalen": 48})
    url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?{qs}"
    headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0 JingYuan/1.0"}
    body = _http_get(url, headers=headers)
    rows = json.loads(body)
    if not isinstance(rows, list) or not rows:
        raise ValueError("Sina 分时空数据")

    import pandas as pd

    df = pd.DataFrame(rows)
    df["dt"] = pd.to_datetime(df["day"], errors="coerce")
    df = df.dropna(subset=["dt"]).sort_values("dt")
    if df.empty:
        raise ValueError("Sina 分时无有效时间字段")
    latest = df["dt"].iloc[-1].date()
    df = df[df["dt"].dt.date == latest]
    now = now_cst()
    if latest == now.date():
        df = df[df["dt"] <= now]
    df["price"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["price"])
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0) / 100.0  # 股→手
    hhmm = (df["dt"].dt.strftime("%H:%M"))
    df = df[(hhmm >= "09:30") & (hhmm <= "15:00")]
    if df.empty:
        # 09:30–09:34 首根右标 5 分钟柱尚未闭合时，抛异常让上层自动降级到 1 分钟分时源
        raise ValueError("Sina 5分钟分时当前尚无已闭合采样点")
    cum_p = (df["price"] * df["volume"]).cumsum()
    cum_v = df["volume"].cumsum()
    df["avg"] = [
        round(float(cp / cv), 2) if cv > 0 else round(float(pr), 2)
        for cp, cv, pr in zip(cum_p, cum_v, df["price"])
    ]
    out = [
        {
            "time": str(r["dt"].strftime("%H:%M")),
            "price": _n(r["price"]),
            "avg": _n(r["avg"]),
            "volume": int(r["volume"] or 0),
        }
        for _, r in df.iterrows()
    ]
    return {
        "data": out,
        "trading_date": str(latest),
        "provenance": {
            "source": "sina",
            "endpoint": "CN_MarketData.getKLineData(scale=5)",
            "fetched_at": now_iso(),
            "raw_value": sym,
            "error": None,
        },
    }


# ────────────────────────────────────────────────────────────
# 数值/格式小工具（与 market.py 同口径）
# ────────────────────────────────────────────────────────────
def _n(v, d=0.0) -> float:
    try:
        x = float(v)
        return d if (x != x or x in (float("inf"), float("-inf"))) else x
    except Exception:
        return d


def _opt_n(v) -> Optional[float]:
    """均线等可缺失指标：NaN/None 返回 None（JSON null），绝不伪装成 0.00 元。"""
    try:
        if v is None:
            return None
        x = float(v)
        if x != x or x in (float("inf"), float("-inf")):
            return None
        return round(x, 2)
    except Exception:
        return None


def _fmt_volume(vol_hand: float) -> str:
    v = _n(vol_hand)
    if v >= 1e8:
        return f"{v/1e8:.2f}亿手"
    if v >= 1e4:
        return f"{v/1e4:.2f}万手"
    return f"{int(round(v))}手"


def _fmt_amount(amount_yuan: float) -> str:
    a = _n(amount_yuan)
    if a >= 1e8:
        return f"{a/1e8:.2f}亿"
    if a >= 1e4:
        return f"{a/1e4:.2f}万"
    return f"{a:.0f}"
