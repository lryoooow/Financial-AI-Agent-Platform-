"""v1.8.0：Codex 式对话自动压缩（纯函数部分）。"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from finresearch.agents.deep import compaction as c
from finresearch.llm import context_window_tokens


def test_thread_ids_keep_generation_zero_compatible():
    assert c.thread_id_for("abc", 0) == "abc"
    assert c.thread_id_for("abc", 2) == "abc:g2"
    assert c.all_thread_ids("abc", 2) == ["abc", "abc:g1", "abc:g2"]


def test_decide_uses_chinese_aware_estimate():
    msgs = [HumanMessage("茅台" * 3000), AIMessage("结论" * 3000)]
    d = c.decide(msgs, "下一问", window=16_000, threshold_pct=70)
    # 1.2 万汉字 ≈ 8000 token（中文 1.5 字/token）+ 系统开销 8000 → 超过 11200 阈值
    assert d.needed and d.threshold == 11_200
    assert not c.decide([HumanMessage("你好")], "嗯", window=64_000, threshold_pct=70).needed


def test_threshold_pct_clamped():
    assert c.decide([], "", window=100_000, threshold_pct=5).threshold == 40_000
    assert c.decide([], "", window=100_000, threshold_pct=99).threshold == 90_000


def test_render_transcript_keeps_newest_and_previews_tools():
    msgs = [HumanMessage(f"问题{i}" + "很长" * 50) for i in range(30)]
    msgs.append(AIMessage("", tool_calls=[{"name": "get_kline", "args": {"code": "600519"}, "id": "1"}]))
    msgs.append(ToolMessage("K" * 2000, tool_call_id="1", name="get_kline"))
    msgs.append(AIMessage("【核心结论】估值合理。"))
    out = c.render_transcript(msgs, max_chars=1200)
    assert out.startswith("（更早的对话已省略）")
    assert "问题29" in out and "问题0" not in out
    assert "助手（调用工具：get_kline）" in out and "K" * 400 not in out
    assert out.rstrip().endswith("助手：【核心结论】估值合理。")


def test_fallback_summary_keeps_questions_and_conclusions():
    msgs = [HumanMessage("茅台估值如何？"), AIMessage("【核心结论】PE 处于中枢。\n其他"), HumanMessage("现金流呢？")]
    s = c.fallback_summary(msgs, previous="前情：关注白酒")
    assert "前情：关注白酒" in s and "茅台估值如何？" in s and "现金流呢？" in s and "PE 处于中枢" in s


def test_seed_messages_budget_and_alternation():
    recent = []
    for i in range(40):
        recent.append({"role": "user", "content": f"第{i}问" + "字" * 300})
        recent.append({"role": "assistant", "content": f"第{i}答" + "字" * 300})
    recent.append({"role": "user", "content": "中断的提问没有回复"})
    seeded = c.seed_messages("摘要内容", recent, budget_tokens=1000)
    assert seeded[0]["content"].startswith(c.SUMMARY_PREFIX) and seeded[1]["role"] == "assistant"
    body = seeded[2:]
    assert body and body[0]["role"] == "user" and body[-1]["role"] == "assistant"
    assert all(a["role"] != b["role"] for a, b in zip(body, body[1:]))
    assert sum(len(m["content"]) for m in body) / 1.5 <= 1000 + 50
    assert "第39答" in body[-1]["content"]  # 最近的一轮一定保留


def test_normalize_turns_merges_same_role():
    out = c.normalize_turns([
        {"role": "assistant", "content": "孤立回复"},
        {"role": "user", "content": "a"}, {"role": "user", "content": "b"},
        {"role": "assistant", "content": "c"}, {"role": "user", "content": "d"},
    ])
    assert out == [{"role": "user", "content": "a\n\nb"}, {"role": "assistant", "content": "c"}]


def test_context_window_inference():
    assert context_window_tokens("deepseek-chat") == 64_000
    assert context_window_tokens("qwen-max") == 128_000
    assert context_window_tokens("claude-sonnet") == 200_000
    assert context_window_tokens("my-relay-model") == 64_000
    assert context_window_tokens("deepseek-chat", override_k=32) == 32_000
