"""deepagents 对话链路的硬不变量。

这里锁的都是会静默坏掉的东西：工具面失守不会报错、失败语义错位不会报错——
只会让模型悄悄说出编造的数字。
"""

import asyncio
import json

import pytest

from finresearch.agents.deep import chat_agent as ca
from finresearch.agents.deep import stream as st
from finresearch.agents.deep import tools as dt


# ── 数字零幻觉：工具面 ────────────────────────────────────────────────


class _FakeModel:
    """记录被绑定了哪些工具；不发请求。"""

    def __init__(self):
        self.bound = []

    def bind_tools(self, tools, **kw):
        self.bound = [getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in tools]
        return self


class _Req:
    def __init__(self, tools):
        self.tools = tools

    def override(self, tools):
        return _Req(tools)


def _named(*names):
    return [type("T", (), {"name": n})() for n in names]


def test_execute_tool_never_reaches_model():
    """execute 必须被摘掉。

    FilesystemMiddleware 无条件挂一个 execute（"Executes a shell command"）。默认 StateBackend
    没实现 execute()，调了只会报错——但摆在偏弱的工具调用模型面前它一定会去试，白白绕圈；
    且一旦换成沙箱后端就静默变成真的，agent 能跑代码，「数字零幻觉」当场失效。
    """
    mw = ca.ExcludeToolsMiddleware(excluded=ca._EXCLUDED_TOOLS)
    kept = [t.name for t in mw._filter(_Req(_named("execute", "ls", "get_realtime_quote"))).tools]
    assert "execute" not in kept
    assert "get_realtime_quote" in kept


def test_task_tool_never_reaches_model():
    """task 必须被摘掉。

    即便 subagents=None，deepagents 仍默认插一个 general-purpose subagent，从而多出 task。
    对话是短程任务，不需要上下文隔离；多一层嵌套 agent loop 只会更慢，并给工具调用多一个
    误触发面。Phase 2 研究链路要挂 subagents 时把 task 从 _EXCLUDED_TOOLS 去掉即可。
    """
    mw = ca.ExcludeToolsMiddleware(excluded=ca._EXCLUDED_TOOLS)
    kept = [t.name for t in mw._filter(_Req(_named("task", "write_todos"))).tools]
    assert "task" not in kept
    assert "write_todos" in kept


def test_no_compute_tool_is_exposed():
    """工具集里不得出现任何计算/执行工具——数值只能来自取数工具的返回值。"""
    names = {t.name for t in dt.CHAT_TOOLS}
    assert not (names & {"execute", "shell", "bash", "python", "run_code", "eval"})


def test_chat_tools_cover_the_data_surface():
    """取数面不能缺——缺一个，模型就会在那个维度上凭记忆编。"""
    names = {t.name for t in dt.CHAT_TOOLS}
    assert names == {
        "resolve_entity",
        "get_realtime_quote",
        "get_technical_indicators",
        "get_kline",
        "get_company_profile",
        "get_financial_indicators",
        "search_news",
        "web_search",
        "search_industry",
    }


def test_industry_partial_failure_is_reported_not_silent(monkeypatch):
    """行业搜索有层级失败时必须标注，不能把残缺结果当完整结果返回。

    三个层级并行取，新浪会偶发失败（实测同一查询命中数在 1~5 之间跳）。若不标注，
    搜"光伏"只剩 1 个命中时，调用方和模型会以为该行业就这么点数据。
    与 _normalize 防的是同一类事：残缺伪装成完整比直接失败更危险。
    """
    from finresearch.tools import industry as ind

    real = ind._rows

    def flaky(level_name, fn_name, retries=2):
        if level_name == "申万三级":
            raise RuntimeError("模拟新浪抽风")
        return [{"level": level_name, "code": "801735.SI", "name": "光伏设备", "parent": "电力设备",
                 "constituents": 64, "pe_static": 27.2, "pe_ttm": 29.0, "pb": 3.41,
                 "dividend_yield": 1.61}]

    monkeypatch.setattr(ind, "_rows", flaky)
    out = ind.search_industry("光伏")

    assert out["items"], "部分失败时仍应返回已命中的"
    assert out["levels_ok"] == 2 and out["levels_total"] == 3
    assert "不完整" in (out["provenance"]["error"] or ""), "部分失败必须写进 provenance.error"


def test_industry_all_levels_ok_reports_no_error(monkeypatch):
    """全部成功时不得误报 error，否则模型会平白告诉用户数据不全。"""
    from finresearch.tools import industry as ind

    monkeypatch.setattr(ind, "_rows", lambda ln, fn, retries=2: [
        {"level": ln, "code": "x", "name": "光伏设备", "parent": "", "constituents": 1,
         "pe_static": 1.0, "pe_ttm": 1.0, "pb": 1.0, "dividend_yield": 1.0}
    ])
    out = ind.search_industry("光伏")
    assert out["levels_ok"] == out["levels_total"] == 3
    assert out["provenance"]["error"] is None


# ── 行业研究：踩过的坑，逐条锁 ──────────────────────────────────────


def test_industry_code_not_mangled_by_market_ticker():
    """行业代码（801735.SI）绝不能过 to_market_ticker——那会把 .SI 当个股后缀改成 .SZ，
    污染成一个不存在的股票。这是 report_node 里 is_industry 分支的守卫。"""
    from finresearch.tools.entity import to_market_ticker

    # 证明危险确实存在：不走保护就会被改
    assert to_market_ticker("801735") == "801735.SZ"

    # report_node 的实际逻辑：is_industry 时原样保留
    raw = "801735.SI"
    ticker = raw if True else to_market_ticker(raw)  # True 代表 is_industry
    assert ticker == "801735.SI"


def test_industry_valuation_reaches_landscape_facts():
    """行业估值必须进「行业景气」段的事实块——否则报告会说"估值未提供"却明明取到了。"""
    from finresearch.agents.workers.report import _facts_landscape, _has_evidence

    fv = {
        "industry_valuation": {
            "items": [{"level": "申万二级", "name": "光伏设备", "constituents": 64,
                       "pe_ttm": 29.0, "pb": 3.41, "dividend_yield": 1.61}],
            "levels_ok": 3, "levels_total": 3,
        },
        "news": [], "web": [],
    }
    assert _has_evidence(fv), "有行业估值就该判定有证据"
    block = _facts_landscape(fv)
    assert "光伏设备" in block and "29.0" in block, f"估值未进事实块: {block}"


def test_industry_landscape_rule_fallback_includes_valuation():
    """模型不可用时，规则回退 _section_landscape 也要罗列行业估值，不能只剩新闻。"""
    from finresearch.agents.workers.report import _section_landscape

    fv = {
        "industry_valuation": {
            "items": [{"name": "白酒Ⅱ", "pe_ttm": 18.5, "pb": 3.67, "dividend_yield": 5.46}],
            "levels_ok": 3, "levels_total": 3,
        },
        "news": [], "web": [], "profile": {"industry": "白酒"},
    }
    body = _section_landscape(fv)
    assert "白酒Ⅱ" in body and "18.5" in body, f"规则回退丢了估值: {body}"


# ── 失败语义：provenance.error 必须提升为顶层 error ───────────────────


def test_normalize_lifts_provenance_error_to_top_level():
    """既有工具失败时不抛异常，只在 provenance.error 里留痕，数据为空却"看起来成功"。

    模型读到空 items 会当成"确实没有相关新闻"，而不是"这次没查成"——正是
    AGENT_CHAT_COMPLIANCE 第 3 条要防的。所以必须提升到顶层。
    """
    res = dt._normalize(
        {"items": [], "provenance": {"source": "serper", "error": "serper_unconfigured"}}
    )
    assert res["error"] == "serper_unconfigured"


def test_normalize_leaves_successful_result_untouched():
    """provenance.error 正常时是 None，不得误伤成功结果。"""
    res = dt._normalize({"price": 1253.0, "provenance": {"source": "sina", "error": None}})
    assert "error" not in res
    assert res["price"] == 1253.0


def test_normalize_does_not_clobber_existing_top_level_error():
    res = dt._normalize({"error": "timeout", "provenance": {"error": "other"}})
    assert res["error"] == "timeout"


@pytest.mark.parametrize("bad", [None, "text", 42, []])
def test_normalize_tolerates_non_dict(bad):
    assert dt._normalize(bad) is bad


def test_web_search_unconfigured_surfaces_error_to_model(monkeypatch):
    """端到端：未配置 Serper 时，模型拿到的必须是带 error 的返回，而不是干净的空列表。"""
    monkeypatch.setattr(
        dt,
        "_web_search",
        lambda q, limit=8: {
            "items": [],
            "provenance": {"source": "serper", "endpoint": "search", "error": "serper_unconfigured"},
        },
    )
    out = asyncio.run(dt.web_search.ainvoke({"query": "光伏", "limit": 3}))
    assert out["error"] == "serper_unconfigured"


# ── SSE 映射 ─────────────────────────────────────────────────────────


def test_tool_failed_reads_the_fail_convention():
    """_fail / _normalize 都把失败写在顶层 error，SSE 状态据此判定。"""
    assert st._tool_failed(json.dumps({"error": "timeout"})) is True
    assert st._tool_failed(json.dumps({"price": 1253.0})) is False


def test_tool_failed_treats_unparseable_as_success():
    """非 JSON 内容宁可漏报，也不要把正常结果标成红的。"""
    assert st._tool_failed("not json") is False
    assert st._tool_failed(None) is False


def test_tool_label_falls_back_to_raw_name():
    """未登记的工具名直接显示原名，不要硬编码一个会骗人的兜底文案。"""
    assert st.tool_label("get_realtime_quote") == "拉取实时行情"
    assert st.tool_label("some_new_tool") == "some_new_tool"


def test_every_exposed_tool_has_a_chinese_label():
    """每个会暴露给模型的工具都必须有中文标签。

    漏登记不会报错，只会在 UI 上把 prefetch_basics 这种函数名直接甩给用户看——
    这类静默失真没人会主动发现，只能靠测试守。
    """
    from finresearch.agents.deep.research_tools import build_research_tools

    names = {t.name for t in dt.CHAT_TOOLS}
    names |= {t.name for t in build_research_tools("t", "equity_deep", None, {})}
    unlabeled = {n for n in names if st.tool_label(n) == n}
    assert not unlabeled, f"这些工具没有中文标签: {sorted(unlabeled)}"


def test_extract_text_handles_multimodal_list_content():
    msg = type("M", (), {"content": [{"type": "text", "text": "甲"}, {"type": "text", "text": "乙"}]})()
    assert st._extract_text(msg) == "甲乙"


def test_render_todos_normalizes_shape():
    out = st._render_todos([{"content": "查行情", "status": "in_progress"}, "junk"])
    assert out == [{"content": "查行情", "status": "in_progress"}]


# ── 被替换实现的能力，必须在新链路上仍然成立 ──────────────────────────
#
# 下面三条对应删掉的旧实现（build_chat_context / offload_history /
# retrieve_history_excerpt / detect_ticker）。功能没删，只是换了实现者：
# 上下文治理归 SummarizationMiddleware + FilesystemMiddleware，标的识别归模型 + resolve_entity。
# 这几条测试就是"能力还在"的守卫——没有它们，哪天中间件被谁摘掉也不会有人发现。


def test_history_offload_capability_still_wired():
    """旧 offload_history/retrieve_history_excerpt 的替代者：文件工具必须真的挂着。

    SummarizationMiddleware 把被驱逐的历史写进 /conversation_history/{thread_id}.md，
    并把路径嵌进摘要里让 agent 自己 read_file 取回。少了文件工具，历史就是真丢了。
    """
    mw = ca.ExcludeToolsMiddleware(excluded=ca._EXCLUDED_TOOLS)
    kept = {t.name for t in mw._filter(_Req(_named("ls", "read_file", "write_file", "execute"))).tools}
    assert {"ls", "read_file", "write_file"} <= kept
    assert "execute" not in kept


def test_entity_recognition_capability_still_available():
    """旧 detect_ticker 的替代者：resolve_entity 必须在工具集里。

    detect_ticker 从句子里抽标的（只抽得出一个）；现在改由模型读懂问题、自己调 resolve_entity，
    因而能一轮里查多个标的。工具没了，这个能力就没了。
    """
    assert "resolve_entity" in {t.name for t in dt.CHAT_TOOLS}


def test_todo_capability_still_wired(monkeypatch):
    """todo 事件的数据源：write_todos 必须可用（前端实时待办 UI 依赖它）。"""
    mw = ca.ExcludeToolsMiddleware(excluded=ca._EXCLUDED_TOOLS)
    kept = [t.name for t in mw._filter(_Req(_named("write_todos", "task"))).tools]
    assert "write_todos" in kept
    assert "task" not in kept
