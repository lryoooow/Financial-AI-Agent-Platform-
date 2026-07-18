"""研究链路的 agent 工具层。

两条设计原则，都是从"数字零幻觉"和"报告不能变慢"倒推出来的：

1. **大块数据不进模型上下文。** prefetch_basics 取回的是 120 根 K 线、12 条新闻、多年财务
   序列——把这些倒给模型既会炸上下文，也给了它逐个数字复述（进而写错）的机会。所以工具把
   数据存进按 task_id 的进程内存储，只回一句紧凑摘要（"行情✓ 财务✓ 新闻12条"）。
   模型据此知道"数据齐了"，但从头到尾看不到原始数值——它结构上就没有伪造的素材。
   真正的数值由 compose_report 交给 report.py 的分段事实块机制，按段精确投喂。

2. **零重写。** research_node / market_node / report_node 本来就是接受 state、返回 dict 的
   async 函数，这里直接调用它们，不复制一行业务逻辑。行为必然与旧管线一致，
   连超时值、兜底策略、并发闸都原样继承。
   顺带还快了一点：旧图是 research → market 串行（add_edge），这里两者并行。

存储是进程内 dict，与 routes.py 的 _TASK_LLM 同一套路（家用单进程桌面应用）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from langchain_core.tools import tool

from ...tools import facts as facts_mod
from ...tools.entity import to_market_ticker
from ...tools.industry import search_industry as _search_industry
from ...tools.news import search_news as _search_news
from .._utils import _timed_call
from ..workers.entity_resolver import entity_node
from ..workers.market import market_node
from ..workers.report import _stream_push, report_node, summary_node
from ..workers.research import research_node

# 行业估值取数超时：与 deep/tools.py 的 _T_INDUSTRY 对齐（实测并行冷启动 5-12s）
_T_INDUSTRY = 20.0

logger = logging.getLogger(__name__)

# 按 task_id 存放取数结果与成稿，避免它们流经模型上下文。
# 上限与 routes._TASK_LLM 同量级：家用场景不会有几百个并发任务，超了丢最早的。
_STORE: dict[str, dict] = {}
_STORE_CAP = 64


def _put(task_id: str, key: str, value: Any) -> None:
    slot = _STORE.setdefault(task_id, {})
    slot[key] = value
    if len(_STORE) > _STORE_CAP:
        for k in list(_STORE)[: len(_STORE) - _STORE_CAP]:
            _STORE.pop(k, None)


def get_stored(task_id: str, key: str, default=None):
    """供 routes 取回 draft_report / findings——它们从不流经模型。"""
    return (_STORE.get(task_id) or {}).get(key, default)


def clear_task(task_id: str) -> None:
    _STORE.pop(task_id, None)


def _summarize_findings(fv: dict) -> str:
    """给模型看的紧凑摘要：只说"有没有、多少条"，不说具体数值。"""
    quote = fv.get("quote") or {}
    fins = fv.get("fins") or {}
    bits = []
    bits.append(f"行情{'✓' if facts_mod.quote_usable(quote) else '✗(取数失败)'}")
    years = len((fins.get("years") or []))
    bits.append(f"财务{'✓ %d期' % years if years else '✗(取数失败)'}")
    inds = fv.get("indicators") or []
    bits.append(f"技术指标{'✓ %d项' % len(inds) if inds else '✗'}")
    kline = fv.get("kline") or []
    bits.append(f"K线{'✓ %d根' % len(kline) if kline else '✗'}")
    news = (fv.get("news") or {}).get("items") if isinstance(fv.get("news"), dict) else fv.get("news")
    bits.append(f"新闻{'✓ %d条' % len(news) if news else '✗'}")
    web = fv.get("web") or {}
    web_items = web.get("items") if isinstance(web, dict) else web
    if fv.get("web_configured"):
        bits.append(f"联网搜索{'✓ %d条' % len(web_items) if web_items else '✗'}")
    else:
        bits.append("联网搜索✗(未配置)")
    return "、".join(bits)


def _emit_summary(state: dict, summary: str) -> None:
    """把摘要快照作为 custom 事件推给前端，字段与旧路由发的 summary 事件逐字段一致。

    摘要是完整事实快照，不是模型 token —— 前端用替换语义渲染，重连/回放时不会重复 append。
    """
    if not summary:
        return
    quote = (state.get("facts") or {}).get("quote") or {}
    # ticker 必须是带市场后缀的前端格式（600519.SH）：Workspace.tsx:183 直接拿 ev.ticker 当 code 用。
    # normalize_ticker 返回的是裸代码（600519），照发会让前端拿到错的标的格式。
    raw = state.get("normalized_ticker") or state.get("code") or ""
    _stream_push({
        "type": "research_summary",
        "text": summary,
        "name": quote.get("name") or "",
        "ticker": to_market_ticker(raw) if raw else "",
        "price": quote.get("price"),
        "change": quote.get("change"),
        "changePct": quote.get("changePct"),
    })


def build_research_tools(task_id: str, template: str, llm_config: Optional[dict], profile: dict):
    """按任务构造工具（task_id / 模板 / 模型配置闭包进去，模型不用也不该传这些）。"""

    @tool
    async def prefetch_basics(code: str) -> str:
        """取齐该标的做研究所需的全部基础数据：公司概况、财务指标、新闻、联网搜索、
        实时行情、K线、技术指标。

        这是研究的第一步，一次调用取全，内部并行，比逐个调快得多。
        返回的是数据清单摘要而非数值本身——数值会在生成报告时按段精确投喂，
        你不需要、也不应该逐个复述它们。

        某项显示 ✗ 表示该数据源本次取数失败，报告里对应部分会如实标注"数据不足"，
        不要用记忆或推算去补。

        Args:
            code: 标的代码，如 "600519.SH"；不确定就先用 resolve_entity 查。
        """
        ent = await entity_node({"code": code})
        norm = ent.get("normalized_ticker") or code
        state = {"code": norm, "normalized_ticker": norm}

        # 旧图是 research → market 串行（add_edge）；这里并行，行为一致但更快。
        research_out, market_out = await asyncio.gather(
            research_node(state), market_node(state)
        )

        findings = (
            ent.get("findings", [])
            + research_out.get("findings", [])
            + market_out.get("findings", [])
        )
        fv = facts_mod.normalize_findings(findings)

        _put(task_id, "findings", findings)
        _put(task_id, "citations", research_out.get("citations", []))
        _put(task_id, "risk_flags", market_out.get("risk_flags", []))
        _put(task_id, "normalized_ticker", norm)
        _put(task_id, "data_as_of", ent.get("data_as_of", ""))
        _put(task_id, "facts", fv)
        _put(task_id, "timings", {
            **(research_out.get("timings") or {}),
            **(market_out.get("timings") or {}),
        })

        name = (fv.get("quote") or {}).get("name") or (fv.get("profile") or {}).get("name") or ""
        return f"{name or norm}（{norm}）数据已取回：{_summarize_findings(fv)}。可以生成报告了。"

    @tool
    async def prefetch_industry(industry_name: str) -> str:
        """取某个行业的估值与资讯数据（申万行业估值 + 该行业相关新闻），用于行业研究。

        个股研究请用 prefetch_basics；行业研究（如"研究光伏行业"）用本工具。
        内部并行取数，返回数据清单摘要而非数值本身。

        某项标 ✗ 表示该数据源本次取数失败，报告对应部分会如实标注，不要用记忆去补。

        Args:
            industry_name: 行业名称，如 "光伏" / "半导体" / "白酒"
        """
        # 行业估值 + 行业新闻并行取。行业新闻用行业名作关键词走 search_news 的东财源
        # （它接受任意关键词，不止个股代码）。
        iv_res, news_res = await asyncio.gather(
            _timed_call(_search_industry, industry_name, 8, timeout=_T_INDUSTRY, label="industry_valuation"),
            _timed_call(_search_news, industry_name, timeout=6, label="industry_news", limit=10),
        )
        iv, _iv_ms, iv_err = iv_res
        news, _news_ms, _news_err = news_res

        if iv_err or not (iv and iv.get("items")):
            # 区分两种失败，别把数据源挂了说成"行业名称不对"——那会让用户以为是自己输错。
            prov_err = (iv.get("provenance") or {}).get("error") if isinstance(iv, dict) else None
            source_failed = bool(iv_err) or bool(prov_err)
            if source_failed:
                reason = f"数据源暂时不可用（{iv_err or prov_err}），请稍后重试"
            else:
                reason = f"未找到名为「{industry_name}」的行业，请换个说法（如「光伏」「半导体」「白酒」）"
            return f"未能取到行业估值数据：{reason}。无法生成行业报告。"

        # 用最匹配的行业（items 已按粒度排序，第一个最具体）作为报告主体
        top = iv["items"][0]
        news_items = (news or {}).get("items", []) if isinstance(news, dict) else []

        # 拼成 report_node 认得的 fv 形状：industry_valuation 供 _facts_landscape，
        # profile.name 供 report_node 取标的名，news 供资讯证据。
        fv = {
            "industry_valuation": iv,
            "profile": {"name": top.get("name", industry_name), "industry": top.get("name", "")},
            "news": news_items,
            "web": [],
            "quote": {},
            "fins": {"years": [], "rows": []},
            "indicators": [],
        }
        findings = [
            {"key": "industry", "title": "行业估值", "items": iv.get("items", []),
             "provenance": iv.get("provenance")},
            {"key": "news", "title": "行业资讯", "items": news_items,
             "provenance": (news or {}).get("provenance") if isinstance(news, dict) else None},
        ]
        citations = [
            {"title": f"[行业估值] {top.get('name','')} · 申万/新浪", "url": "sw_index_info",
             "date": "", "kind": "industry"},
        ]
        for n in news_items[:6]:
            citations.append({"title": "[行业资讯] " + n.get("title", ""), "url": n.get("url", ""),
                              "date": n.get("time", ""), "kind": "news"})

        _put(task_id, "findings", findings)
        _put(task_id, "facts", fv)
        _put(task_id, "citations", citations)
        _put(task_id, "risk_flags", [])
        _put(task_id, "normalized_ticker", top.get("code", ""))
        _put(task_id, "is_industry", True)
        _put(task_id, "industry_name", top.get("name", industry_name))
        _put(task_id, "data_as_of", "")

        warn = "" if (iv.get("levels_ok") or 0) >= (iv.get("levels_total") or 0) else "（行业列表可能不完整）"
        n_ind = len(iv.get("items", []))
        return (
            f"{top.get('name', industry_name)} 行业数据已取回：命中 {n_ind} 个申万行业估值、"
            f"新闻 {len(news_items)} 条{warn}。可以生成报告了。"
        )

    @tool
    async def compose_report() -> str:
        """基于 prefetch_basics 取回的数据生成研究报告。

        必须先调 prefetch_basics 再调本工具。
        报告各段由本工具按模板逐段撰写（每段只喂该段所需的事实，数据不足的段会如实标注
        "不展开"而不是编内容），你不需要自己写正文。
        返回生成结果摘要。
        """
        stored = _STORE.get(task_id) or {}
        if not stored.get("findings"):
            return "还没有数据，请先调用 prefetch_basics。"

        state = {
            "code": stored.get("normalized_ticker", ""),
            "normalized_ticker": stored.get("normalized_ticker", ""),
            "template": "industry" if stored.get("is_industry") else template,
            "task_id": task_id,
            "profile": profile,
            "llm_config": llm_config,
            "findings": stored.get("findings", []),
            "facts": stored.get("facts", {}),
            "citations": stored.get("citations", []),
            "risk_flags": stored.get("risk_flags", []),
            "data_as_of": stored.get("data_as_of", ""),
            # 行业研究：告诉 report_node 别把行业代码（801735.SI）当个股过 to_market_ticker
            "is_industry": bool(stored.get("is_industry")),
        }

        # 先跑 summary_node，再跑 report_node —— 复刻旧图的 summary → report 边，一步都不能省：
        # 1) 它让摘要在四段详细分析之前就产出（"降低首段等待时间"，见 summary_node docstring）；
        # 2) report_use_llm_summary=True 时摘要由 LLM 撰写，与四段口吻统一。
        # 只调 report_node 的话，它那句 `state.get("draft_summary") or _compose_summary_block(...)`
        # 会永远走规则模板——LLM 摘要这条路就被静默砍掉了，开关打开也没反应。
        sum_out = await summary_node(state)
        state["draft_summary"] = sum_out.get("draft_summary", "")
        state["facts"] = sum_out.get("facts") or state["facts"]

        # 摘要快照推给前端：旧路由是在 updates 流里看到 summary 节点完成时发 summary 事件；
        # 现在 summary_node 跑在工具内部，updates 流看不见它，改用 custom 事件带出去。
        _emit_summary(state, sum_out.get("draft_summary", ""))

        out = await report_node(state)
        doc = out.get("draft_report")
        if not doc:
            return "报告生成失败：未产出草稿。"

        _put(task_id, "draft_report", doc)
        _put(task_id, "timings", {
            **(stored.get("timings") or {}),
            **(sum_out.get("timings") or {}),
            **(out.get("timings") or {}),
        })

        secs = doc.get("sections") or []
        return (
            f"报告已生成：{doc.get('name', '')}（{doc.get('ticker', '')}），"
            f"共 {len(secs)} 段，数据时点 {doc.get('as_of', '')}。研究完成。"
        )

    return [prefetch_basics, prefetch_industry, compose_report]
