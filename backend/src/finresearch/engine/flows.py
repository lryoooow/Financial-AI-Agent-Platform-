"""Presets with validated inputs and deterministic artifacts."""

import asyncio
import re
from datetime import datetime
from collections.abc import AsyncIterator
from uuid import uuid4

from ..api import sse
from ..agents._utils import _timed_call
from ..agents.deep.research_runner import run_research_direct
from ..agents.deep.research_tools import _put
from ..tools import make_provenance, report, now_cst
from ..tools.batch_radar import batch_radar
from ..tools.citation_verify import verify_citation
from ..core.guardrails.untrusted import looks_like_instruction
from ..tools.facts import measure
from ..tools.market_breadth import market_overview
from .event_bridge import EventBridge
from .router import resolve_stock_target


async def _peer_comparison(symbols: list[str], blocked: set[str]) -> dict:
    if len(symbols) < 2 or len(symbols) > 6:
        raise ValueError("同业对比需要 2 至 6 只已确认的标的。")
    result = await batch_radar(symbols, blocked=blocked | {"search_news", "get_kline"})
    rows = result["rows"]
    return {"rows": rows, "matrix": [{
        "symbol": row["symbol"], "name": row["name"], "period": row["earnings"].get("period"),
        "pe_ttm": row["valuation"].get("pe_ttm"), "pb": row["valuation"].get("pb"),
        "revenue_yoy": row["earnings"].get("revenue_yoy"), "profit_yoy": row["earnings"].get("profit_yoy"),
        "roe": row["earnings"].get("roe"), "valuation_as_of": row["valuation"].get("as_of"),
    } for row in rows]}


async def _batch(symbols: list[str], blocked: set[str]) -> dict:
    return await batch_radar(symbols, blocked=blocked)


def _market_catalysts() -> list[dict]:
    import akshare as ak

    frame = ak.stock_info_global_em()
    received = []
    seen = set()
    for row in frame.to_dict(orient="records"):
        headline = str(row.get("标题") or "").strip()
        stamp = str(row.get("发布时间") or "")
        url = str(row.get("链接") or "")
        try:
            published = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if published.replace(tzinfo=None) > now_cst().replace(tzinfo=None) or not url.startswith("https://finance.eastmoney.com/a/"):
            continue
        if headline in seen or looks_like_instruction(headline) or not re.search(r"A股|沪深|上证|深证|收盘|央行|行业|指数|板块", headline):
            continue
        seen.add(headline)
        received.append({"title": headline, "time": stamp, "url": url, "source": "东方财富全球财经快讯"})
    return sorted(received, key=lambda item: item["time"], reverse=True)


async def _recap(_symbols: list[str], blocked: set[str]) -> dict:
    if "search_news" in blocked:
        return {**await market_overview(), "catalyst_scope": "本轮已排除新闻。"}
    overview, (news, _, error) = await asyncio.gather(market_overview(), _timed_call(_market_catalysts, timeout=6))
    trade_date = (overview.get("turnover_dates") or {}).get("trading_date")
    if not trade_date:
        dates = [str(row["as_of"])[:10] for row in overview.get("indices", []) if row.get("as_of")]
        trade_date = max(dates, default="")
    items = [item for item in (news or []) if trade_date and item["time"][:10] == trade_date][:5]
    return {**overview, "items": items, "catalyst_provenance": make_provenance(
        source="eastmoney", endpoint="stock_info_global_em", raw_value=trade_date, error=error,
    )}


PRESET_FLOWS = {
    "equity_deep": run_research_direct,
    "peer_compare": _peer_comparison,
    "market_recap": _recap,
    "batch_radar": _batch,
}


def _recap_doc(data: dict) -> dict:
    indices = data.get("indices") or []
    breadth = data.get("breadth") or {}
    captured = [item for item in indices if item.get("price") is not None]
    timestamps = {item.get("as_of") for item in captured if item.get("as_of")}
    day = max(timestamps) if timestamps else "—（指数时点未获取）"
    summary = (
        f"全市场上涨 {measure(breadth.get('up'), '家', 0)}，"
        f"下跌 {measure(breadth.get('down'), '家', 0)}，"
        f"平盘 {measure(breadth.get('flat'), '家', 0)}。"
    )
    sections = [
        {"title": "宽基指数", "body": "\n".join(
            f"{item['name']} · {measure(item.get('price'), '点', 2)} · {measure(item.get('changePct'), '%', 2)}"
            f" · {(item.get('provenance') or {}).get('source', '—')} · {item.get('as_of') or '—'}"
            for item in indices
        ) or "指数数据暂不可用。"},
        {"title": "成交与广度", "body": (
            f"{summary} 两市成交额 {measure(breadth.get('turnover_yuan'), '元', 0)}。"
            f" 较上一交易日 {measure(breadth.get('turnover_change_yuan'), '元', 0)}。"
        )},
        {"title": "申万一级行业", "body": "\n".join(
            f"{item['name']} {measure(item.get('changePct'), '%', 2)} · 同名东财板块主力净流入 {measure(item.get('net_flow_yuan'), '元', 0)}"
            for item in [*data.get("leaders", [])[:6], *data.get("laggards", [])[:6]]
        ) or "行业数据暂不可用。"},
    ]
    catalysts = data.get("items") or []
    sections.append({"title": "当日市场快讯", "body": "\n".join(
        f"「{item['title']}」 · {item['time']}" for item in catalysts
    ) or data.get("catalyst_scope") or "暂未取得与交易日一致的市场快讯。"})
    sources = [
        {"title": str((item.get("provenance") or {}).get("source") or "行情接口"),
         "url": "", "date": str(item.get("as_of") or "")}
        for item in captured
    ]
    sources.extend({"title": item["title"], "url": item["url"], "date": item["time"]} for item in catalysts)
    doc = report.build_doc(
        ticker="market", name="A 股收盘复盘", template="market_recap", summary=summary,
        catalyst=f"「{catalysts[0]['title']}」" if catalysts else "",
        financials={"years": [], "rows": []}, sections=sections, risks=["部分数据源可能存在延迟或缺失。"],
        sources=sources, as_of=day, quote=None,
    )
    doc["generated_at"] = now_cst().isoformat()
    doc["market_overview"] = data
    doc["html"] = report.render_html(doc)
    return doc


async def run_recap_research(task_id: str, blocked: set[str] | None = None) -> AsyncIterator[str]:
    call_id = uuid4().hex
    bridge = EventBridge()
    yield sse.sse(bridge.start_tool("market_recap", call_id, "market_overview", "聚合大盘行情", {}))
    data = await PRESET_FLOWS["market_recap"]([], blocked or set())
    doc = _recap_doc(data)
    for event in bridge.finish_tool("market_recap", call_id, "market_overview", "聚合大盘行情", "ok", data):
        yield sse.sse(event)
    yield sse.sse({"type": "summary", "text": doc["summary"]})
    for section in doc["sections"]:
        yield sse.sse({"type": "section", **section, "source": "template"})
    _put(task_id, "draft_report", doc)


async def symbols_from_query(query: str) -> list[str]:
    raw_parts = re.split(
        r"[、，,;；/|\s]+|\b(?:vs|pk)\b|和|与|跟|对比|同行|同业|横向|比较|请|帮我|做一下|做个|做|分析|看看|查一下|体检|巡检",
        query,
        flags=re.I,
    )
    cleaned: list[str] = []
    for part in raw_parts:
        term = re.sub(r"的(?:估值|财报|财务|行情|走势|业绩|基本面|技术面|指标).*$", "", part.strip(" ？?。！!：:()（）"))
        term = term.rstrip("的")
        if 2 <= len(term) <= 14:
            cleaned.append(term)
    groups = await asyncio.gather(*(asyncio.to_thread(resolve_stock_target, term) for term in cleaned[:12]))
    return list(dict.fromkeys(group[0]["code"] for group in groups if len(group) == 1))[:6]


async def run_chat_flow(flow: str, query: str, blocked: set[str], active_symbol: str = "") -> AsyncIterator[tuple[str, str]]:
    symbols = await symbols_from_query(query) if flow in {"peer_compare", "batch_radar"} else []
    if flow == "batch_radar" and not symbols:
        from ..api.watchlist_routes import group_symbols

        symbols = (await group_symbols())[:50]
        if not symbols and active_symbol:
            symbols = [active_symbol]
    elif flow == "peer_compare" and len(symbols) == 1 and active_symbol and active_symbol not in symbols:
        symbols.insert(0, active_symbol)
    call_id = uuid4().hex
    bridge = EventBridge()
    yield sse.sse(bridge.start_tool(flow, call_id, flow, "获取结构化事实", {"symbols": symbols})), ""
    try:
        data = await PRESET_FLOWS[flow](symbols, blocked)
        if flow == "market_recap":
            doc = _recap_doc(data)
            text = doc["summary"] + "\n\n" + next(section["body"] for section in doc["sections"] if section["title"] == "当日市场快讯")
        elif flow == "peer_compare":
            lines = ["| 标的 | 披露期 | PE TTM | PB | 营收同比 | 净利同比 | ROE |",
                     "| :--- | :--- | ---: | ---: | ---: | ---: | ---: |"]
            for row in data["matrix"]:
                lines.append("| " + " | ".join([
                    row["name"].replace("|", " "), row["period"] or "—", measure(row["pe_ttm"], " 倍"),
                    measure(row["pb"], " 倍"), measure(row["revenue_yoy"], "%"),
                    measure(row["profit_yoy"], "%"), measure(row["roe"], "%"),
                ]) + " |")
            periods = {row["period"] for row in data["matrix"]}
            if len(periods) > 1 or not next(iter(periods), None):
                lines.append("\n财务报告期存在差异，逐行查看披露口径。")
            else:
                values = [row for row in data["matrix"] if row["revenue_yoy"] is not None]
                if len(values) > 1:
                    highest = max(values, key=lambda row: row["revenue_yoy"])
                    lines.append(f"\n{highest['name']}的营收同比为 {measure(highest['revenue_yoy'], '%')}，在所列可比样本中最高。")
            text = "\n".join(lines)
        else:
            text = "\n".join(
                f"{row['name']} · 同口径营收同比 {measure(row['earnings'].get('revenue_yoy'), '%', 2)}"
                f" · 归母净利同比 {measure(row['earnings'].get('profit_yoy'), '%', 2)}"
                for row in data["rows"]
            ) or "请先选择需要体检的标的。"
    except ValueError as exc:
        data, text = None, str(exc)
    for event in bridge.finish_tool(flow, call_id, flow, "获取结构化事实", "ok" if data is not None else "error", data):
        yield sse.sse(event), ""
    yield sse.ev_token(text), text
