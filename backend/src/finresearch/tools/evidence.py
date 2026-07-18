"""联网证据状态（/api/evidence/status）。

根因修复（#6）：前端"联网证据"之前复用 /api/market/diagnostics（慢、现场探测、AkShare 失败就整块红）。
现在拆出独立、零网络的证据状态接口：默认只报国内证据源（东方财富新闻、行情源），
Google/Serper 明确标成"可选外部增强"，默认关闭。绝不现场探测，绝不卡住前端。
"""

from __future__ import annotations

import logging

from . import market_provider

logger = logging.getLogger(__name__)


def _latest(recent: list, source: str, kinds=None):
    cand = [r for r in recent if r.get("source") == source and (kinds is None or r.get("kind") in kinds)]
    if not cand:
        return None
    return max(cand, key=lambda r: r.get("ts", ""))


def get_evidence_status() -> dict:
    """证据状态：纯配置 + recent_diagnostics 聚合，零网络。"""
    from ..config import settings

    recent = market_provider.recent_diagnostics(30)

    # 国内新闻证据：东方财富个股新闻（akshare stock_news_em）
    news_last = _latest(recent, "akshare", {"news"})
    news_available = bool(news_last and news_last.get("ok"))

    # 行情证据：任一行情源最近成功即视为可用
    mkt_last_ok = any(
        (lr := _latest(recent, s, {"quote", "intraday", "kline", "book"})) and lr.get("ok")
        for s in ("sina", "tencent", "eastmoney-direct", "akshare")
    )
    mkt_last = None
    for s in ("sina", "tencent", "eastmoney-direct", "akshare"):
        lr = _latest(recent, s, {"quote", "intraday", "kline", "book"})
        if lr and (mkt_last is None or lr.get("ts", "") > mkt_last.get("ts", "")):
            mkt_last = lr

    serper_on = bool(settings.enable_serper_search and settings.serper_api_key and settings.serper_api_key.strip())

    return {
        "domestic_news": {
            "name": "东方财富个股新闻（akshare·stock_news_em）",
            "available": news_available,
            "last_checked": (news_last or {}).get("ts", ""),
            "note": "国内默认新闻证据源，免费、免 key；研究证据主要来自此源。",
        },
        "market_source": {
            "name": "行情证据（新浪/腾讯/东财直连 → AkShare 兜底）",
            "last_ok": bool(mkt_last_ok),
            "last_checked": (mkt_last or {}).get("ts", ""),
            "source": (mkt_last or {}).get("source", ""),
        },
        "filings": {
            "available": False,
            "note": "公告/研报源暂未接入；如需更丰富证据可显式开启 Serper。",
        },
        "search": {
            "enabled": serper_on,
            "label": ("已启用：Google/Serper 网页搜索增强" if serper_on
                      else "未启用（国内优先；显式开启后补充公开网页证据）"),
            "provider": "serper" if serper_on else "",
        },
        "summary": (
            "证据主要来自国内免费源（东方财富新闻 + 行情）。"
            + ("已启用 Serper 外部网页搜索增强。" if serper_on else "Serper 外部搜索默认关闭。")
        ),
    }
