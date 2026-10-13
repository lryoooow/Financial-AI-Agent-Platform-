"""deepagents 研究 agent。

与旧固定管线（agents/graph.py 的 entity→research→market→summary→report）的差别：
旧图靠 add_edge 写死顺序，一步都不能变；这里 agent 自己决定做什么，因而能处理
"代码不确定先查一下"、"某源取数失败要不要重试"这类旧图无法表达的情况，
也为后续的开放式研究（"看看光伏行业"）留了口子。

报告质量的骨架原样保留，一行没改：数据闸门（数据不足就如实说不展开）、分段事实块、
单段独立降级、禁用词重写、证据分层如实上报——全都还在 report.py 里，
由 compose_report 工具调用。agent 换掉的是"谁来决定取什么数"，不是"报告怎么写"。

── 模型分档：这里的答案和对话链路是反的，理由要说清楚 ──
对话链路我把强模型压在主循环上，因为那里的规划是开放的——问题千奇百怪，决定调哪个工具、
调几次、要不要追问，全靠模型判断，弱模型一崩整条链就废。
研究链路正相反：主循环的活是窄的、几乎是固定的三步（确认标的 → 取数 → 生成报告），
工具只有三个，顺序几乎没有歧义。这种活轻量档完全够用，还更快更省。
而真正吃模型能力的是各段正文撰写——那是产品的核心价值，仍然用强模型（report.py 里的
build_llm，未改动）。
所以：主循环 = 轻量档（chart_analyze_model），分段撰写 = 强档。
轻量档未配置时回落强模型，不会因为没配就跑不起来。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from deepagents import create_deep_agent
from deepagents.backends import StateBackend

from ...llm import build_chart_llm, build_llm
from ...tools.context import render_time_anchor
from ..prompts import TEMPLATE_FOCUS
from .chat_agent import (
    RESEARCH_EXCLUDED_TOOLS,
    ContextOverflowAdapterMiddleware,
    ExcludeToolsMiddleware,
)
from .research_tools import build_research_tools
from .tools import resolve_entity

logger = logging.getLogger(__name__)

RESEARCH_SYSTEM_PROMPT = """你是金融研究任务的执行者。你的职责是把一次研究任务跑完，而不是自己写分析。

【工作流程】
先判断这是个股研究还是行业研究：
- 个股研究（给定股票代码如 600519.SH，或明确的公司名）：
  1. 代码不确定时先用 resolve_entity 确认。
  2. 调用 prefetch_basics 取齐基础数据。
  3. 调用 compose_report 生成报告。
- 行业研究（任务是研究某个行业，如"光伏行业"、"半导体"）：
  1. 调用 prefetch_industry，传行业名称（如"光伏"）。
  2. 调用 compose_report 生成报告。
compose_report 返回后任务即告完成：不要生成总结性文字、不要复述报告内容，
只回复"已完成"三个字立即结束——报告已通过事件流直接送达用户，
多余的收尾话术只会拖慢出报告的时间。

【纪律】
- 你没有任何计算能力，也不掌握任何实时数据。不要在回复里给出任何具体数值——
  数值全部由工具取得、由报告各段按事实块精确引用，与你无关。
- prefetch_basics 的返回里某项标 ✗ 表示该数据源取数失败。这是正常的，
  报告对应部分会如实标注"数据不足"。不要因此重复调用取数工具超过一次，
  也绝不要用记忆去补。
- 不要自己撰写报告正文。compose_report 会按模板逐段撰写。
"""


def build_research_agent(
    *,
    task_id: str,
    template: str = "equity_deep",
    llm_config: Optional[dict] = None,
    profile: Optional[dict] = None,
    checkpointer: Optional[Any] = None,
) -> Optional[Any]:
    """构建研究 agent；LLM 未配置时返回 None，由调用方降级。

    Args:
        task_id: 研究任务 id，同时用作 thread_id 与工具层存储的键。
        template: equity_deep / industry / earnings / risk，决定报告栏目。
        llm_config: 前端所选模型；为空回落环境变量。
        profile: 用户画像，透传给报告各段撰写。
        checkpointer: 复用 graph.py:init_checkpointer() 的全局检查点。
    """
    strong = build_llm(streaming=True, override=llm_config)
    if strong is None:
        logger.warning("LLM 未配置，research agent 不可用。")
        return None

    # 主循环用轻量档（见模块 docstring 的分档理由）；未配置轻量档时回落强模型。
    loop_model = build_chart_llm(llm_config) or strong

    focus = TEMPLATE_FOCUS.get(template, "")
    system_prompt = "\n".join(
        [
            RESEARCH_SYSTEM_PROMPT,
            f"【本次任务模板】{template}。{focus}" if focus else f"【本次任务模板】{template}。",
            render_time_anchor(),
        ]
    )

    tools = [resolve_entity, *build_research_tools(task_id, template, llm_config, profile or {})]

    return create_deep_agent(
        model=loop_model,
        tools=tools,
        system_prompt=system_prompt,
        backend=StateBackend(),
        checkpointer=checkpointer,
        # 与对话链路同样摘掉 execute（"能跑 shell"，会破坏数字零幻觉）与 task
        # （研究是定向任务，不需要 general-purpose subagent 再套一层循环）。
        middleware=[
            ExcludeToolsMiddleware(excluded=RESEARCH_EXCLUDED_TOOLS),
            ContextOverflowAdapterMiddleware(),
        ],
    )
