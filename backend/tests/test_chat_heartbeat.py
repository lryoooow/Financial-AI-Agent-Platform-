"""v1.7.0：聊天 SSE 心跳 _pump_with_heartbeat 的行为与取消语义。

重点锁住（对照 v1.6.3 P1 教训）：
- 静默期发心跳且等于最近一次 stage（fallback → 真实 stage 更新）
- 上游异常原样转交；正常结束无多余心跳
- 消费方 break/aclose 后 pump 与上游生成器确定性收尾（无残留 task）
- 外部取消不悬挂

风格说明：本仓 pytest 无异步插件，统一同步用例 + asyncio.run（与 test_routes_b.py 一致）。
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finresearch.api import routes, sse  # noqa: E402


def _parse(line: str) -> dict:
    return json.loads(line[6:])


def _run(coro):
    return asyncio.run(coro)


def test_heartbeat_during_silence_and_stage_update():
    async def body():
        async def source():
            yield sse.ev_chat_stage("tool", "拉取实时行情"), ""
            await asyncio.sleep(0.3)  # 静默 > interval×3
            yield sse.ev_token("答案"), "答案"

        out = []
        async for item in routes._pump_with_heartbeat(source(), interval=0.05):
            out.append(item)
        return out

    got = _run(body())
    stages = [_parse(l) for l, p in got if '"stage"' in l and not p]
    heartbeats = [s for s in stages if s["label"] == "拉取实时行情"]
    assert len(heartbeats) >= 2, "静默期应有 ≥2 条心跳"
    assert all(s["label"] == "拉取实时行情" for s in stages), "心跳跟随最近一次真实 stage"
    assert got[-1] == (sse.ev_token("答案"), "答案"), "真实产出原样透传"


def test_heartbeat_before_first_stage_uses_fallback():
    async def body():
        async def source():
            await asyncio.sleep(0.15)
            yield sse.ev_token("x"), "x"

        out = []
        async for item in routes._pump_with_heartbeat(source(), interval=0.04):
            out.append(item)
        return out

    got = _run(body())
    stages = [_parse(l) for l, p in got if '"stage"' in l and not p]
    assert any(s["label"] == "正在思考" for s in stages), "首个真实 stage 前用心跳 fallback"


def test_source_exception_propagates():
    async def body():
        boom = RuntimeError("boom")

        async def source():
            yield sse.ev_token("部分"), "部分"
            raise boom

        agen = routes._pump_with_heartbeat(source(), interval=0.05)
        async for _ in agen:
            pass
        raise AssertionError("不可达：上游异常必须先转交")

    try:
        _run(body())
        raise AssertionError("上游异常必须转交给消费方")
    except RuntimeError as e:
        assert str(e) == "boom"


def test_no_heartbeat_after_done():
    """弱断言说明：生成器正常耗尽后本就不可能再 yield——本用例真正锁的是
    「_DONE 哨兵先于超时被取走」（若哨兵处理有 bug 导致 continue，静默 0.06s
    会再产出 ≥2 条心跳，len 断言即红），而非字面上的"迟到帧"。"""

    async def body():
        async def source():
            yield sse.ev_token("a"), "a"

        out = []
        async for item in routes._pump_with_heartbeat(source(), interval=0.02):
            out.append(item)
        await asyncio.sleep(0.06)  # 结束后等 3 个 interval
        return out

    got = _run(body())
    assert len(got) == 1, "结束后不得有迟到心跳（_DONE 先 break）"


def test_consumer_break_closes_source_and_pump():
    """合规闸 break / 弃置路径：按生产用法（aclosing 包心跳流）提前退出 →
    上游生成器确定性关闭、pump 无残留。裸 break（不 aclosing）不会关生成器——
    生产侧 routes.gen 已用 contextlib.aclosing 包裹，测试同构。"""

    async def body():
        import contextlib

        closed = {"flag": False}

        async def source():
            try:
                i = 0
                while True:
                    yield sse.ev_token(f"t{i}"), f"t{i}"
                    await asyncio.sleep(0.01)  # 无 await 的 source 会让 pump 同步自旋卡死事件循环
                    i += 1
            finally:
                closed["flag"] = True

        n = 0
        async with contextlib.aclosing(
            routes._pump_with_heartbeat(source(), interval=0.05)
        ) as agen:
            async for _ in agen:
                n += 1
                if n == 3:
                    break
        await asyncio.sleep(0.15)  # 给 pump 收尾时间
        assert closed["flag"] is True, "上游生成器必须被确定性关闭（v1.6.3 P1 承诺）"
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert not leftovers, f"不得残留后台 task（实际 {[t.get_name() for t in leftovers]}）"

    _run(body())


def test_external_cancel_does_not_hang():
    """客户端断开（uvicorn 取消）：消费任务被取消后 pump 一并被收，不悬挂。"""

    async def body():
        import contextlib

        started = asyncio.Event()

        async def source():
            started.set()
            while True:
                yield sse.ev_token("x"), "x"
                await asyncio.sleep(0.01)

        async def consumer():
            # 生产同构：routes.gen 用 aclosing 包心跳流，取消沿栈展开时确定性收 pump
            async with contextlib.aclosing(
                routes._pump_with_heartbeat(source(), interval=0.05)
            ) as agen:
                async for _ in agen:
                    await asyncio.sleep(0.01)

        task = asyncio.create_task(consumer())
        await asyncio.wait_for(started.wait(), 1.0)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await asyncio.wait_for(task, 2.0)
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)  # 给 pump 收尾一个调度拍
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert not leftovers, f"取消后不得残留 task（实际 {[t.get_name() for t in leftovers]}）"

    _run(body())
