"""Exercise ephemeral prompts, real checkpoints, and the retrieval boundary."""

import asyncio
import io
import json
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from finresearch.agents.deep.stream import stream_agent_sse
from finresearch.core.guardrails.isolated_worker import parse_document
from finresearch.engine import chat_agent, model_client
from finresearch.engine.context_middleware import read_thread_messages, repair_thread
from finresearch.engine.event_bridge import CancellationToken, watch_disconnect
from finresearch.engine.turn_context import TurnToolState, bind_turn_state
from finresearch.knowledge import store
from finresearch.tools import registry


def test_ephemeral_context_tool_pairing_and_checkpoint_repair(monkeypatch):
    requests = []
    quote = {"code": "600519.SH", "name": "测试标的", "price": 19.0, "provenance": {"source": "fixture"}}
    monkeypatch.setattr(registry, "_get_realtime_quote", lambda _code: quote)

    def endpoint(request):
        data = json.loads(request.content)
        requests.append(data)
        last = data["messages"][-1]
        if last.get("content") == "需要行情":
            payload = {"tool_calls": [{
                "index": 0, "id": "q1", "type": "function",
                "function": {"name": "get_realtime_quote", "arguments": '{"code":"600519.SH"}'},
            }]}
        else:
            payload = {"content": "<think>私有片段</think>查询完成。"}
        packet = {"id": "fixture", "created": 1, "model": "fixture", "object": "chat.completion.chunk",
                  "choices": [{"index": 0, "delta": payload, "finish_reason": "stop"}]}
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"},
            content=f"data: {json.dumps(packet, ensure_ascii=False)}\n\ndata: [DONE]\n\n",
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            model = model_client.ReasoningChatOpenAI(
                model="fixture", streaming=True, api_key="sk-fixture-only",
                base_url="https://example.invalid/v1", http_async_client=client,
            )
            monkeypatch.setattr(chat_agent, "build_llm", lambda **_kw: model)
            agent = chat_agent.build_chat_agent(checkpointer=MemorySaver())
            config = {"configurable": {"thread_id": "same-thread"}, "recursion_limit": 30}
            events = []
            for i in range(10):
                state = TurnToolState(context_budget=16000)
                state.context.upsert_block("当前时间", f"transient-marker-{i}", priority=100)
                text = "需要行情" if i == 0 else "继续讨论"
                with bind_turn_state(state):
                    async for line, _ in stream_agent_sse(agent, {"messages": [("user", text)]}, config):
                        events.append(json.loads(line[6:]))
            history = await read_thread_messages(agent, config)
            assert "transient-marker-" not in repr(history) and "私有片段" not in repr(history)
            assert requests[0]["messages"][0] == requests[-1]["messages"][0]
            assert "transient-marker-9" in str(requests[-1]["messages"])
            assert "transient-marker-8" not in str(requests[-1]["messages"])
            tool_end = next(e for e in events if e.get("type") == "tool_call" and e.get("status") == "ok")
            tool_artifact = next(e for e in events if e.get("type") == "tool_artifact")
            assert tool_end["child_run_id"] == tool_artifact["child_run_id"]
            assert tool_artifact["artifact"]["price"] == 19.0
            original = next(msg.content for msg in history if isinstance(msg, ToolMessage))

            await agent.aupdate_state(config, {"messages": [
                HumanMessage(content="未完成提问"),
                AIMessage(content="", tool_calls=[{"id": "pending", "name": "get_realtime_quote", "args": {}}]),
            ]}, as_node="model")
            await repair_thread(agent, config, await read_thread_messages(agent, config))
            repaired = await read_thread_messages(agent, config)
            tools = {msg.tool_call_id: msg.content for msg in repaired if isinstance(msg, ToolMessage)}
            assert tools["q1"] == original
            assert "中止" in tools["pending"]

    asyncio.run(run())


def test_late_indexing_cannot_resurrect_deleted_report(monkeypatch, tmp_path):
    monkeypatch.setenv("FINRESEARCH_KNOWLEDGE_DB", str(tmp_path / "knowledge.db"))
    doc = {"ticker": "300750.SZ", "name": "宁德时代", "summary": "电池储能与现金流。"}
    assert store.index_report("old-report", doc)
    assert store.search("宁德时代 电池")
    assert not store.search("水利工程 人口统计")
    store.delete_by_ref("report", "old-report")
    assert store.index_report("old-report", doc) is None
    assert not store.search("宁德时代 电池")


def test_worker_rejects_zip_bomb_and_bad_pdf():
    archive = io.BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as doc:
        doc.writestr("word/document.xml", b"a" * 100_000)
    with pytest.raises(ValueError, match="压缩比例"):
        asyncio.run(parse_document(".docx", archive.getvalue()))
    with pytest.raises(ValueError, match="PDF 文件头"):
        asyncio.run(parse_document(".pdf", b"invalid file"))


def test_disconnect_cancels_a_silent_producer():
    class Request:
        async def is_disconnected(self):
            return True

    async def run():
        token = CancellationToken()
        task = asyncio.create_task(asyncio.sleep(20))
        await asyncio.wait_for(watch_disconnect(Request(), token, task), 0.3)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert token.is_cancelled

    asyncio.run(run())
