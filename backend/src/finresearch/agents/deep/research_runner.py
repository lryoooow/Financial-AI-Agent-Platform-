"""研究任务的确定性编排（v1.7.1，默认路径）。

为什么不再让 agent 主循环来编排：模板化研究的流程是固定的 ——
个股：prefetch_basics → compose_report；行业：prefetch_industry → compose_report。
agent 主循环在这里没有任何需要"判断"的地方，却要多跑 3 次模型往返
（决定取数 → 决定撰写 → 写一句收尾话术），每次都是**非流式**、前端零事件的静默等待：

- 开头：等模型决定调 prefetch_basics；
- 中间：数据取回后，等模型决定调 compose_report；
- 结尾：四段早已写完，done 还要再等一轮收尾话术才发出。

接中转站思考模型时每轮 30–120s，用户看到的就是"开头卡、中间卡、写完了还卡"——
研究任务"分段等待/最后一次性跳出来"的主因（e2e 探针实测：THINK_S=5 时 15s 的总
静默全部来自这三轮）。工具层与报告层原样复用（同一套业务服务，零复制），只是把
"谁来按顺序调用它们"从模型换成代码（CLAUDE.md：确定性流程不包装成不必要的 Agent）。

agent 编排仍保留：标的不是规范代码（需要 resolve_entity 理解意图）或显式打开
settings.research_agent_loop 时由 routes 走 agent 路径。

事件形状与 agent 路径逐字段一致（tool_call start/ok/error、stage、custom→SSE），
前端无需区分两条编排。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any, AsyncIterator, Optional

from ...api import sse
from ..workers.report import PUSH_SINK
from .research_tools import _put, build_research_tools
from .stream import _tool_failed, custom_event_to_sse, tool_label

logger = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"^\d{6}(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)


def is_direct_target(code: str, template: str) -> bool:
    """能否走确定性编排：行业模板（输入即行业名）或规范 A 股代码。"""
    if (template or "").strip() == "industry":
        return bool((code or "").strip())
    return bool(_TICKER_RE.match((code or "").strip()))


def _error_text(result: Any) -> str:
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (ValueError, TypeError):
        return str(result or "")
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])
    return str(result or "")


async def _run_tool(tool: Any, args: dict) -> AsyncIterator[tuple[str, Any]]:
    """运行一个工具，边跑边把它推送的 custom 事件转成 SSE 行。

    产出 ("line", sse_line) 若干次，最后产出一次 ("result", 工具返回值)。
    事件槽经 contextvar 挂在工具任务上（任务创建时复制 context，report_node 里
    gather 出的并行分段同样继承），推送与消费在同一事件循环、无锁。
    """
    queue: asyncio.Queue = asyncio.Queue()
    getter: Optional[asyncio.Future] = None
    token = PUSH_SINK.set(queue.put_nowait)
    try:
        work = asyncio.ensure_future(tool.ainvoke(args))
    finally:
        PUSH_SINK.reset(token)
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({work, getter}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                line = custom_event_to_sse(getter.result())
                if line:
                    yield "line", line
                continue
            getter.cancel()
            break
        while not queue.empty():
            line = custom_event_to_sse(queue.get_nowait())
            if line:
                yield "line", line
        yield "result", work.result()
    finally:
        # 生成器被提前关闭（客户端合规闸 break / 任务取消 / 删除任务）时确定性收尾，
        # 不让撰写任务在后台继续烧 token；挂起中的 queue.get 一并取消（不留悬挂任务）。
        if getter is not None and not getter.done():
            getter.cancel()
        if not work.done():
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await work


async def run_research_direct(
    *,
    task_id: str,
    code: str,
    template: str,
    llm_config: Optional[dict],
    profile: Optional[dict],
) -> AsyncIterator[str]:
    """按固定流程执行研究，产出 SSE 行。失败原因写入工具层存储的 "failure" 键，
    供 routes 落库（报告草稿同样从存储取回，与 agent 路径一致）。"""
    tpl = (template or "equity_deep").strip() or "equity_deep"
    tools = {t.name: t for t in build_research_tools(task_id, tpl, llm_config, profile or {})}
    if tpl == "industry":
        steps = [("prefetch_industry", {"industry_name": code.strip()})]
    else:
        steps = [("prefetch_basics", {"code": code.strip().upper()})]
    steps.append(("compose_report", {}))

    for name, args in steps:
        label = tool_label(name)
        yield sse.ev_tool_call(name, label, "start")
        yield sse.ev_chat_stage("tool", label)
        result: Any = None
        try:
            async for kind, value in _run_tool(tools[name], args):
                if kind == "line":
                    yield value
                else:
                    result = value
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001  工具异常 = 该步失败，如实上报
            logger.exception("研究工具 %s 失败：%s", name, exc)
            _put(task_id, "failure", f"{label}失败：{exc}")
            yield sse.ev_tool_call(name, label, "error")
            return
        failed = _tool_failed(result)
        yield sse.ev_tool_call(name, label, "error" if failed else "ok")
        if failed:
            _put(task_id, "failure", _error_text(result))
            return
