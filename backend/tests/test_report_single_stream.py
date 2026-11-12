"""v1.8.0：研究报告单次流式撰写（一次请求从摘要写到最后一段）。

覆盖：
- SectionStreamParser：标题变体 / 标题被拆在多个 token / 前导正文 / 回跳标题丢弃 / 正文不被整行缓冲；
- _llm_report_stream：事件顺序（摘要 token 先于第一段、每段在下一段开写前即收口）、
  逐 token 外发（模型还在吐字时前端已收到）、思维链只出一次、禁用词单段回退、
  中途异常已写段保留、数据不足段按顺位收口、模板不可用时整体回退。
"""

import asyncio

import pytest

from finresearch.agents.workers import report as rep
from finresearch.agents.workers.section_stream import SectionStreamParser

_QUOTE = {
    "name": "贵州茅台", "industry": "白酒",
    "price": 1523.0, "changePct": 1.24, "pe": 22.4, "pb": 8.1,
    "marketCap": "1.9万亿", "as_of": "2026-09-01 15:00",
    "provenance": {"source": "sina"},
}
_FINS = {
    "years": ["2024", "2025"],
    "rows": [
        {"name": "营业收入", "values": ["100.0亿", "120.0亿"]},
        {"name": "净利润", "values": ["10.0亿", "9.0亿"]},
        {"name": "ROE", "values": ["15.0%", "14.0%"]},
    ],
}
_FV = {"quote": _QUOTE, "fins": _FINS, "profile": {},
       "kline": [{"close": 1.0, "date": "2026-09-01"}], "news": []}
_STATE = {"template": "equity_deep", "task_id": "t1", "normalized_ticker": "600519",
          "llm_config": {"model": "deepseek-chat", "api_key": "k", "base_url": "http://x"}}

_REPORT = (
    "## 投资摘要\n我们认为公司经营稳健，PE 22.4 倍处于合理区间。\n"
    "## 技术面分析\n均线多头排列，短期偏强。\n"
    "## 基本面分析\n营收同比增长，净利润小幅回落。\n"
    "## 估值分析\n估值与 ROE 匹配度尚可。\n"
    "## 行业与公司分析\n白酒行业龙头地位稳固。\n"
)


# ── 解析器 ───────────────────────────────────────────────
def _parse(chunks, titles):
    p = SectionStreamParser(titles)
    events = []
    for c in chunks:
        events += p.feed(c)
    events += p.flush()
    texts: dict[int, str] = {}
    enters = []
    for kind, idx, text in events:
        if kind == "enter":
            enters.append(idx)
        else:
            texts[idx] = texts.get(idx, "") + text
    return enters, texts


TITLES = [("投资摘要", "摘要"), ("技术面分析",), ("基本面分析",)]


def test_parser_char_by_char_routes_sections():
    text = "## 投资摘要\n摘要正文。\n## 技术面分析\n技术正文。\n## 基本面分析\n基本正文。"
    enters, texts = _parse(list(text), TITLES)
    assert enters == [1, 2]
    assert texts[0].strip() == "摘要正文。"
    assert texts[1].strip() == "技术正文。"
    assert texts[2].strip() == "基本正文。"


@pytest.mark.parametrize("heading", ["**技术面分析**", "【技术面分析】", "二、技术面分析：", "### 技术面分析", "技术面分析"])
def test_parser_heading_variants(heading):
    enters, texts = _parse([f"摘要。\n{heading}\n技术。"], TITLES)
    assert enters == [1]
    assert texts[1].strip() == "技术。"


def test_parser_leading_text_without_heading_goes_to_summary():
    enters, texts = _parse(["直接写摘要。\n", "## 技术面分析\n", "技术。"], TITLES)
    assert texts[0].strip() == "直接写摘要。"
    assert enters == [1]


def test_parser_backward_heading_dropped_and_body_not_mixed():
    # 回跳到更早部分的标题：标题行与其后正文都丢弃，直到下一个向后的标题（宁缺勿串段）
    enters, texts = _parse(["## 技术面分析\n技术。\n## 投资摘要\n回跳正文。\n## 基本面分析\n基本。"], TITLES)
    assert enters == [1, 2]
    assert texts[1].strip() == "技术。" and texts[2].strip() == "基本。"
    assert "回跳正文" not in "".join(texts.values())


def test_parser_body_tokens_released_immediately():
    p = SectionStreamParser(TITLES)
    p.feed("## 投资摘要\n")
    # 行中正文 token 不等换行就放行（不会被整行缓冲拖慢）
    assert p.feed("我们认为") == [("text", 0, "我们认为")]
    assert p.feed("经营稳健") == [("text", 0, "经营稳健")]


def test_parser_body_starting_like_title_released_when_diverges():
    p = SectionStreamParser(TITLES)
    p.feed("## 技术面分析\n")
    out = p.feed("技术指标显示偏强，")
    assert "".join(t for k, _, t in out if k == "text") == "技术指标显示偏强，"


# ── 单次流式撰写 ─────────────────────────────────────────
class _Chunk:
    def __init__(self, content, rc=None):
        self.content = content
        self.additional_kwargs = {"reasoning_content": rc} if rc else {}


class _FakeLLM:
    def __init__(self, pieces, *, delay=0.0, fail_after=None, log=None):
        self._pieces = pieces
        self._delay = delay
        self._fail_after = fail_after
        self.calls = 0
        self._log = log

    async def astream(self, _messages):
        self.calls += 1
        for i, piece in enumerate(self._pieces):
            if self._fail_after is not None and i >= self._fail_after:
                raise RuntimeError("upstream reset")
            await asyncio.sleep(self._delay)
            if self._log is not None:
                self._log.append(("llm", i))
            yield piece if isinstance(piece, _Chunk) else _Chunk(piece)


def _run_stream(monkeypatch, llm, fv=_FV, state=_STATE):
    pushed: list = []
    monkeypatch.setattr(rep, "build_llm", lambda **_kw: llm)

    async def main():
        token = rep.PUSH_SINK.set(pushed.append)
        try:
            specs = rep._section_specs(state.get("template", "equity_deep"))
            return specs, await rep._llm_report_stream(dict(state), specs, fv)
        finally:
            rep.PUSH_SINK.reset(token)

    specs, (summary, results) = asyncio.run(main())
    return specs, summary, results, pushed


def _types(pushed):
    return [(p.get("type"), p.get("title", "")) if isinstance(p, dict) else ("progress", "") for p in pushed]


def test_single_request_all_sections_llm_and_ordered(monkeypatch):
    llm = _FakeLLM(list(_REPORT))  # 逐字符：标题必然被拆在多个 token 里
    specs, summary, results, pushed = _run_stream(monkeypatch, llm)
    assert llm.calls == 1  # 整份报告只有一次模型请求
    assert summary[1] == "llm" and summary[0].startswith("我们认为")
    assert [src for _, src in results] == ["llm"] * 4
    assert results[0][0] == "均线多头排列，短期偏强。"

    seq = _types(pushed)
    first_summary_tok = seq.index(("summary_token", ""))
    summary_final = next(i for i, p in enumerate(pushed) if isinstance(p, dict) and p.get("type") == "research_summary_text")
    first_tech_tok = seq.index(("section_token", "技术面分析"))
    assert first_summary_tok < summary_final < first_tech_tok
    # 每段在下一段开写之前就已收口（不是全部写完才统一出结果）
    for cur, nxt in zip([s.title for s in specs], [s.title for s in specs][1:]):
        closed = seq.index(("report_section", cur))
        assert closed < seq.index(("section_start", nxt)) < seq.index(("section_token", nxt))


def test_tokens_pushed_while_model_still_streaming(monkeypatch):
    log: list = []
    pushed_ref: list = []
    llm = _FakeLLM(list(_REPORT), delay=0.001, log=log)
    monkeypatch.setattr(rep, "build_llm", lambda **_kw: llm)

    def sink(ev):
        pushed_ref.append(ev)
        log.append(("push", ev.get("type") if isinstance(ev, dict) else "progress"))

    async def main():
        token = rep.PUSH_SINK.set(sink)
        try:
            specs = rep._section_specs("equity_deep")
            await rep._llm_report_stream(dict(_STATE), specs, _FV)
        finally:
            rep.PUSH_SINK.reset(token)

    asyncio.run(main())
    first_tok = log.index(("push", "summary_token"))
    last_llm = max(i for i, e in enumerate(log) if e[0] == "llm")
    assert first_tok < last_llm  # 首个正文 token 外发时模型远未写完


def test_reasoning_once_then_content(monkeypatch):
    pieces = [_Chunk("", rc="先看估值"), _Chunk("", rc="再看技术面")] + list(_REPORT)
    _specs, _s, _r, pushed = _run_stream(monkeypatch, _FakeLLM(pieces))
    seq = _types(pushed)
    assert seq.index(("report_thinking", "")) < seq.index(("summary_token", ""))
    assert not any(t == "section_thinking" for t, _ in seq)  # 不再按段各自思考


def test_inline_think_tag_not_in_body(monkeypatch):
    pieces = ["<think>", "推理过程", "</think>"] + list(_REPORT)
    _specs, summary, _r, pushed = _run_stream(monkeypatch, _FakeLLM(pieces))
    assert "推理过程" not in summary[0]
    assert any(isinstance(p, dict) and p.get("type") == "report_thinking" for p in pushed)


def test_forbidden_word_only_that_section_falls_back(monkeypatch):
    text = _REPORT.replace("均线多头排列，短期偏强。", "建议买入。")
    _specs, _summary, results, pushed = _run_stream(monkeypatch, _FakeLLM(list(text)))
    assert results[0][1] == "template"
    assert [src for _, src in results[1:]] == ["llm"] * 3
    leaked = "".join(p["text"] for p in pushed if isinstance(p, dict) and p.get("type") == "section_token")
    assert "买入" not in leaked


def test_midstream_failure_keeps_written_sections(monkeypatch):
    cut = _REPORT.index("## 估值分析")
    _specs, summary, results, pushed = _run_stream(monkeypatch, _FakeLLM(list(_REPORT), fail_after=cut))
    assert summary[1] == "llm"
    assert results[0][1] == "llm" and results[1][1] == "llm"
    assert results[2][1] == "template" and results[3][1] == "template"
    assert all(body.strip() for body, _ in results)
    # 每个分析段都有且仅有一次权威收口
    finals = [p["title"] for p in pushed if isinstance(p, dict) and p.get("type") == "report_section"]
    assert sorted(finals) == sorted(s.title for s in _specs)


def test_insufficient_section_closed_in_order(monkeypatch):
    fv = dict(_FV, kline=[], indicators=[])  # 技术面无数据
    text = _REPORT.replace("## 技术面分析\n均线多头排列，短期偏强。\n", "")
    specs, _summary, results, pushed = _run_stream(monkeypatch, _FakeLLM(list(text)), fv=fv)
    assert results[0][1] == "insufficient"
    seq = _types(pushed)
    # 「技术面（数据不足）」在基本面开写之前就按顺位收口
    assert seq.index(("report_section", "技术面分析")) < seq.index(("section_token", "基本面分析"))


def test_missing_heading_section_falls_back(monkeypatch):
    text = _REPORT.replace("## 估值分析\n估值与 ROE 匹配度尚可。\n", "")
    _specs, _summary, results, _pushed = _run_stream(monkeypatch, _FakeLLM(list(text)))
    assert results[2][1] == "template" and results[3][1] == "llm"


def test_no_llm_falls_back_everything(monkeypatch):
    _specs, summary, results, pushed = _run_stream(monkeypatch, None)
    assert summary[1] == "template"
    assert all(src == "template" for _, src in results)
    assert sum(1 for p in pushed if isinstance(p, dict) and p.get("type") == "report_section") == 4


def test_report_node_stream_mode_single_call(monkeypatch):
    llm = _FakeLLM(list(_REPORT))
    monkeypatch.setattr(rep, "build_llm", lambda **_kw: llm)
    state = dict(_STATE, facts=_FV, findings=[], write_mode="stream")
    out = asyncio.run(rep.report_node(state))
    doc = out["draft_report"]
    assert llm.calls == 1
    assert doc["summary"].startswith("我们认为")
    titles = [s["title"] for s in doc["sections"]]
    assert titles[:4] == ["技术面分析", "基本面分析", "估值分析", "行业与公司分析"]


# ── v1.8.0 评审补充：乱序标题 / 阿拉伯编号标题 / 加粗引导句 ─────────────
def test_parser_out_of_order_heading_does_not_mix_sections():
    enters, texts = _parse(["## 基本面分析\nB\n## 技术面分析\nT\n"], TITLES)
    assert enters == [2]
    assert texts[2].strip() == "B"  # 回跳段落的正文被丢弃，绝不串进基本面
    assert 1 not in texts


@pytest.mark.parametrize("heading", ["1. 技术面分析", "1 技术面分析", "2、技术面分析", "第二部分 技术面分析"])
def test_parser_arabic_and_chinese_numbering(heading):
    enters, texts = _parse(list(f"摘要。\n{heading}\n技术。"), TITLES)
    assert enters == [1]
    assert texts[1].strip() == "技术。"


def test_parser_bold_lead_in_is_not_heading():
    enters, texts = _parse(["摘要首句。\n**技术面分析**：偏弱，需观察。\n"], TITLES)
    assert enters == []
    assert "偏弱" in texts[0]


def test_parser_parenthetical_suffix_heading_still_matches():
    enters, _ = _parse(["摘要。\n## 技术面分析（短期）\n技术。"], TITLES)
    assert enters == [1]
