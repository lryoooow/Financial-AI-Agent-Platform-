"""事实块与报告组装的铁律测试（数字零幻觉 / 可溯源 / 引文完整）。

这些断言存在的意义：1.2.0 重构声称修好了"0.0 冒充未获取"，但 price/changePct
仍走 facts.fmt（只映射 None），编造值一路流进摘要与送给模型的事实块。
下面每条都对应一次真实回归。
"""

from __future__ import annotations

import asyncio

import pytest

from finresearch.agents.workers import report as rep
from finresearch.tools import facts


class _FakeStreamLLM:
    """假流式 LLM：astream 逐 token 吐出预设内容（替代旧 _invoke 的一次性返回）。

    报告分段自 1.5.0 起走真流式（llm.astream 逐 token），故这些测试改为 mock astream。
    """

    def __init__(self, tokens):
        self._tokens = list(tokens)
        self.astream_calls = 0

    async def astream(self, _messages):
        self.astream_calls += 1
        for t in self._tokens:
            yield type("Chunk", (), {"content": t})()


FAILED_QUOTE = {
    "name": "贵州茅台", "industry": "白酒",
    "price": 0.0, "change": 0.0, "changePct": 0.0,
    "pe": 0.0, "pb": 0.0, "marketCap": "—",
    "as_of": "2026-07-10 09:30",
    "provenance": {"source": "akshare", "error": "timeout"},
}

GOOD_QUOTE = {
    "name": "贵州茅台", "industry": "白酒",
    "price": 1523.0, "changePct": 1.24, "pe": 22.4, "pb": 8.1,
    "marketCap": "1.9万亿", "as_of": "2026-07-10 15:00",
    "provenance": {"source": "eastmoney-direct"},
}

FINS = {
    "years": ["2024", "2025"],
    "rows": [
        {"name": "营业收入", "values": ["100.0亿", "120.0亿"]},
        {"name": "净利润", "values": ["10.0亿", "9.0亿"]},
        {"name": "ROE", "values": ["15.0%", "14.0%"]},
    ],
}


# ────────────────────────────────────────────────────────────
# 铁律 1：缺失绝不渲染成 0
# ────────────────────────────────────────────────────────────
def test_quote_usable_rejects_zero_price_and_provenance_error():
    assert facts.quote_usable(GOOD_QUOTE)
    assert not facts.quote_usable(FAILED_QUOTE)
    assert not facts.quote_usable({"price": 0.0})
    assert not facts.quote_usable({"price": 12.3, "provenance": {"error": "boom"}})
    assert not facts.quote_usable({})
    assert not facts.quote_usable(None)


def test_zero_pe_pb_never_render_as_numbers():
    assert facts.mult_str(0.0) == facts.MISSING
    assert facts.mult_str(None) == facts.MISSING
    assert facts.measure(0.0, " 元", 2, zero_missing=True) == facts.MISSING
    # 0 是合法涨跌幅（平盘），不能被误判为缺失
    assert facts.pct(0.0, 2) == "+0.00%"


def test_missing_never_glued_to_a_unit():
    """"—（未获取） 倍" / "—（未获取）%" 这类粘连串不得出现。"""
    for s in (facts.mult_str(None), facts.measure(None, " 元"), facts.pct(None)):
        assert s == facts.MISSING
        assert not s.endswith("倍") and not s.endswith("元") and not s.endswith("%")


def test_failed_quote_facts_block_has_no_fabricated_numbers():
    block = facts.render_quote_facts(FAILED_QUOTE)
    assert "0.0" not in block and "0.00" not in block
    assert block.count(facts.MISSING) >= 5
    # 可溯源：失败原因必须披露，不能把失败的源当成功的源报出来
    assert "timeout" in block
    assert "失败" in block


def test_good_quote_facts_block_keeps_units_and_provenance():
    block = facts.render_quote_facts(GOOD_QUOTE)
    assert "1523.00 元" in block
    assert "+1.24%" in block
    assert "22.4 倍" in block
    assert "eastmoney-direct" in block


def test_fallback_summary_never_prints_zero_valuation():
    fv = {"quote": FAILED_QUOTE, "profile": {}, "fins": {"years": [], "rows": []}, "indicators": []}
    summary = rep._compose_summary_block({"template": "equity_deep"}, fv)
    assert "PE 0.0" not in summary
    assert "0.00 元" not in summary
    assert "PE —（未获取）" in summary


def test_summary_node_emits_a_fact_preview_before_report_sections():
    """研究流应可在详细段落生成前拿到摘要，不能等 report_node 全部结束。"""
    result = asyncio.run(rep.summary_node({
        "normalized_ticker": "600519.SH",
        "template": "equity_deep",
        "findings": [
            {"key": "quote", "quote": GOOD_QUOTE},
            {"key": "financials", "financials": FINS},
            {"key": "indicators", "indicators": []},
        ],
    }))
    assert result["draft_summary"]
    assert "贵州茅台" in result["draft_summary"]
    assert result["facts"]["quote"] == GOOD_QUOTE
    assert "summary_ms" in result["timings"]


def test_llm_summary_facts_never_hand_zero_to_the_model():
    """送给模型的事实块是"严格引用"的唯一来源，绝不能含编造值。"""
    m = rep._quote_measures(FAILED_QUOTE)
    assert set(m.values()) == {facts.MISSING}
    m2 = rep._quote_measures(GOOD_QUOTE)
    assert m2["price"] == "1523.00 元" and m2["pe"] == "22.4 倍"


# ────────────────────────────────────────────────────────────
# 铁律 1（续）：MA10 缺失时不得插值伪造
# ────────────────────────────────────────────────────────────
def test_ma10_missing_is_not_interpolated():
    kline = [{"close": 100.0, "ma5": 101.0, "ma20": 99.0}]  # 无 ma10
    im = {"MA5": {"value": "101.0"}, "MA20": {"value": "99.0"}}
    text = rep._ma_arrangement(kline, im)
    assert "10日均线" not in text
    assert "5日均线" in text and "20日均线" in text


def test_ma10_present_is_reported():
    kline = [{"close": 100.0, "ma5": 101.0, "ma10": 100.0, "ma20": 99.0}]
    text = rep._ma_arrangement(kline, {})
    assert "10日均线 100.00 元" in text


def test_zero_moving_average_is_treated_as_not_computed():
    """行情工具用 0.0 填"样本不足算不出的均线"——股价均线不可能为 0。"""
    import re

    kline = [{"close": 100.0, "ma5": 101.0, "ma10": 0.0, "ma20": 99.0}]
    text = rep._ma_arrangement(kline, {})
    assert "10日均线" not in text
    # 不得出现独立的 "0.00 元"（注意 "100.00 元" 里也含这个子串，需边界）
    assert not re.search(r"(?<!\d)0\.00 元", text)

    # 全为 0 → 数据不足，不得研判
    assert "数据不足" in rep._ma_arrangement([{"close": 0.0, "ma5": 0.0, "ma20": 0.0}], {})


# ────────────────────────────────────────────────────────────
# 铁律 3：合规自检只检测，不盲替换；引文永不改写
# ────────────────────────────────────────────────────────────
def test_check_generated_detects_without_rewriting():
    hits = rep.check_generated("我们不建议买入，短期承压。")
    assert hits == ["买入"]
    # 旧 _sanitize 会把这句改成"不建议建议关注（偏多）"——语义崩坏
    assert not hasattr(rep, "_sanitize")


def test_forbidden_section_output_sanitized_not_discarded(monkeypatch):
    """裸指令词收口替换：流不中断、不落模板、词被换成方向性表述。

    曾经"流中命中即停流回退模板"——降级段拿同样事实块大概率再次命中，
    生成零 token 的模板段，是"报告正文整块出现"的主因之一。
    引文/数据口径豁免由 check_generated 分层保证，这里锁裸词的新语义。
    """
    llm = _FakeStreamLLM(["建议", "买入", "。"])
    monkeypatch.setattr(rep, "build_llm", lambda **_kwargs: llm)
    body, source = asyncio.run(rep._llm_section(
        {"template": "equity_deep", "normalized_ticker": "600519.SH"},
        rep._SPEC_VALUATION,
        {"quote": GOOD_QUOTE, "fins": FINS, "profile": {}},
    ))

    assert llm.astream_calls == 1  # 只一次流式，无第二次重写调用
    assert source == "llm"  # 不再因裸词落模板
    assert "买入" not in body
    assert "看多" in body  # 方向性替换生效
    # 引文与数据口径豁免：含禁用词的引文/搭配原样保留
    quoted = rep.sanitize_generated("「北向资金净买入3.2亿元」创纪录")[0]
    assert quoted == "「北向资金净买入3.2亿元」创纪录"
    compound = rep.sanitize_generated("获买入评级，净卖出减少")[0]
    assert compound == "获买入评级，净卖出减少"


def test_quoted_headlines_are_never_sanitized():
    fv = {
        "quote": GOOD_QUOTE, "profile": {}, "fins": FINS, "indicators": [],
        "news": [{"title": "机构买入评级密集出现", "source": "东方财富"}], "web": [],
    }
    body = rep._section_industry_company(fv)
    assert "「机构买入评级密集出现」" in body  # 引文一字未改


# ────────────────────────────────────────────────────────────
# 事实块装配：【约束】只出现一次
# ────────────────────────────────────────────────────────────
def test_fact_footer_appears_exactly_once_per_assembled_block():
    for build in (rep._facts_technical, rep._facts_valuation, rep._facts_industry, rep._facts_scenario):
        block = build({"quote": GOOD_QUOTE, "fins": FINS, "indicators": [], "news": [], "web": []})
        assert block.count("【约束】") == 1, build.__name__
    chat = facts.compose_facts(facts.render_quote_facts(GOOD_QUOTE))
    assert chat.count("【约束】") == 1


# ────────────────────────────────────────────────────────────
# 模板驱动组装：四模板不再产出同一套栏目
# ────────────────────────────────────────────────────────────
def test_templates_assemble_different_sections():
    keys = {t: [s.key for s in rep._section_specs(t)] for t in ("equity_deep", "industry", "earnings", "risk")}
    assert len({tuple(v) for v in keys.values()}) == 4, "四模板栏目集合应各不相同"
    assert "industry_landscape" in keys["industry"]
    assert "earnings_review" in keys["earnings"]
    assert "risk_scenario" in keys["risk"]
    assert "industry_landscape" not in keys["equity_deep"]
    assert len(keys["industry"]) == len(keys["earnings"]) == len(keys["risk"]) == 1
    assert set(keys["industry"] + keys["earnings"] + keys["risk"]).isdisjoint(
        {"technical", "fundamentals", "valuation"}
    )


def test_generated_section_has_hard_character_cap():
    text, truncated = rep._bound_generated_text("很长的分析。" * 100, 80)
    assert truncated is True
    assert len(text) <= 80


def test_section_llm_receives_completion_token_cap(monkeypatch):
    captured = {}

    def fake_build_llm(**kwargs):
        captured.update(kwargs)
        return _FakeStreamLLM(["事实", "分析", "。"])

    monkeypatch.setattr(rep, "build_llm", fake_build_llm)
    asyncio.run(rep._llm_section(
        {"template": "equity_deep", "normalized_ticker": "600519.SH"},
        rep._SPEC_VALUATION,
        {"quote": GOOD_QUOTE, "fins": FINS, "profile": {}},
    ))
    assert captured["max_tokens"] == rep.settings.report_section_max_tokens


def test_unknown_template_falls_back_to_equity_deep():
    assert rep._section_specs("nope") == rep._section_specs("equity_deep")


def test_template_focus_reaches_section_prompt():
    from finresearch.agents.prompts import section_system_prompt

    risk = section_system_prompt("technical", "risk")
    deep = section_system_prompt("technical", "equity_deep")
    assert risk != deep, "模板侧重必须进入段落 system prompt"
    assert "风险评估侧重" in risk


def test_landscape_section_requires_real_evidence():
    """行业景气栏在没有真实资讯证据时不得组装。"""
    spec = rep._SPEC_LANDSCAPE
    assert not spec.has_data({"news": [], "web": []})
    assert spec.has_data({"news": [{"title": "t"}], "web": []})


def test_landscape_fallback_only_lists_evidence_no_boilerplate():
    """模型不可用时只摆原始证据，不产出"行业景气度受宏观经济…"这类假分析。"""
    fv = {"quote": GOOD_QUOTE, "profile": {},
          "news": [{"title": "白酒动销回暖", "source": "东方财富"}], "web": []}
    body = rep._section_landscape(fv)
    assert "「白酒动销回暖」" in body
    assert "宏观经济" not in body and "政策导向" not in body


def test_scenario_fallback_lists_monitors_not_fabricated_scenarios():
    fv = {"quote": GOOD_QUOTE, "fins": FINS,
          "indicators": [{"name": "MACD", "value": "-0.2", "signal": "死叉"}]}
    body = rep._section_scenario(fv)
    assert "MACD（当前信号：死叉）" in body
    assert "目标价" not in body
    assert "乐观情形" not in body  # 旧实现的三情景套话


# ────────────────────────────────────────────────────────────
# 可溯源：证据分层栏必须如实说明每段是谁写的
# ────────────────────────────────────────────────────────────
def test_evidence_section_reports_actual_generation_source():
    fv = {"quote": GOOD_QUOTE, "news": [], "web": [], "web_configured": False}
    gen = {"技术面分析": "llm", "基本面分析": "template", "情景分析": "insufficient"}
    body = rep._section_evidence_traceback([], fv, gen)
    assert "模型撰写：技术面分析" in body
    assert "规则模板撰写：基本面分析" in body
    assert "数据不足未展开：情景分析" in body


def test_evidence_section_does_not_claim_llm_when_all_template():
    fv = {"quote": GOOD_QUOTE, "news": [], "web": [], "web_configured": False}
    body = rep._section_evidence_traceback([], fv, {"技术面分析": "template"})
    assert "模型撰写" not in body
    assert "规则模板撰写" in body


def test_evidence_section_discloses_failed_market_source():
    fv = {"quote": FAILED_QUOTE, "news": [], "web": [], "web_configured": False}
    body = rep._section_evidence_traceback([], fv, {})
    assert "取数失败" in body and "timeout" in body


# ────────────────────────────────────────────────────────────
# 空返回兜底：LLM 偶发返回空 content 时，不能产出空白章节
# （用户报告"多数分析章节空白、有些有有些没有"的根因——空返回偶发，故不稳定）
# ────────────────────────────────────────────────────────────
def _good_fv():
    return {
        "quote": GOOD_QUOTE, "profile": {"name": "贵州茅台", "industry": "白酒"},
        "fins": FINS, "indicators": [], "kline": [], "news": [], "web": [],
    }


@pytest.mark.parametrize("empty", ["", "   ", "\n\n  \n"])
def test_empty_llm_output_falls_back_to_template_not_blank(monkeypatch, empty):
    """LLM 返回空/纯空白时，估值段必须回退模板（body 非空、source=template），
    绝不能把空字符串当成功的 llm 输出返回——那会让前端渲染成一片空白的标题。"""
    monkeypatch.setattr(rep, "build_llm", lambda **_kwargs: _FakeStreamLLM([empty]))

    state = {"template": "equity_deep", "task_id": "t", "llm_config": {"api_key": "x", "model": "m"}}
    body, source = asyncio.run(rep._llm_section(state, rep._SPEC_VALUATION, _good_fv()))
    assert body.strip(), "空返回不该产出空段"
    assert source == "template", f"空返回应标注来源为 template，实际 {source}"


def test_nonempty_llm_output_still_uses_llm(monkeypatch):
    """回归：正常返回仍走 llm，不被空返回兜底误伤。"""
    tokens = ["估值处于行业中枢，", "PE 22.4 倍与 ROE 支撑相匹配，", "短期无明显高估。"]
    monkeypatch.setattr(rep, "build_llm", lambda **_kwargs: _FakeStreamLLM(tokens))

    state = {"template": "equity_deep", "task_id": "t", "llm_config": {"api_key": "x", "model": "m"}}
    body, source = asyncio.run(rep._llm_section(state, rep._SPEC_VALUATION, _good_fv()))
    assert source == "llm" and body.strip()


def test_financial_indicator_row_names_match_consumers(monkeypatch):
    """fundamentals 生产方行名必须与 report/facts 消费方查询名一致。

    曾经行名是"扣非净利润"而值优先取"净利润"，消费方 row_vals(fins, "净利润")
    永远查不到 → 报告里净利润一栏长期显示"—（未获取）"，即使接口完全正常。
    """
    import sys
    import types

    import pandas as pd

    from finresearch.cache import slow_cache
    from finresearch.tools import fundamentals

    # 双源：摘要源（主，营收绝对值/归母净利/毛利率）+ indicator 源（原生同比）
    abstract = pd.DataFrame(
        [
            {"选项": "常用指标", "指标": "归母净利润", "20241231": 61e8, "20251231": 71e8},
            {"选项": "常用指标", "指标": "营业总收入", "20241231": 151e8, "20251231": 171e8},
            {"选项": "常用指标", "指标": "毛利率", "20241231": 91.0, "20251231": 91.5},
            {"选项": "常用指标", "指标": "净资产收益率(ROE)", "20241231": 30.0, "20251231": 31.0},
            {"选项": "常用指标", "指标": "资产负债率", "20241231": 20.0, "20251231": 19.0},
        ]
    )
    df = pd.DataFrame([
        {"日期": "2024-12-31", "主营业务收入(元)": 150e8, "净利润": 60e8,
         "主营业务收入增长率(%)": 10.0, "净利润增长率(%)": 8.0,
         "净资产收益率(%)": 30.0, "销售毛利率(%)": 91.0, "资产负债率(%)": 20.0},
        {"日期": "2025-12-31", "主营业务收入(元)": 170e8, "净利润": 70e8,
         "主营业务收入增长率(%)": 13.3, "净利润增长率(%)": 16.7,
         "净资产收益率(%)": 31.0, "销售毛利率(%)": 91.5, "资产负债率(%)": 19.0},
    ])
    fake_ak = types.SimpleNamespace()
    fake_ak.stock_financial_abstract = lambda **_: abstract
    fake_ak.stock_financial_analysis_indicator = lambda **_: df
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    slow_cache.clear()

    fins = fundamentals.get_financial_indicators("600519.SH")["financials"]
    names = {r["name"] for r in fins["rows"]}
    # report._section_fundamentals 与 facts.render_financial_facts 查询的行名全集
    for needed in ("营业收入", "净利润", "营收同比", "净利润同比", "ROE", "毛利率", "资产负债率"):
        assert needed in names, f"行名 {needed} 缺失，消费方将永远查不到"
    net = next(r for r in fins["rows"] if r["name"] == "净利润")
    assert net["values"][-1] == "71.00亿"  # 摘要源归母净利润优先（71e8），不是 indicator 的 70e8
    rev = next(r for r in fins["rows"] if r["name"] == "营业收入")
    assert rev["values"][-1] == "171.00亿"  # 营收绝对值来自摘要源
    gm = next(r for r in fins["rows"] if r["name"] == "毛利率")
    assert gm["values"][-1] == "91.5%"  # 毛利率来自摘要源
    assert fins["periods"][-1] == "2025-12-31"  # 报告期披露


def test_financial_yoy_prefers_native_growth_and_guards_period_mismatch():
    """中报（H1 累计）不得与上年年报直接相除；接口原生同比列优先；报告期必须披露。

    曾经拿当年中报对上年年报算同比，4~10 月的报告"营收同比"稳定输出 -50% 式
    错误值，且"最新一期（2026）"不披露是中报还是年报，用户无从察觉。
    """
    # 1) 接口原生同比列优先（对比口径天然正确）
    fins_native = {
        "years": ["2025", "2026"], "periods": ["2025-12-31", "2026-06-30"],
        "rows": [
            {"name": "营业收入", "values": ["100.0亿", "45.0亿"]},
            {"name": "营收同比", "values": ["10.0%", "12.5%"]},
            {"name": "净利润", "values": ["20.0亿", "9.0亿"]},
        ],
    }
    assert facts.yoy_from_fins(fins_native, "营业收入", "营收同比") == "+12.5%"
    block = facts.render_financial_facts(fins_native)
    assert "12.5%" in block
    assert "2026-06-30（中报）" in block  # 披露报告期口径
    assert "-55" not in block and "-55.0" not in block  # 不得出现 45/100-1 的错算

    # 2) 无原生列 + 报告期不同形（中报 vs 年报）→ MISSING，宁可缺失不编数
    fins_mismatch = {
        "years": ["2025", "2026"], "periods": ["2025-12-31", "2026-06-30"],
        "rows": [{"name": "净利润", "values": ["20.0亿", "9.0亿"]}],
    }
    assert facts.yoy_from_fins(fins_mismatch, "净利润", "净利润同比") == facts.MISSING
    assert facts.periods_comparable(fins_mismatch) is False

    # 3) 报告期同形（年报对年报）→ 仍可由 levels 重算
    fins_ok = {
        "years": ["2024", "2025"], "periods": ["2024-12-31", "2025-12-31"],
        "rows": [{"name": "净利润", "values": ["10.0亿", "12.0亿"]}],
    }
    assert facts.yoy_from_fins(fins_ok, "净利润", "净利润同比") == "+20.0%"
    assert facts.periods_comparable(fins_ok) is True

    # 4) 旧形状（无 periods 字段）保持旧行为，不误伤
    legacy = {"rows": [{"name": "净利润", "values": ["10.0亿", "12.0亿"]}]}
    assert facts.yoy_from_fins(legacy, "净利润", "净利润同比") == "+20.0%"
    assert facts.latest_period_label({"years": ["2024", "2025"]}) == "2025"


def test_check_generated_layered_exemptions():
    """分层检测：引文（「」）与数据口径名词豁免，裸指令词仍命中。

    曾经纯子串扫描：A 股新闻标题高频含"净买入"，模型照引文要求原样引用
    → 命中 → 整篇 7 秒作废降级 → 降级段再次命中 → 模板零 token。
    """
    assert rep.check_generated("北向资金净买入3.2亿元") == []
    assert rep.check_generated("「主力净买入创纪录」") == []
    assert rep.check_generated("获买入评级，机构净卖出减少") == []
    assert rep.check_generated("") == []
    assert rep.check_generated("建议买入该股") == ["买入"]
    assert rep.check_generated("止损位设在3200点") == ["止损"]
    # 引文内的裸指令词也豁免（提示词要求引文一字不改）
    assert rep.check_generated("「散户纷纷清仓离场」") == []


def test_whole_report_keeps_clean_sections_on_midstream_failure(monkeypatch):
    """整篇中途异常：已切完的干净段保留，只有缺失段进入补写——
    用户不再看到"全文消失重来"。"""
    class _BoomAfterTwo:
        def __init__(self):
            self.calls = 0

        def astream(self, _messages):
            async def _gen():
                yield type("Chunk", (), {"content": "【技术面分析】\n"})()
                yield type("Chunk", (), {"content": "均线多头排列。\n"})()
                yield type("Chunk", (), {"content": "【基本面分析】\n"})()
                yield type("Chunk", (), {"content": "营收"})()
                raise RuntimeError("connection reset")
            return _gen()

    monkeypatch.setattr(rep, "build_llm", lambda **_kwargs: _BoomAfterTwo())
    state = {"template": "equity_deep", "normalized_ticker": "600519.SH",
             "llm_config": {"model": "deepseek-chat", "api_key": "k"}}
    fv = {"quote": GOOD_QUOTE, "profile": {}, "fins": FINS,
          "indicators": [{"name": "MACD", "value": "-8.60", "signal": "空"}],
          "news": [], "web": []}
    specs = [s for s in rep._section_specs("equity_deep") if s.has_data(fv)]
    assert any(s.title == "技术面分析" for s in specs)  # fixture 必须让技术面 spec 可用
    whole = asyncio.run(rep._llm_report(state, specs, fv))
    assert "技术面分析" in whole and whole["技术面分析"][1] == "llm"
    assert "均线多头排列。" in whole["技术面分析"][0]
    # 中断在"基本面分析"中途：该段不完整，不得保留
    assert "基本面分析" not in whole
