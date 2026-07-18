"""研究后台任务不依赖任意一个 SSE 订阅者的生命周期。"""

import asyncio

from finresearch.api import routes
from finresearch.config import settings


def test_research_runtime_completes_without_subscribers():
    async def scenario():
        async def frames():
            yield "data: one\n\n"
            await asyncio.sleep(0)
            yield "data: done\n\n"

        runtime = routes._start_research_runtime("runtime-no-client", frames)
        await runtime.task
        assert runtime.terminal is True
        assert runtime.history == ["data: one\n\n", "data: done\n\n"]
        routes._RESEARCH_RUNS.pop("runtime-no-client", None)

    asyncio.run(scenario())


def test_research_runtime_fans_out_and_sends_terminal_marker():
    async def scenario():
        gate = asyncio.Event()

        async def frames():
            await gate.wait()
            yield "data: section\n\n"

        runtime = routes._start_research_runtime("runtime-subscriber", frames)
        queue = asyncio.Queue()
        runtime.subscribers.add(queue)
        gate.set()
        assert await queue.get() == "data: section\n\n"
        assert await queue.get() is None
        await runtime.task
        routes._RESEARCH_RUNS.pop("runtime-subscriber", None)

    asyncio.run(scenario())


def test_report_timeout_and_preview_defaults_are_bounded():
    assert settings.report_section_timeout_s <= 30
    assert settings.report_use_llm_summary is False
