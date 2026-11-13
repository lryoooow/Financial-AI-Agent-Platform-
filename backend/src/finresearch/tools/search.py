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

import html
import json
import logging
import re
from urllib.parse import quote

from ..core.guardrails.errors import public_failure
from . import make_provenance
from .market_provider import configure_market_network, record

logger = logging.getLogger(__name__)

_SERPER_URL = "https://google.serper.dev/search"
_EM_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"
_EM_ANOTICE_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"


def _clean_search_text(val: object, max_len: int = 0) -> str:
    if val is None:
        return ""
    txt = html.unescape(re.sub(r"</?[^>]+>", "", str(val)))
    txt = re.sub(r"\(\s*\)|（\s*）", "", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:max_len] if max_len > 0 else txt


def _extract_stock_code(query: str) -> str:
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", query or "")
    if m:
        return m.group(1)
    try:
        from .entity import _seed_name_map

        for code, name in _seed_name_map().items():
            if name and len(name) >= 2 and name in query:
                return code[:6]
    except Exception:  # noqa: BLE001
        pass
    return ""


def domestic_web_search(query: str, limit: int = 8) -> dict:
    """国内免费财经网页与公告检索（东方财富全文资讯 + 交易所公告直连），零 API Key 依赖。"""
    import urllib.request as _u

    q = (query or "").strip()
    if not q:
        record("search", "eastmoney_search", False, "空查询")
        return {
            "items": [],
            "provenance": make_provenance(source="eastmoney_search", endpoint="search/jsonp", error="空查询"),
        }

    configure_market_network()
    tokens = [t.strip() for t in re.split(r"[\s,，;；/｜|]+", q) if t.strip()]
    primary_token = tokens[0] if tokens else q
    primary_code = _extract_stock_code(q)
    lim = max(1, min(int(limit), 20))
    items: list[dict] = []
    seen_titles: set[str] = set()
    last_err: str | None = None

    # 1) 东方财富全文资讯搜索（search-api-web.eastmoney.com）
    try:
        page_size = max(12, min(lim * 3, 30))
        inner = {
            "uid": "",
            "keyword": q,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": page_size,
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        cb = "jQuery_finresearch"
        qs = f"cb={cb}&param={quote(json.dumps(inner, ensure_ascii=True))}&_=1"
        req = _u.Request(
            f"{_EM_SEARCH_URL}?{qs}",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": f"https://so.eastmoney.com/news/s?keyword={quote(q)}",
            },
        )
        with _u.urlopen(req, timeout=5) as resp:
            raw_text = resp.read().decode("utf-8", errors="replace")
        m = re.match(r"^[^(]*\((.*)\)\s*;?\s*$", raw_text, flags=re.S)
        payload = json.loads(m.group(1) if m else raw_text)
        rows = (payload.get("result") or {}).get("cmsArticleWebOld") or []

        matched_primary: list[dict] = []
        fallback_rows: list[dict] = []
        for row in rows:
            title = _clean_search_text(row.get("title"))
            content = _clean_search_text(row.get("content"), 300)
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            entry = {
                "title": title,
                "content": content,
                "snippet": content,
                "url": str(
                    row.get("url")
                    or (f"https://finance.eastmoney.com/a/{row.get('code')}.html" if row.get("code") else "")
                ),
                "time": str(row.get("date") or ""),
                "source": _clean_search_text(row.get("mediaName")) or "东方财富资讯",
            }
            combined = f"{title} {content}"
            if (primary_token and primary_token in combined) or (primary_code and primary_code in combined):
                matched_primary.append(entry)
            else:
                fallback_rows.append(entry)

        items.extend(matched_primary if matched_primary else fallback_rows)
    except Exception as e:  # noqa: BLE001
        last_err = public_failure(e)
        logger.info("国内网页资讯搜索失败：%s", last_err)

    # 2) 若查询包含明确个股且关注公告/管线/业绩/事件，补充最新交易所公告标题
    if primary_code and len(items) < lim:
        try:
            ann_url = (
                f"{_EM_ANOTICE_URL}?sr=-1&page_size={max(4, lim)}"
                f"&page_index=1&ann_type=A&client_source=web&stock_list={primary_code}"
            )
            req_ann = _u.Request(
                ann_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://data.eastmoney.com/",
                },
            )
            with _u.urlopen(req_ann, timeout=4) as resp_ann:
                ann_data = json.loads(resp_ann.read().decode("utf-8", errors="replace"))
            ann_rows = ((ann_data.get("data") or {}).get("list")) or []
            for row in ann_rows:
                title = _clean_search_text(row.get("title"))
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)
                art_code = str(row.get("art_code") or "")
                cols = row.get("columns") or []
                col_name = _clean_search_text(cols[0].get("column_name")) if cols and isinstance(cols[0], dict) else "交易所公告"
                snippet = f"[{col_name}] {title}"
                items.append(
                    {
                        "title": title,
                        "content": snippet,
                        "snippet": snippet,
                        "url": f"https://data.eastmoney.com/notices/detail/{primary_code}/{art_code}.html" if art_code else "",
                        "time": str(row.get("notice_date") or "")[:10],
                        "source": "东方财富公告",
                    }
                )
                if len(items) >= lim:
                    break
        except Exception as e:  # noqa: BLE001
            if not last_err:
                last_err = public_failure(e)

    if items:
        record("search", "eastmoney_search", True)
        return {
            "items": items[:lim],
            "provenance": make_provenance(
                source="eastmoney_search",
                endpoint="search/jsonp",
                raw_value={"q": q, "num": lim},
            ),
        }

    err_msg = last_err or "未检索到匹配的公开资讯"
    record("search", "eastmoney_search", False, err_msg)
    return {
        "items": [],
        "provenance": make_provenance(
            source="eastmoney_search",
            endpoint="search/jsonp",
            raw_value=q,
            error=err_msg,
        ),
    }


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
        failure = public_failure(e)
        logger.warning("Serper 搜索失败（联网搜索增强不可用）：%s", failure)
        record("search", "serper", False, failure)
        return {
            "items": [],
            "provenance": make_provenance(
                source="serper", endpoint="search", raw_value=q, error=failure
            ),
        }
