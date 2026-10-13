"""研究后台任务不依赖任意一个 SSE 订阅者的生命周期。"""

import asyncio
import datetime as dt
from types import SimpleNamespace

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


def test_legacy_done_status_is_exposed_as_succeeded():
    task = SimpleNamespace(
        id="task-1",
        code="600519.SH",
        template="equity_deep",
        query=None,
        status="done",
        stage="done",
        report_id="report-1",
        error=None,
        created_at=dt.datetime(2026, 1, 1),
        updated_at=dt.datetime(2026, 1, 1),
        progress_events=[],
        timings={},
    )

    payload = routes._research_task_meta(task)

    assert payload["status"] == "succeeded"
    assert payload["stage"] == "succeeded"


def test_industry_source_failure_surfaces_actionable_reason():
    """行业数据源挂了要说清是哪一步、能不能重试。

    实测踩到过：申万三个层级全返回解析错误时，prefetch_industry 只把原因写进工具返回值
    （模型看得到、用户看不到），最终 SSE 只报一句"未生成报告草稿（数据源可能不可用）"——
    用户既不知道是行业估值源挂了，也不知道该重试还是换个行业名。
    现在真实原因经 fatal_error 传到路由层。
    """
    import asyncio

    from finresearch.agents.deep import research_tools as rt

    task_id = "industry-fail-case"
    rt._STORE.pop(task_id, None)

    def _boom(*_a, **_kw):
        raise ValueError("申万一级 未返回数据：'NoneType' object has no attribute 'find_all'")

    tools = rt.build_research_tools(task_id, "industry", None, {})
    prefetch_industry = next(t for t in tools if t.name == "prefetch_industry")

    original = rt._search_industry
    rt._search_industry = _boom
    try:
        message = asyncio.run(prefetch_industry.ainvoke({"industry_name": "光伏"}))
    finally:
        rt._search_industry = original

    stored_reason = rt.get_stored(task_id, "fatal_error", "")
    assert stored_reason, "取数失败必须把原因写进 fatal_error，否则路由层只能报笼统文案"
    assert "行业估值数据源" in stored_reason
    assert "重试" in stored_reason
    # 工具返回给模型的文案也要说清楚，别让它自己编一个原因
    assert "未能取到行业估值数据" in message
    rt._STORE.pop(task_id, None)
