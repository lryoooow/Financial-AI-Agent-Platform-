"""联网网页搜索（默认国内优先，Serper/Google 仅显式开启）。

设计（对应根因 3：原 agent 不是真正的联网搜索 agent）：
- 默认不调用国外搜索；研究证据主要来自 AkShare/东方财富个股新闻。
- 同时设置 ENABLE_SERPER_SEARCH=true 和 SERPER_API_KEY 时，才调 https://google.serper.dev/search，返回真实网页结果
  [{title, content(snippet), url, time, source}]，带 provenance.source='serper'。
- 未显式开启或无 key：明确返回 items=[] + provenance.error（绝不静默、绝不伪造结果）。
- 任何网络/HTTP 错误：返回空 + provenance.error；本函数永不抛异常，确保研究流程不受影响。
- 记录到诊断环形缓冲（kind='search'），供 /api/market/diagnostics 暴露搜索源状态。
"""

from __future__ import annotations

import json
import logging

from . import make_provenance
from .market_provider import record

logger = logging.getLogger(__name__)

_SERPER_URL = "https://google.serper.dev/search"


def is_configured() -> bool:
    from ..config import settings

    return bool(settings.enable_serper_search and settings.serper_api_key and settings.serper_api_key.strip())


def web_search(query: str, limit: int = 8) -> dict:
    """Serper 网页搜索。无 key 返回空 + 明确未配置标记；失败永不抛异常。"""
    from ..config import settings
    import urllib.request as _u

    key = (settings.serper_api_key or "").strip()
    q = (query or "").strip()
    if not settings.enable_serper_search:
        record("search", "serper", False, "serper_disabled_domestic_first")
        return {
            "items": [],
            "provenance": make_provenance(
                source="serper",
                endpoint="search",
                raw_value=q,
                error="serper_disabled_domestic_first",
            ),
        }
    if not key:
        record("search", "serper", False, "serper_unconfigured")
        return {
            "items": [],
            "provenance": make_provenance(
                source="serper",
                endpoint="search",
                raw_value=q,
                error="serper_unconfigured",
            ),
        }
    if not q:
        record("search", "serper", False, "空查询")
        return {"items": [], "provenance": make_provenance(source="serper", endpoint="search", error="空查询")}

    try:
        body = json.dumps({"q": q, "num": int(limit), "gl": "cn", "hl": "zh-cn"}).encode("utf-8")
        req = _u.Request(
            _SERPER_URL,
            data=body,
            method="POST",
            headers={
                "X-API-KEY": key,  # Serper 用 X-API-KEY，非 Bearer
                "Content-Type": "application/json",
                "User-Agent": "JingYuan/1.0",
            },
        )
        with _u.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))

        items = []
        for r in (data.get("organic") or [])[: int(limit)]:
            items.append(
                {
                    "title": str(r.get("title", "") or ""),
                    "content": str(r.get("snippet", "") or "")[:300],
                    "url": str(r.get("link", "") or ""),
                    "time": str(r.get("date", "") or ""),
                    "source": str(r.get("source") or r.get("displayLink") or "serper/google"),
                }
            )
        record("search", "serper", True)
        return {
            "items": items,
            "provenance": make_provenance(
                source="serper",
                endpoint="search",
                raw_value={"q": q, "num": int(limit)},
            ),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("Serper 搜索失败（联网搜索增强不可用）：%s", e)
        record("search", "serper", False, str(e))
        return {
            "items": [],
            "provenance": make_provenance(
                source="serper", endpoint="search", raw_value=q, error=str(e)
            ),
        }
