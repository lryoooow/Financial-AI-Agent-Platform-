"""Deterministic routing and conservative stock-name resolution."""

import re
from dataclasses import dataclass, field
from typing import Literal

from ..tools.entity import resolve_entity, stock_ticker

_GRAPH_TEMPLATES = {"equity_deep", "peer_compare", "market_recap", "batch_radar", "industry", "earnings", "risk"}
_NEGATION = re.compile(r"(?:不用|不必|不要|别|无需|不需要|不看|别看|跳过|排除)\s*(?:再|去)?\s*(?:查询|查阅|查看|搜索|查|搜|看)?\s*(新闻|快讯|公告|联网|网页|技术面|财报)")
_QUOTED = re.compile(r"「[^」]*」|“[^”]*”|\"[^\"]*\"")
_EXPLICIT_CODE = re.compile(
    r"(?<![0-9])(?:(?:SH|SZ|BJ)[:.]?)?(?:00|30|60|68|8\d|92|43)\d{4}(?:\.(?:SH|SZ|BJ))?(?![0-9])",
    re.I,
)


@dataclass(slots=True)
class RouteChoice:
    mode: Literal["main", "graph"]
    flow: str = ""
    blocked_tools: dict[str, str] = field(default_factory=dict)
    first_tool: str = ""


def choose_route(query: str, template: str = "", *, active_symbol: str = "") -> RouteChoice:
    text = _QUOTED.sub("", query)
    blocked = {}
    restrictions = {
        "新闻": ("search_news", "web_search"), "快讯": ("search_news",), "公告": ("search_news",),
        "联网": ("web_search", "extract_article_text"), "网页": ("web_search", "extract_article_text"),
        "技术面": ("get_technical_indicators", "get_kline"), "财报": ("get_financial_indicators",),
    }
    for match in _NEGATION.finditer(text):
        for name in restrictions[match[1]]:
            blocked[name] = "[USER_SCOPE] 用户本轮已明确排除此类数据。"
    flow = template if template in _GRAPH_TEMPLATES else ""
    if not flow:
        if re.search(r"收盘复盘|大盘复盘|复盘大盘", text):
            flow = "market_recap"
        elif re.search(r"(?:批量体检|体检自选|巡检自选)", text):
            flow = "batch_radar"
        elif re.search(r"同业对比|横向对比|同行对比", text):
            flow = "peer_compare"
    if flow:
        first = {
            "market_recap": "market_overview", "batch_radar": "batch_radar", "peer_compare": "batch_radar",
            "industry": "prefetch_industry",
        }.get(flow, "prefetch_basics")
        return RouteChoice("graph", flow, blocked, first)
    intents = (
        (r"之前.*(?:报告|研报)|以前.*(?:报告|研报)|知识库|本地研报|历史研报", "search_knowledge"),
        (r"最大回撤|\bBeta\b|年化波动|\bCVaR\b", "compute_risk_metrics"),
        (r"ROE|毛利率|财报|净利润|营收|现金流|年报|季报|资产负债", "get_financial_indicators"),
        (r"K线|均线|走势|复权|分时", "get_kline"),
        (r"股价|现价|行情|报价|成交量|市盈率|市净率|估值", "get_realtime_quote"),
        (r"新闻|快讯|公告", "search_news"),
    )
    first = next((tool for pattern, tool in intents if tool not in blocked and re.search(pattern, text, re.I)), "")
    if first and first != "search_knowledge" and not active_symbol and not _EXPLICIT_CODE.search(text):
        first = "resolve_entity"
    return RouteChoice("main", "", blocked, first)


def resolve_stock_target(query: str) -> list[dict]:
    text = re.sub(r"\s+", "", query.strip())
    try:
        symbol = stock_ticker(text)
    except ValueError:
        symbol = ""
    hits = resolve_entity(text, 12).get("results") or []
    if symbol:
        first = next((hit for hit in hits if hit.get("code") == symbol), None)
        return [{"code": symbol, "name": (first or {}).get("name") or symbol}]
    folded = text.casefold()
    exact = [hit for hit in hits if str(hit.get("name") or "").replace(" ", "").casefold() == folded]
    substring = [hit for hit in hits if folded and folded in str(hit.get("name") or "").replace(" ", "").casefold()]
    direct = exact or substring or (hits[:1] if len(hits) == 1 else [])
    return [{"code": hit["code"], "name": hit["name"]} for hit in direct][:8]


def ambiguous_query(query: str) -> list[dict]:
    text = query.strip()
    candidate = re.sub(r"^(?:请问|查一下|看看|分析|帮我看|帮我查|研究|看一下)", "", text)
    candidate = re.split(r"的(?:行情|估值|财报|走势)|现在|股价|多少钱|[？?。]", candidate, maxsplit=1)[0].strip()
    if not re.fullmatch(r"[\u3400-\u9fff]{2,8}", candidate):
        return []
    hits = resolve_stock_target(candidate)
    return hits if len(hits) > 1 else []
