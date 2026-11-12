"""新闻：akshare stock_news_em。

返回 [{title, content, time, source}]，用作报告 sources。
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
import threading
from typing import List
from urllib.parse import quote

from ..cache import slow_cache
from . import make_provenance
from .entity import normalize_ticker
from .market_provider import record

logger = logging.getLogger(__name__)

# 线程局部直连会话（requests.Session 非线程安全；并发线程下各自独立复用连接池 + 统一 UA）。
# 保留 _DIRECT_SESSION 作为测试/覆盖注入点（默认 None 时走线程局部实例）。
_DIRECT_SESSION = None
_DIRECT_SESSION_LOCAL = threading.local()


def _direct_session():
    if _DIRECT_SESSION is not None:
        return _DIRECT_SESSION
    sess = getattr(_DIRECT_SESSION_LOCAL, "session", None)
    if sess is None:
        import requests

        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        _DIRECT_SESSION_LOCAL.session = sess
    return sess


def _s(val: object) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and not math.isfinite(val):
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none", "<na>") else s


def _clean_news_text(val: object, max_len: int = 0) -> str:
    raw = _s(val)
    txt = html.unescape(re.sub(r"</?[^>]+>", "", raw))
    txt = re.sub(r"\(\s*\)|（\s*）", "", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:max_len] if max_len > 0 else txt


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
        headers={"Referer": f"https://so.eastmoney.com/news/s?keyword={quote(raw)}"},
        timeout=6,
    )
    resp.raise_for_status()
    match = re.match(r"^[^(]*\((.*)\)\s*;?\s*$", resp.text, flags=re.S)
    payload = json.loads(match.group(1) if match else resp.text)
    rows = ((payload.get("result") or {}).get("cmsArticleWebOld") or [])[:limit]
    return [{
        "title": _clean_news_text(row.get("title")),
        "content": _clean_news_text(row.get("content"), 300),
        "time": str(row.get("date") or ""),
        "url": str(row.get("url") or (f"https://finance.eastmoney.com/a/{row.get('code')}.html" if row.get("code") else "")),
        "source": _clean_news_text(row.get("mediaName")) or "东方财富",
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
                "title": _clean_news_text(r.get("新闻标题")),
                "content": _clean_news_text(r.get("新闻内容"), 300),
                "time": _s(r.get("发布时间")),
                "url": _s(r.get("新闻链接")),
                "source": _clean_news_text(r.get("文章来源")) or "东方财富",
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
