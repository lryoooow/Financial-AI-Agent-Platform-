"""deepagents 对话 agent。

与旧对话链路（routes.py:send_chat_message 的裸 llm.astream）的本质区别：
模型自己决定调什么工具。旧链路每轮先跑 detect_ticker 预取一次行情+新闻塞进 prompt，
而 detect_ticker 只返回单个标的——"茅台和五粮液比一下"结构上就答不了。

模型分档：Phase 1 全程强模型（build_llm），轻量档没落地。这是实测后的让步，不是遗漏。
- 主循环必须是强模型——规划环节决定调哪个工具、按什么顺序调，是工具调用可靠性最吃紧的
  地方。把轻量模型放这儿是分档最容易翻车的用法。这条没得商量。
- 对话链路里唯一适合轻量档的窄任务是"压缩被驱逐的历史"（SummarizationMiddleware）。
  但已装的 0.6.12 里 middleware= 是纯追加、没有按名替换（那是未发布代码才有的），
  自己再塞一个摘要中间件会直接撞 "duplicate middleware instances"。
  官方替换路径要在 HarnessProfile 上设 excluded_middleware，而 profile 按 provider key
  匹配——我们所有模型（DeepSeek/Kimi/GLM）都走 ChatOpenAI，全落在 "openai" 这个 key 上，
  等于在导入期改库的全局注册表，还可能盖掉 deepagents 自带的 openai provider profile。
  为一个成本优化背上全局副作用不划算，遂放弃。
- 分档真正该落地的地方是 Phase 2 的研究链路：SubAgent(model=...) 是干净的公开 API，
  窄任务（图表速读/长文压缩）挂 build_chart_llm 即可。

Phase 1 不挂 subagents——同样是有意偏离方案，理由：
本项目默认模型是 DeepSeek（config.py:27），方案已把"模型工具调用能力"列为头号风险。
挂 subagents 会多出一个 task 工具，对偏弱的工具调用模型是额外的误触发面，还要多跑一层
嵌套 agent loop（更慢）。而对话是短程任务，本就不需要 subagent 的上下文隔离。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware.types import AgentMiddleware

from ...llm import build_llm
from ...tools.context import render_profile_block, render_time_anchor
from ..prompts import AGENT_CHAT_SYSTEM_PROMPT
from .tools import CHAT_TOOLS

logger = logging.getLogger(__name__)

# 两个 deepagents 默认塞进来、而本链路不要的工具：
#
# execute —— FilesystemMiddleware 无条件暴露（描述写着 "Executes a shell command in an
#   isolated sandbox environment"）。默认 StateBackend 没实现 execute()，模型调了只会报错，
#   算不出数字；但把这么个工具摆在 DeepSeek 这类模型面前它一定会去试，白白绕圈。
#   而且换成沙箱后端它就静默变成真的了——agent 一旦能跑代码，"数字零幻觉"当场失效。
# task —— 即便 subagents=None，deepagents 仍默认插一个 general-purpose subagent
#   （graph.py:770 inline_subagents.insert(0, general_purpose_spec)）。对话是短程任务，
#   不需要 subagent 的上下文隔离，多一层嵌套 loop 只会更慢、多一个误触发面。
#   Phase 2 研究链路要挂 subagents 时把它从这里去掉即可。
#
# 为什么自己过滤而不用官方开关：两者的官方路径（FilesystemMiddleware(tools=[...]) 白名单、
# GeneralPurposeSubagentProfile(enabled=False)）都要么只存在于未发布代码里——本地 clone 领先
# PyPI 33 个提交，别拿 clone 的源码当已装库的 API 依据——要么挂在 HarnessProfile 上，而
# profile 按 provider key 匹配：我们所有模型（DeepSeek/Kimi/GLM）都走 ChatOpenAI，全落在
# "openai" 这一个 key 上，注册它等于在导入期改库的全局注册表，还可能盖掉 deepagents 自带的
# openai provider profile。自己筛一下 request.tools 就够了，不赌私有 API、不赌未发布版本。
_EXCLUDED_TOOLS = frozenset({"execute", "task"})


def _tool_name(t: Any) -> Optional[str]:
    if isinstance(t, dict):
        name = t.get("name")
        return name if isinstance(name, str) else None
    name = getattr(t, "name", None)
    return name if isinstance(name, str) else None


class ExcludeToolsMiddleware(AgentMiddleware):
    """在模型看到工具列表之前把指定工具摘掉。

    必须排在注入工具的中间件（filesystem/todo）之后，否则过滤时它们还没注入。
    """

    def __init__(self, *, excluded: frozenset[str]) -> None:
        super().__init__()
        self._excluded = excluded

    def _filter(self, request):
        if not self._excluded:
            return request
        kept = [t for t in request.tools if _tool_name(t) not in self._excluded]
        return request.override(tools=kept)

    def wrap_model_call(self, request, handler):
        return handler(self._filter(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._filter(request))


def build_chat_agent(
    *,
    llm_config: Optional[dict] = None,
    profile: Optional[dict] = None,
    checkpointer: Optional[Any] = None,
) -> Optional[Any]:
    """构建对话 agent；LLM 未配置时返回 None，由调用方按旧链路的方式优雅降级。

    Args:
        llm_config: 前端选择的模型 {base_url, api_key, model}；为空回落环境变量。
        profile: 用户画像，注入 system prompt（缺字段由 render_profile_block 回填默认）。
        checkpointer: 复用 graph.py:init_checkpointer() 的全局检查点；thread_id 由调用方按
            conversation_id 传入。
    """
    model = build_llm(streaming=True, override=llm_config)
    if model is None:
        logger.warning("LLM 未配置，chat agent 不可用。")
        return None

    # 段序沿用 prompts.py 的约定：身份/纪律/输出规范 → 画像 → 时间锚点。
    # 稳定前缀在前、易变内容在后，既贴合注意力分布，也让厂商 prompt cache 命中长前缀。
    system_prompt = "\n".join(
        [
            AGENT_CHAT_SYSTEM_PROMPT,
            render_profile_block(profile),
            render_time_anchor(),
        ]
    )

    backend = StateBackend()

    # 传字符串 system_prompt 是前置到 deepagents 内置提示词之前（graph.py:863
    # `system_prompt + "\n\n" + base_prompt`），不是顶掉——内置那段讲 todo/文件工具怎么用，
    # 必须留着。
    return create_deep_agent(
        model=model,
        tools=CHAT_TOOLS,
        system_prompt=system_prompt,
        backend=backend,
        checkpointer=checkpointer,
        # middleware= 是纯追加，插在基础栈之后、tail 之前——正好满足"排在注入工具的
        # 中间件（filesystem/todo）之后"这个前提，过滤时它们已经注入完毕。
        middleware=[ExcludeToolsMiddleware(excluded=_EXCLUDED_TOOLS)],
    )
