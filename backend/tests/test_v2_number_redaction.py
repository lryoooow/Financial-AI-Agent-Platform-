"""Regression: 11:07 live cutoff at 'PB 13.37 倍为行业（' (derived ratio 4.4 倍) and the redaction-not-truncation design."""

from finresearch.core.guardrails.number_evidence import NumberEvidence
from finresearch.core.guardrails.sanitizer import AnswerStreamSanitizer

FACTS = (
    "get_realtime_quote: price 86.8, changePct -0.92, pe 40.4, pb 13.37, marketCap 22977000000\n"
    "get_technical_indicators: MA60 106.13, MA20 92.38\n"
    "search_industry: 化学制药 pe_ttm 32.05, pb 3.04, dividend_yield 1.41\n"
    "get_financial_indicators: 毛利率 93.0%, 2025 毛利率 90.4%, 营业收入 15.36亿, 净利润 6.38亿\n"
    "search_news: Q1 确认首付款 6.62 亿元，Q1 营收 9.05 亿元，Q2 净利 740 万元，Q3 单季亏损 174 万元。"
)


def _ne() -> NumberEvidence:
    return NumberEvidence(FACTS)


def _stream(ne: NumberEvidence, doc: str, step: int) -> tuple[str, AnswerStreamSanitizer]:
    san = AnswerStreamSanitizer(
        lambda a, streaming=False: [],
        numeric_locator=lambda a, streaming=False: ne.locate(a, streaming=streaming, financial_only=True),
    )
    out = []
    for i in range(0, len(doc), step):
        d = san.feed(doc[i : i + step])
        assert not d.compliance_triggered, doc[max(0, i - 40) : i + step]
        out.append(d.visible_text)
    tail = san.flush()
    assert not tail.compliance_triggered
    out.append(tail.visible_text)
    return "".join(out), san


def test_derived_ratio_premium_share_pp_and_difference_are_verified():
    ne = _ne()
    ok = [
        "PB 13.37 倍为行业（化学制药）PB 3.04 倍的 4.4 倍。",
        "PB 是行业的 4 倍以上（13.37 倍 vs 3.04 倍）。",
        "PE 40.4 倍较行业 PE(TTM) 32.05 倍溢价 26%。",
        "Q1 首付款 6.62 亿元占 Q1 营收 9.05 亿元的 73%。",
        "毛利率 93.0% 较 2025 年 90.4% 提升 2.6 个百分点。",
        "现价 86.8 元较 MA60 106.13 元低 19.33 元。",
    ]
    for s in ok:
        assert ne.check(s, financial_only=True) == [], s


def test_wrong_arithmetic_and_wrong_direction_are_still_rejected():
    ne = _ne()
    bad = [
        "PB 13.37 倍为行业 PB 3.04 倍的 5.4 倍。",        # wrong ratio
        "PB 是行业的 5 倍以上（13.37 倍 vs 3.04 倍）。",   # wrong bound direction
        "PE 40.4 倍较行业 32.05 倍溢价 40%。",            # wrong premium
        "毛利率 93.0% 较 90.4% 提升 2.6%。",               # pp written as relative % is not accepted
        "PB 为行业的 4.4 倍。",                            # no operands written -> unverifiable
        "公司 2027 年营收将达到 31.5 亿元。",               # pure fabrication
    ]
    for s in bad:
        assert ne.check(s, financial_only=True) != [], s


def test_locate_returns_original_spans_including_fullwidth_text():
    ne = _ne()
    doc = "营收将达到 ３１.５ 亿元，PB 13.37 倍。"
    spans = [(doc[s:e], r) for s, e, r in ne.locate(doc, financial_only=True)]
    assert spans == [("３１.５ 亿元", "unsupported_number")]


def test_live_1107_turn_streams_to_completion_with_local_redaction():
    ne = _ne()
    doc = (
        "【核心结论】💡 前三季营收 15.36 亿、净利 6.38 亿——增长几乎全部来自首付款（Q1 确认 6.62 亿），"
        "剔除后 Q2 净利 740 万、Q3 单季 -174 万。股价运行在 MA60 下方（MA60=106.13，现价 86.8），"
        "PB 13.37 倍为行业（化学制药）PB 3.04 倍的 4.4 倍，PE 40.4 倍较行业 32.05 倍溢价 26%。\n"
        "· 编造测试：公司 2027 年营收将达到 31.5 亿元，净利率 41.3%。\n"
        "仅供参考，不构成投资建议。"
    )
    for step in (1, 2, 3, 7, 40):
        text, san = _stream(ne, doc, step)
        assert "4.4 倍" in text and "溢价 26%" in text and "仅供参考，不构成投资建议。" in text
        assert "31.5" not in text and "41.3" not in text
        assert text.count(AnswerStreamSanitizer.REDACTION) == 2
        assert san.redactions == 2 and not san.violated


def test_operands_after_the_number_are_waited_for_until_sentence_end():
    ne = _ne()
    doc = "PB 约为行业的 4.4 倍（13.37 倍 / 3.04 倍），估值偏高。仅供参考。"
    for step in (1, 2, 5):
        text, san = _stream(ne, doc, step)
        assert text == doc and san.redactions == 0


def test_hallucination_burst_still_hard_stops():
    ne = _ne()
    doc = "；".join(f"指标{i} 为 {i}.37 亿元" for i in range(11, 22)) + "。仅供参考。"
    san = AnswerStreamSanitizer(
        lambda a, streaming=False: [],
        numeric_locator=lambda a, streaming=False: ne.locate(a, streaming=streaming, financial_only=True),
        max_redactions=8,
    )
    triggered = False
    emitted = []
    for ch in doc:
        d = san.feed(ch)
        emitted.append(d.visible_text)
        triggered |= d.compliance_triggered
        if triggered:
            break
    if not triggered:
        d = san.flush()
        triggered = d.compliance_triggered
    assert triggered and san.numeric_violated


def test_compliance_violation_still_hard_stops_even_with_locator():
    ne = _ne()
    san = AnswerStreamSanitizer(
        lambda a, streaming=False: ["advice"] if "建议买入" in a else [],
        numeric_locator=lambda a, streaming=False: ne.locate(a, streaming=streaming, financial_only=True),
    )
    triggered = any(san.feed(ch).compliance_triggered for ch in "现价 86.8 元，建议买入。")
    assert triggered and san.violated and not san.numeric_violated
