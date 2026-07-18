"""research 节点：公司概况 + 财务指标 + 新闻 + 联网搜索。"""

from __future__ import annotations

import asyncio
import logging
import time

from .._utils import _timed_call
from ...tools.fundamentals import get_company_profile, get_financial_indicators
from ...tools.news import search_news
from ...tools.search import web_search, is_configured as serper_configured
from ..state import ResearchState

logger = logging.getLogger(__name__)


async def research_node(state: ResearchState) -> dict:
    t0 = time.perf_counter()
    code = state.get("normalized_ticker") or state.get("code", "")
    profile_fallback = {
        "name": "",
        "industry": "",
        "market_cap": "",
        "provenance": {"source": "akshare", "endpoint": "stock_individual_info_em", "error": "timeout_or_failed"},
    }
    fins_fallback = {
        "financials": {"years": [], "rows": []},
        "provenance": {"source": "akshare", "endpoint": "stock_financial_analysis_indicator", "error": "timeout_or_failed"},
    }
    news_fallback = {
        "items": [],
        "provenance": {"source": "akshare", "endpoint": "stock_news_em", "error": "timeout_or_failed"},
    }

    # 联网搜索无需等待公司概况返回；先用证券代码发起，避免原先 profile + search 的串行等待。
    code_query = code.split(".", 1)[0] if code else code
    ws_query = f"{code_query} 股票 最新动态".strip()
    web_fallback = {
        "items": [],
        "provenance": {"source": "serper", "endpoint": "search", "error": "timeout_or_failed"},
    }
    profile_res, fins_res, news_res, web_res = await asyncio.gather(
        _timed_call(get_company_profile, code, timeout=6, label="company_profile", fallback=profile_fallback),
        # 15s 不是拍的：实测冷启动稳定 10.6s（akshare 串行抓 7 页 × 1.24s，见 stock_financial_
        # analysis_indicator 的分页进度）。原值 8s 卡在数据到达之前，于是**每次冷缓存必丢财务**——
        # slow_cache 是纯内存 TTL，进程一重启就空，等于每次开软件后的第一份报告都没有
        # PE/PB/ROE/营收同比，摘要里一排"（未获取）"。这不是超时保护，是自己掐自己。
        # 其余三源实测 3.3s / 2.1s / 0.3s，余量充足，不动。
        _timed_call(get_financial_indicators, code, timeout=15, label="financial_indicators", fallback=fins_fallback),
        _timed_call(search_news, code, timeout=6, label="news", fallback=news_fallback, limit=12),
        _timed_call(web_search, ws_query, timeout=5, label="web_search", fallback=web_fallback, limit=8),
    )
    profile, profile_ms, profile_err = profile_res
    fins, fins_ms, fins_err = fins_res
    news, news_ms, news_err = news_res
    web, web_ms, web_err = web_res
    name = profile.get("name", "") or ""

    findings = [
        {
            "key": "profile",
            "title": "公司概况",
            "name": name,
            "industry": profile.get("industry", ""),
            "market_cap": profile.get("market_cap", ""),
            "provenance": profile.get("provenance"),
        },
        {
            "key": "financials",
            "title": "财务指标",
            "financials": fins.get("financials", {"years": [], "rows": []}),
            "provenance": fins.get("provenance"),
        },
        {
            "key": "news",
            "title": "相关新闻",
            "items": news.get("items", []),
            "provenance": news.get("provenance"),
        },
        {
            "key": "web_search",
            "title": "联网搜索",
            "items": web.get("items", []),
            "configured": serper_configured(),
            "provenance": web.get("provenance"),
        },
    ]

    # 来源 citations（新闻证据 + 公开信息证据 + 财务接口）
    citations = []
    for n in news.get("items", [])[:6]:
        citations.append(
            {
                "title": "[新闻证据] " + n.get("title", ""),
                "url": n.get("url", ""),
                "date": n.get("time", ""),
                "kind": "news",
            }
        )
    for w in web.get("items", [])[:6]:
        citations.append(
            {
                "title": "[公开信息] " + w.get("title", ""),
                "url": w.get("url", ""),
                "date": w.get("time", ""),
                "kind": "web",
            }
        )
    citations.append(
        {
            "title": f"[行情事实/财务] {name or code} 财务分析指标 · akshare",
            "url": "stock_financial_analysis_indicator",
            "date": state.get("data_as_of", ""),
            "kind": "fundamentals",
        }
    )

    return {
        "findings": findings,
        "citations": citations,
        "timings": {
            "research_ms": int((time.perf_counter() - t0) * 1000),
            "profile_ms": profile_ms,
            "profile_ok": profile_err is None,
            "financials_ms": fins_ms,
            "financials_ok": fins_err is None,
            "news_ms": news_ms,
            "news_ok": news_err is None,
            "web_search_ms": web_ms,
            "web_search_ok": web_err is None,
        },
    }
