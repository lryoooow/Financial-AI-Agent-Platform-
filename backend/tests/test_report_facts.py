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


def test_forbidden_section_output_falls_back_without_a_second_model_call(monkeypatch):
    """合规检测保留：流式中命中禁用词即停流回退模板，不为同一段发起第二次模型调用。"""
    llm = _FakeStreamLLM(["建议", "买入", "。"])
    monkeypatch.setattr(rep, "build_llm", lambda **_kwargs: llm)
    body, source = asyncio.run(rep._llm_section(
        {"template": "equity_deep", "normalized_ticker": "600519.SH"},
        rep._SPEC_VALUATION,
        {"quote": GOOD_QUOTE, "fins": FINS, "profile": {}},
    ))

    assert llm.astream_calls == 1  # 只一次流式，无第二次重写调用
    assert source == "template"
    assert "事实模板" in body


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
    chat = facts.render_chat_facts(GOOD_QUOTE, [{"title": "t", "source": "s"}])
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
