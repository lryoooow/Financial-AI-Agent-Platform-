"""新闻：akshare stock_news_em。

返回 [{title, content, time, source}]，用作报告 sources。
"""

from __future__ import annotations

import json
import logging
import re
from typing import List
from urllib.parse import quote

from ..cache import slow_cache
from . import make_provenance
from .entity import normalize_ticker
from .market_provider import record

logger = logging.getLogger(__name__)

# 核验补（B6 遗漏侧）：直连会话复用（连接池 + 统一 UA），不再每次裸 requests.get
# 新建 TCP+TLS。与 fundamentals._direct_session 同款模式。
_DIRECT_SESSION = None


def _direct_session():
    global _DIRECT_SESSION
    if _DIRECT_SESSION is None:
        import requests

        _DIRECT_SESSION = requests.Session()
        _DIRECT_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    return _DIRECT_SESSION


def _news_direct(raw: str, limit: int) -> List[dict]:
    """东方财富新闻直连；请求参数全 ASCII，避免 Windows cp950 对中文路径/参数编码失败。"""
    from .market_provider import configure_market_network

    configure_market_network()
    inner = {
        "uid": "",
        "keyword": raw,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "default", "pageIndex": 1,
            "pageSize": max(1, min(int(limit), 20)), "preTag": "", "postTag": "",
        }},
    }
    callback = "jQuery_finresearch"
    resp = _direct_session().get(
        "https://search-api-web.eastmoney.com/search/jsonp",
        params={"cb": callback, "param": json.dumps(inner, ensure_ascii=True), "_": "1"},
        # HTTP 头只允许 latin-1：中文关键词必须 percent-encode，否则 UnicodeEncodeError
        #（行业研究固定用中文行业名调本接口，不编码则直连路径 100% 失败）。
        headers={"Referer": f"https://so.eastmoney.com/news/s?keyword={quote(raw)}"},
        timeout=6,
    )
    resp.raise_for_status()
    match = re.match(r"^[^(]*\((.*)\)\s*;?\s*$", resp.text, flags=re.S)
    payload = json.loads(match.group(1) if match else resp.text)
    rows = ((payload.get("result") or {}).get("cmsArticleWebOld") or [])[:limit]
    return [{
        "title": re.sub(r"</?em>", "", str(row.get("title") or "")),
        "content": re.sub(r"</?em>", "", str(row.get("content") or ""))[:300],
        "time": str(row.get("date") or ""),
        "url": str(row.get("url") or (f"http://finance.eastmoney.com/a/{row.get('code')}.html" if row.get("code") else "")),
        "source": str(row.get("mediaName") or "东方财富"),
    } for row in rows]


def search_news(code: str, limit: int = 12) -> dict:
    """拉取个股相关新闻（东方财富）。"""
    raw = normalize_ticker(code)
    cache_key = f"news:{raw}:{limit}"
    cached = slow_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        direct_used = False
        try:
            items = _news_direct(raw, int(limit))
            direct_used = bool(items)
        except Exception as direct_error:  # noqa: BLE001
            logger.info("新闻直连失败，回退 AkShare：%s", direct_error)
            items = []
        if not items:
            import akshare as ak
            df = ak.stock_news_em(symbol=raw)
            items = [] if df is None else [{
                "title": str(r.get("新闻标题", "") or ""),
                "content": str(r.get("新闻内容", "") or "")[:300],
                "time": str(r.get("发布时间", "") or ""),
                "url": str(r.get("新闻链接", "") or ""),
                "source": str(r.get("文章来源", "") or "东方财富"),
            } for _, r in df.head(int(limit)).iterrows()]
        if not items:
            raise ValueError("无新闻")
        # v1.6.0（审计 E2）：溯源如实 —— 直连成功就标直连源，别把东财搜索说成 akshare
        #（诊断面板此前据此误报）。
        result = {
            "items": items,
            "provenance": make_provenance(
                source="eastmoney_search" if direct_used else "akshare",
                endpoint="search/jsonp" if direct_used else "stock_news_em",
                raw_value=raw,
            ),
        }
        # 核验补：健康统计渠道与 provenance 同源（直连成功别记成 akshare）
        record("news", "eastmoney_search" if direct_used else "akshare", True)
        slow_cache.set(cache_key, result, ttl=600)
        return result
    except Exception as e:  # noqa: BLE001
        logger.warning("search_news 失败：%s", e)
        record("news", "akshare", False, str(e))
        return {
            "items": [],
            "provenance": make_provenance(
                source="akshare", endpoint="stock_news_em", error=str(e)
            ),
        }
