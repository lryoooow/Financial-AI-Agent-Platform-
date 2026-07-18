"""行情/新闻/搜索 数据源诊断。

两套接口：
- get_market_diagnostics_fast()：零网络。从 recent_diagnostics 环形缓冲聚合每源最近一次成功/失败、
  当前数据源优先级顺序、代理模式、搜索配置。前端默认用这个，绝不会被慢探测卡住（根因 #2）。
- get_market_diagnostics()：现场实时探测（AkShare/Sina/新闻），只在"手动刷新"时调用；
  test_diagnostics_news_probe_reports_failure 直接依赖它，行为保持不变。

绝不抛异常；拿不到就标 unknown，绝不伪造 ok。
"""

from __future__ import annotations

import logging

from . import market_provider

logger = logging.getLogger(__name__)

# 行情源优先级顺序（直连优先，AkShare 兜底）。注意：不同接口精确顺序略有差异
# （quote: sina→tencent→akshare；intraday: sina→eastmoney→akshare；kline: tencent→eastmoney→sina→akshare），
# 此处展示"直连优先、AkShare 兜底"的总体偏好，供前端按源展示状态。
MARKET_SOURCE_ORDER = [
    {"name": "sina", "label": "新浪直连", "priority": 1},
    {"name": "tencent", "label": "腾讯直连", "priority": 2},
    {"name": "eastmoney-direct", "label": "东方财富直连", "priority": 3},
    {"name": "akshare", "label": "AkShare(东财)", "priority": 4},
]
_MARKET_KINDS = {"quote", "intraday", "kline", "book", "indicators"}


def _latest(recent: list, source: str, kinds=None):
    """从环形缓冲取某源（可限 kind）最近一条；按 ts 字符串比较（ISO 格式可比较）。"""
    cand = [r for r in recent if r.get("source") == source and (kinds is None or r.get("kind") in kinds)]
    if not cand:
        return None
    return max(cand, key=lambda r: r.get("ts", ""))


def get_market_diagnostics_fast() -> dict:
    """快速诊断：零网络，纯缓冲聚合。冷启动（缓冲空）→ 全部 unknown。"""
    from ..config import settings

    recent = market_provider.recent_diagnostics(30)
    sources = []
    any_ok = False
    for ms in MARKET_SOURCE_ORDER:
        last = _latest(recent, ms["name"], _MARKET_KINDS)
        status = "unknown" if not last else ("ok" if last.get("ok") else "fail")
        if status == "ok":
            any_ok = True
        sources.append({
            "name": ms["name"], "label": ms["label"], "priority": ms["priority"],
            "status": status, "last_ts": (last or {}).get("ts", ""),
            "error": (last or {}).get("error", "") if last and not last.get("ok") else "",
        })
    news_last = _latest(recent, "akshare", {"news"})
    news_status = "unknown" if not news_last else ("ok" if news_last.get("ok") else "fail")

    serper_on = bool(settings.enable_serper_search and settings.serper_api_key and settings.serper_api_key.strip())
    last_any_ts = max((r.get("ts", "") for r in recent), default="")

    return {
        "market": {
            "sources": sources,
            "available": any_ok,
            "aggregated_from": "recent_diagnostics",
            "last_any_ts": last_any_ts,
        },
        "news": {
            "name": "akshare · stock_news_em（东方财富个股新闻）",
            "available": news_status == "ok",
            "status": news_status,
            "last_ts": (news_last or {}).get("ts", ""),
            "error": (news_last or {}).get("error", "") if news_last and not news_last.get("ok") else "",
            "note": "国内默认新闻证据源；不依赖 Serper。",
        },
        "search": {
            "name": "Serper/Google（可选外部增强）",
            "configured": serper_on,
            "status": ("已显式启用：真实 Google/Serper 网页搜索" if serper_on
                       else "默认关闭（国内优先）；显式开启 ENABLE_SERPER_SEARCH+SERPER_API_KEY 后启用"),
        },
        "proxy": market_provider.proxy_status(),
        "recent": recent[:12],
        "fresh": False,  # 缓冲派生，非实时探测
    }


# ────────────────────────────────────────────────────────────
# 实时探测（仅手动刷新调用；test_diagnostics_news_probe_reports_failure 依赖其行为）
# ────────────────────────────────────────────────────────────
def _probe_http(source: str, name: str, url: str, headers: dict, *, decode="utf-8", expect, timeout=4) -> dict:
    import urllib.request as _u
    try:
        req = _u.Request(url, headers=headers)
        with _u.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode(decode, errors="replace")
        ok = bool(body) and expect(body)
        return {"source": source, "name": name, "ok": ok, "error": None if ok else "响应异常"}
    except Exception as e:  # noqa: BLE001
        return {"source": source, "name": name, "ok": False, "error": str(e)[:160]}


def _probe_akshare() -> dict:
    try:
        import akshare as ak
        df = ak.stock_individual_info_em(symbol="600519")
        ok = df is not None and not df.empty
        return {"source": "akshare", "name": "akshare · 东方财富", "ok": ok, "error": None if ok else "空数据"}
    except Exception as e:  # noqa: BLE001
        return {"source": "akshare", "name": "akshare · 东方财富", "ok": False, "error": str(e)[:160]}


def _probe_sina() -> dict:
    return _probe_http(
        "sina", "sina · hq.sinajs.cn", "http://hq.sinajs.cn/list=sh000001",
        {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0 JingYuan/1.0"},
        decode="gbk", expect=lambda b: "hq_str" in b,
    )


def _probe_tencent() -> dict:
    return _probe_http(
        "tencent", "tencent · qt.gtimg.cn", "http://qt.gtimg.cn/q=sh000001",
        {"Referer": "https://stockapp.finance.qq.com/", "User-Agent": "Mozilla/5.0 JingYuan/1.0"},
        decode="gbk", expect=lambda b: "~" in b,
    )


def _probe_eastmoney() -> dict:
    return _probe_http(
        "eastmoney-direct", "eastmoney · push2his", "http://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001&fields1=f1&fields2=f51&klt=101&fqt=1&beg=20200101&end=20990101",
        {"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0 JingYuan/1.0"},
        expect=lambda b: "klines" in b,
    )


def _probe_news() -> dict:
    try:
        import akshare as ak
        df = ak.stock_news_em(symbol="600519")
        ok = df is not None and not df.empty
        return {"name": "akshare · stock_news_em", "available": ok,
                "note": "个股新闻默认免费源（东方财富）；不依赖 Serper。",
                "error": None if ok else "空数据"}
    except Exception as e:  # noqa: BLE001
        return {"name": "akshare · stock_news_em", "available": False,
                "note": "个股新闻默认免费源（东方财富）；不依赖 Serper。",
                "error": str(e)[:160]}


def get_market_diagnostics() -> dict:
    """实时探测聚合（慢；仅手动刷新）。全部 4 个行情源 + 新闻回写环形缓冲，后续 fast 调用即可反映。"""
    from ..config import settings
    serper_on = bool(settings.enable_serper_search and settings.serper_api_key and settings.serper_api_key.strip())
    primary = _probe_akshare()
    backup = _probe_sina()
    tx = _probe_tencent()
    em = _probe_eastmoney()
    news = _probe_news()
    # 全部 4 个行情源 + 新闻回写环形缓冲（用显式 source key，避免依赖 name 字符串解析）
    for p in (primary, backup, tx, em):
        market_provider.record("quote", p["source"], p["ok"], p.get("error") or None)
    market_provider.record("news", "akshare", bool(news.get("available")), news.get("error") or None)
    return {
        "market": {
            "primary": primary, "backup": backup,
            "available": bool(primary["ok"] or backup["ok"] or tx["ok"] or em["ok"]),
        },
        "news": news,
        "search": {
            "name": "Serper/Google（可选外部增强）",
            "configured": serper_on,
            "status": ("已显式启用：真实 Google/Serper 网页搜索" if serper_on
                       else "默认关闭（国内优先）；显式开启 ENABLE_SERPER_SEARCH+SERPER_API_KEY 后启用"),
        },
        "proxy": market_provider.proxy_status(),
        "recent": market_provider.recent_diagnostics(20),
        "fresh": True,
    }
