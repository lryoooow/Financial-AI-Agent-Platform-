"""整篇一次撰写（2.0.3）的契约测试。

守三条用户实测出来的问题：
1. 正文不许被人为截断（旧实现每段 320 字符硬断流，末尾补"…"）
2. 正文不许含 Markdown，但 emoji 要留着
3. 整篇 token 流要能正确切成分段事件（前端契约不变的前提）
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import finresearch.agents.workers.report as rep  # noqa: E402
from finresearch.tools.text import strip_md  # noqa: E402

TITLES = ["技术面分析", "基本面分析", "估值分析"]


def _collect(tokens, titles=TITLES):
    """把 token 序列喂给 splitter，返回 (事件列表, 最终各段正文)。"""
    events = []
    splitter = rep._SectionSplitter(titles, events.append)
    for tok in tokens:
        splitter.feed(tok)
    return events, splitter.close()


# ── 段切分 ────────────────────────────────────────────────────────────
def test_splitter_cuts_sections_by_heading():
    _events, bodies = _collect(["【技术面分析】\n", "均线空头排列。\n", "【基本面分析】\n", "营收增长。"])
    assert bodies == {"技术面分析": "均线空头排列。", "基本面分析": "营收增长。"}


def test_splitter_handles_heading_split_across_chunks():
    """标题被切成多个 token 是流式常态，行缓冲必须扛住。"""
    _events, bodies = _collect(["【技术", "面分析", "】", "\n", "均线空头。"])
    assert bodies == {"技术面分析": "均线空头。"}


def test_splitter_keeps_heading_out_of_body():
    """标题行不进正文——前端另有 title 字段渲染，正文再来一遍就是重复。"""
    _events, bodies = _collect(["【技术面分析】\n均线空头排列。"])
    assert "【技术面分析】" not in bodies["技术面分析"]
    token_texts = "".join(e["text"] for e in _events if e["type"] == "section_token")
    assert "【技术面分析】" not in token_texts


def test_splitter_emits_section_start_once_per_section():
    events, _bodies = _collect(["【技术面分析】\n", "甲。\n", "乙。\n", "【基本面分析】\n", "丙。"])
    starts = [e["title"] for e in events if e["type"] == "section_start"]
    assert starts == ["技术面分析", "基本面分析"]


def test_splitter_accepts_common_heading_styles():
    """模型不一定照着【标题】写——只认一种写法，换个写法就一段都切不出来，
    整篇被判空后静默降级回分段撰写，用户看到的还是"一段段等"。"""
    styles = [
        "【技术面分析】",
        "技术面分析",
        "## 技术面分析",
        "### 技术面分析",
        "**技术面分析**",
        "1. 技术面分析",
        "一、技术面分析",
        "技术面分析：",
        "  【技术面分析】  ",
        "- 技术面分析",
        "[技术面分析]",
        "第一部分 技术面分析",
    ]
    for style in styles:
        _events, bodies = _collect([f"{style}\n", "均线空头排列。"])
        assert bodies.get("技术面分析") == "均线空头排列。", f"未识别标题写法: {style!r}"


def test_splitter_does_not_mistake_prose_for_heading():
    """宽容匹配不能宽容到把正文当标题——长句、含标题词的句子都必须留在正文里。"""
    prose = "从技术面分析来看，该股短期均线已经形成空头排列并且成交量持续萎缩。"
    _events, bodies = _collect(["【技术面分析】\n", prose])
    assert bodies["技术面分析"] == prose
    assert set(bodies) == {"技术面分析"}


def test_splitter_keeps_sentence_ending_with_section_name():
    """正文里恰好以段名结尾的句子不能被吞掉。

    实测踩到过：装饰剥离连行尾句号一起剥了，于是"……以上是技术面分析。"归一成
    "技术面分析"，整句被当成标题，正文末尾平白少一句。
    """
    prose = "该股走势偏弱，以上是技术面分析。"
    _events, bodies = _collect(["【技术面分析】\n", prose])
    assert bodies["技术面分析"] == prose


def test_splitter_treats_unknown_heading_as_body():
    """模型自己发挥的小标题按正文处理，不能切出幽灵段。"""
    _events, bodies = _collect(["【技术面分析】\n", "【我编的小标题】\n", "正文。"])
    assert set(bodies) == {"技术面分析"}
    assert "我编的小标题" in bodies["技术面分析"]


def test_single_section_template_adopts_untitled_body():
    """单段模板（industry/earnings/risk）漏写标题时，正文直接归给那一段。

    否则整篇被判空 → 降级重跑一轮，白白多花一次调用和一次等待。
    """
    _events, bodies = _collect(["光伏设备行业 PE 25.3 倍，", "估值处于中枢水平。"], ["行业景气与竞争格局"])
    assert bodies == {"行业景气与竞争格局": "光伏设备行业 PE 25.3 倍，估值处于中枢水平。"}


def test_multi_section_template_does_not_adopt_untitled_body():
    """多段模板不能认领：分不清哪段是哪段，宁可判空降级重来。"""
    _events, bodies = _collect(["一段没有任何标题的正文。"])
    assert bodies == {}


def test_splitter_drops_content_before_first_heading():
    """模型开场白（"好的，以下是报告"）没有归属段，不该混进任何一段正文。"""
    _events, bodies = _collect(["好的，以下是报告。\n", "【技术面分析】\n", "均线空头。"])
    assert bodies == {"技术面分析": "均线空头。"}


def test_splitter_streams_long_paragraph_without_waiting_for_newline():
    """长段落不能卡在行缓冲里——用户要看到字在动，不是等一整段憋完才闪出来。"""
    events, _bodies = _collect(["【技术面分析】\n", "均", "线", "空", "头", "排", "列"])
    tokens = [e for e in events if e["type"] == "section_token"]
    assert len(tokens) >= 5, "逐字输入应产生多个 section_token，而非攒成一条"


def test_splitter_close_flushes_trailing_line():
    """模型不以换行结尾是常态，收尾必须把残留行冲出来。"""
    _events, bodies = _collect(["【技术面分析】\n", "没有结尾换行"])
    assert bodies["技术面分析"] == "没有结尾换行"


# ── 不许有人为截断 ────────────────────────────────────────────────────
def test_whole_report_path_has_no_character_cap():
    """喂一段远超旧 320 字符上限的流，必须原样完整保留、末尾不带省略号。

    这正是用户报的问题：每段刚写到 200 字就被 break 掉，末尾补个"…"。
    """
    long_body = "这是一段很长的技术面分析。" * 80          # 约 1040 字符
    _events, bodies = _collect([f"【技术面分析】\n{long_body}"])
    assert bodies["技术面分析"] == long_body
    assert len(bodies["技术面分析"]) > 1000
    assert not bodies["技术面分析"].endswith("…")


def test_section_specs_no_longer_dictate_word_count():
    """段任务提示词里不许再写死字数区间——篇幅归思考档位管。"""
    import re

    from finresearch.agents.prompts import SECTION_TASK

    for key, task in SECTION_TASK.items():
        assert not re.search(r"\d+\s*-\s*\d+\s*字", task), f"{key} 仍写死了字数区间"


# ── 格式：去 Markdown，留 emoji ───────────────────────────────────────
def test_report_output_spec_bans_markdown_and_allows_emoji():
    from finresearch.agents.prompts import REPORT_OUTPUT_SPEC, section_system_prompt

    assert "Markdown" in REPORT_OUTPUT_SPEC
    assert "emoji" in REPORT_OUTPUT_SPEC
    # 报告链路此前完全没有格式约束，模型自然会写 ** 和 ###
    assert "Markdown" in section_system_prompt("technical", "equity_deep")


def test_strip_md_removes_markup_but_keeps_emoji():
    """清洗是硬保证（提示词只是软约束），但不能误伤 emoji 与正文里的符号。"""
    cleaned = strip_md("### 毛利率\n📊 **营收**同比 +440.1%，3*5 的 snake_case 保持原样")
    assert "📊" in cleaned
    assert "**" not in cleaned
    assert "###" not in cleaned
    assert "营收" in cleaned and "毛利率" in cleaned
    # 只删「处在语法位置」的符号：行中间的 * _ # 是正文，不能动
    assert "3*5" in cleaned and "snake_case" in cleaned
    assert "排名 #1" in strip_md("排名 #1")

    bullets = strip_md("- 第一条\n- 第二条")
    assert "- 第一条" not in bullets and "第一条" in bullets


def test_whole_report_strips_markdown_from_bodies():
    """整篇路径返回给上层的 body 必须已清洗——前端是 whitespace-pre-wrap 纯文本渲染。"""
    spec = rep._SPEC_TECHNICAL
    fv = {"kline": [{"close": 10.0}], "indicators": [{"name": "MA5", "value": "1", "signal": "多头"}]}

    class _FakeLLM:
        async def astream(self, _messages):
            for tok in ["【技术面分析】\n", "**均线**空头排列。"]:
                await asyncio.sleep(0)
                yield type("C", (), {"content": tok})()

    pushed = []
    original_llm, original_push = rep.build_llm, rep._stream_push
    rep.build_llm = lambda **_kw: _FakeLLM()
    rep._stream_push = pushed.append
    try:
        out = asyncio.run(rep._llm_report(
            {"template": "equity_deep", "normalized_ticker": "600519.SH"}, [spec], fv,
        ))
    finally:
        rep.build_llm, rep._stream_push = original_llm, original_push

    body, source = out["技术面分析"]
    assert source == "llm"
    assert "**" not in body and "均线空头排列" in body


def test_whole_report_returns_empty_when_llm_unavailable():
    """模型不可用返回空 dict，由 report_node 降级到分段——不能假装成功。"""
    spec = rep._SPEC_TECHNICAL
    fv = {"kline": [{"close": 10.0}], "indicators": [{"name": "MA5", "value": "1", "signal": "多头"}]}
    original = rep.build_llm
    rep.build_llm = lambda **_kw: None
    try:
        out = asyncio.run(rep._llm_report({"template": "equity_deep"}, [spec], fv))
    finally:
        rep.build_llm = original
    assert out == {}
