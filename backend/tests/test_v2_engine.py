"""Model and tool integration gates for Phase 1."""

import asyncio
import json

import httpx
from langchain_core.messages import HumanMessage
from tenacity import wait_none

from finresearch.agents.workers.report import check_generated
from finresearch.core.guardrails.sanitizer import AnswerStreamSanitizer
from finresearch.engine import model_client
from finresearch.engine.tools_adapter import compile_tool
from finresearch.engine.turn_context import TurnToolState
from finresearch.tools import registry


def test_quota_is_reserved_before_concurrent_execution():
    state = TurnToolState()

    async def invoke():
        allowed, _ = state.try_reserve("web_search")
        await asyncio.sleep(0)
        return allowed

    async def scenario():
        return await asyncio.gather(*(invoke() for _ in range(10)))

    assert asyncio.run(scenario()).count(True) == 4
    assert state.reservations == {"web_search": 4}
    assert state.check_and_mark_fingerprint("test", {"a": 1, "b": 2})[0]
    assert not state.check_and_mark_fingerprint("test", {"b": 2, "a": 1})[0]


def test_sanitizer_guards_split_phrases_and_multiple_think_spans():
    guard = AnswerStreamSanitizer(check_generated)
    pieces = [
        "这是一条足够长的公开数据说明。<th", "ink>私有", "</thi", "nk>数据。",
        "<think>第二段思考</think>强烈建议", "买", "入",
    ]
    output = [guard.feed(piece) for piece in pieces]
    output.append(guard.flush())
    visible = "".join(delta.visible_text for delta in output)
    reasoning = "".join(delta.reasoning_text for delta in output)
    assert "强烈建议" not in visible
    assert "私有第二段思考" == reasoning
    assert any(delta.compliance_triggered for delta in output)


def test_compiled_financial_tool_has_separate_artifact(monkeypatch):
    bars = [{"date": "2026-05-20", "close": 11.3, "high": 12, "low": 10, "volume": 20}] * 120

    async def data(*_args, **_kwargs):
        return {"data": bars, "provenance": {"source": "fixture", "trading_date": "2026-05-20"}}, 0, None

    monkeypatch.setattr(registry, "_timed_call", data)
    tool = compile_tool(registry.get_kline)
    msg = asyncio.run(tool.ainvoke({
        "type": "tool_call", "name": tool.name, "id": "call_k", "args": {"code": "600519.SH"},
    }))
    assert len(msg.content) <= 500 and "2026-05-20" in msg.content
    assert msg.artifact["data"] == bars
    assert tool.args_schema.model_json_schema()["properties"]["n"]["maximum"] == 500


def test_gateway_retries_then_falls_back_and_merges_calls(monkeypatch):
    monkeypatch.setattr(model_client, "wait_exponential_jitter", lambda **_kw: wait_none())
    seen = []

    def tool(index, *, name=None, arguments="", call_id=None):
        return {"index": index, "id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}

    def response(request):
        body = json.loads(request.content)
        seen.append(body["model"])
        if body["model"] == "primary":
            return httpx.Response(429, json={"error": {"message": "fixture quota"}})
        fragments = [
            {"content": "<think>隐藏"},
            {"content": "</think>事实<th", "tool_calls": [tool(1, name="bad", arguments="{", call_id="b")]},
            {"content": "ink>第二段</think>。", "tool_calls": [tool(0, name="quote", arguments='{"code":', call_id="a")]},
            {"tool_calls": [tool(0, arguments='"600519.SH"}')]},
        ]
        frames = [
            "data: " + json.dumps({
                "id": "c", "created": 1, "model": "fallback", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }, ensure_ascii=False) + "\n\n"
            for delta in fragments
        ]
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content="".join(frames) + "data: [DONE]\n\n")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            model = model_client.ReasoningChatOpenAI(
                model="primary", fallback_models=("fallback",), max_retries=1, streaming=True,
                api_key="sk-unit-example", base_url="https://example.invalid/v1", http_async_client=client,
            )
            return await model.ainvoke([HumanMessage(content="quote")])

    message = asyncio.run(run())
    assert seen == ["primary", "primary", "fallback"]
    assert message.content == "事实。"
    assert message.tool_calls[0]["args"] == {"code": "600519.SH"}
    assert [call["id"] for call in message.invalid_tool_calls] == ["b"]
