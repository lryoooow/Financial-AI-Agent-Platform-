"""v1.7.1：正文内联 <think>…</think>（部分中转站/自部署推理端的思维链形态）拆分。

不拆的后果：研究分段 420 字上限被思维链吃满、对话把思考过程当答案流给用户并落库。
"""
import asyncio

from langchain_core.messages import AIMessageChunk

from finresearch.agents.deep import stream as stream_mod
from finresearch.llm import ThinkTagSplitter, strip_think


def _run(chunks):
    sp = ThinkTagSplitter()
    content = reasoning = ""
    for c in chunks:
        a, b = sp.feed(c)
        content += a
        reasoning += b
    a, b = sp.flush()
    return content + a, reasoning + b


def test_split_across_chunk_boundaries():
    assert _run(["<thi", "nk>先看", "数据</th", "ink>\n\n结论", "。"]) == ("结论。", "先看数据")


def test_plain_text_untouched_and_not_delayed():
    sp = ThinkTagSplitter()
    assert sp.feed("营收增长") == ("营收增长", "")  # 非 <think> 开头：立即放行，不缓冲
    assert sp.feed("<think>不是开头") == ("<think>不是开头", "")  # 正文中途出现按原文处理


def test_html_like_prefix_is_released():
    assert _run(["<", "b>粗体"]) == ("<b>粗体", "")


def test_unclosed_think_counts_as_reasoning():
    assert _run(["<think>想到一半"]) == ("", "想到一半")


def test_strip_think_non_stream():
    assert strip_think("<think>x</think>答案") == "答案"
    assert strip_think("普通回答") == "普通回答"


def test_chat_stream_routes_inline_think_to_reasoning():
    """对话链路：内联思维链走 reasoning 事件（不落库），正文 piece 不含思考过程。"""

    class FakeAgent:
        async def astream(self, inputs, config=None, stream_mode=None):
            meta = {"langgraph_node": "model"}
            for part in ("<think>推理", "过程</think>", "最终", "回答"):
                yield "messages", (AIMessageChunk(content=part, id="run-1"), meta)

    async def go():
        lines, pieces = [], []
        async for line, piece in stream_mod.stream_agent_sse(FakeAgent(), {}, {}):
            lines.append(line)
            pieces.append(piece)
        return lines, "".join(pieces)

    lines, reply = asyncio.run(go())
    assert reply == "最终回答"
    assert any('"reasoning"' in ln and "推理" in ln for ln in lines)
    assert not any('"token"' in ln and "推理" in ln for ln in lines)


def test_tool_node_llm_chunks_are_not_tool_results():
    """研究工具内部的撰写 LLM 也会以 node=tools 的 AIMessageChunk 出现在 messages 流，
    不得被当成工具结果（tool_call ok）误报。"""

    class FakeAgent:
        async def astream(self, inputs, config=None, stream_mode=None):
            yield "messages", (AIMessageChunk(content="段落正文", name="compose_report"), {"langgraph_node": "tools"})

    async def go():
        return [line async for line, _ in stream_mod.stream_agent_sse(FakeAgent(), {}, {})]

    assert asyncio.run(go()) == []
