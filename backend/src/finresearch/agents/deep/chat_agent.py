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
import re
from typing import Any, Optional

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware.model_call_limit import ModelCallLimitMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.exceptions import ContextOverflowError
from langchain_core.tools import BaseTool

from ...llm import build_llm
from ...tools.context import render_profile_block, render_time_anchor
from ..prompts import AGENT_CHAT_SYSTEM_PROMPT
from .tools import (
    CHAT_TOOLS,
    get_company_profile,
    get_financial_indicators,
    get_intraday,
    get_kline,
    get_realtime_quote,
    get_technical_indicators,
    read_memory,
    resolve_entity,
    search_industry,
    search_memory,
    search_news,
    web_search,
)

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
# 父 agent（编排层）排除集：
# - execute：能跑代码会破坏数字零幻觉（0.6.12 的 FilesystemMiddleware 对非沙箱
#   backend 已自滤，此处是防后端换成沙箱的纵深防御）
# - write_file/edit_file/ls/glob/grep：文件管理死重——chat 场景没有 agent 自管文件
#   的需求，留着只占 schema（~4K 字符）并分散弱模型注意力。
#   read_file 保留：SummarizationMiddleware 的历史卸载取回依赖它。
#   write_todos 保留：前端待办 UI 的事件源。
_EXCLUDED_TOOLS = frozenset({"execute", "write_file", "edit_file", "ls", "glob", "grep"})
# research_agent 仍摘除 task（定向流程不需要委派）+ 文件死重
RESEARCH_EXCLUDED_TOOLS = frozenset({"execute", "task", "write_file", "edit_file", "ls", "glob", "grep"})


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


# 厂商超窗报错 → ContextOverflowError 的适配层。
#
# langchain-openai 只认 OpenAI 官方报错文本（context_length_exceeded 等三个 pattern），
# DeepSeek 实际返回 "This model's maximum context length is 65536 tokens. However,
# you requested ..."——不匹配 → 普通 BadRequestError 冒泡 → SummarizationMiddleware
# 的"压缩后重试"路径（except ContextOverflowError）永远捕不到 → 该轮 400、
# HumanMessage 已进检查点、线程永久坏死。这层把语义对齐：命中超窗关键词就转成
# ContextOverflowError，交给摘要中间件压缩重试。
#
# 误匹配代价可控：多跑一次摘要尝试，重试仍 400 则如实上抛。
# 注意避开 "too large"（那是图片体积错误，routes._is_vision_error 另有语义）。
_OVERFLOW_HINTS = re.compile(
    r"maximum context length|context_length_exceeded|context window"
    r"|exceed\w*\s*(?:the\s*)?(?:maximum|context|token|limit)"
    r"|输入.*(?:超|长)|上下文.*(?:超|长)",
    re.IGNORECASE,
)


class ContextOverflowAdapterMiddleware(AgentMiddleware):
    """厂商超窗报错文本 → ContextOverflowError（激活摘要中间件的压缩重试）。"""

    async def awrap_model_call(self, request, handler):
        try:
            return await handler(request)
        except ContextOverflowError:
            raise
        except Exception as e:  # noqa: BLE001
            if _OVERFLOW_HINTS.search(str(e)):
                raise ContextOverflowError(str(e)) from e
            raise


# task 工具的默认描述约 6.5-6.9K 字符（≈2200 tok，含英文长示例）——每轮都在
# 请求体里。换成中文短描述 + 场景清单，单这一项每轮省约 2000 tok。
_TASK_TOOL_DESCRIPTION = (
    "把一个需要多工具组合的分析任务委派给场景专家代理并取回结论。\n"
    "可用场景：market-analysis（行情诊断/多标的对比）、fundamentals-news（基本面+资讯综合）、"
    "industry-valuation（行业估值）、memory（历史对话/报告/知识库检索）、"
    "general-purpose（仅当其他场景都不匹配）。\n"
    "description 必须自包含：写明标的代码（先用 resolve_entity 确认）、要取的字段、"
    "期望的输出——场景代理看不到本对话历史。结果不会展示给用户，拿到后由你组织作答。"
)


class RewriteToolDescriptionMiddleware(AgentMiddleware):
    """把指定工具的描述替换为精简版（对模型可见的 schema 瘦身）。"""

    def __init__(self, *, rewrites: dict) -> None:
        super().__init__()
        self._rewrites = rewrites

    def _filter(self, request):
        kept = []
        for t in request.tools:
            name = _tool_name(t)
            if name in self._rewrites:
                if isinstance(t, dict):
                    t = {**t, "description": self._rewrites[name]}
                else:
                    try:
                        t = t.model_copy(update={"description": self._rewrites[name]})
                    except Exception:  # noqa: BLE001  # 非 pydantic 对象：跳过改写
                        pass
            kept.append(t)
        return request.override(tools=kept)

    def wrap_model_call(self, request, handler):
        return handler(self._filter(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._filter(request))


def _scenario_subagents() -> list:
    """场景子代理：只保留真正需要多步组合分析的场景。

    速度教训（2.4.0 实测）：委派一次 = 父决策 + 子 N 步 + 父综合 ≈ N+2 次 LLM 调用，
    且子代理每轮带 ~1600 tok 死重（BASE_AGENT_PROMPT + Filesystem/TodoList 工具）。
    单次调用工具（quote/kline/memory/industry）内联直调只要 2 次调用——差一倍以上。
    只有"多源取数+交叉综合"才值得付委派税。
    """
    # 子代理内部也摘除文件死重（Filesystem/TodoList 中间件无条件注入，但可以过滤）
    subagent_excluded = frozenset({
        "execute", "write_file", "edit_file", "ls", "glob", "grep", "write_todos",
    })
    cap_and_trim = [
        ExcludeToolsMiddleware(excluded=subagent_excluded),
        ModelCallLimitMiddleware(run_limit=6),
        ContextOverflowAdapterMiddleware(),
    ]

    def spec(name, description, system_prompt, tools):
        return {
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "tools": tools,
            "middleware": cap_and_trim,
        }

    return [
        spec(
            "market-analysis",
            "行情深度诊断：多标的行情对比、技术面+分时+K线综合研判。需要组合 3 个以上行情维度时用。",
            "你是 A 股行情分析专家。调用工具取真实数据后作答：数字必须一字不差引用工具返回值并注明数据时点；"
            "取不到就如实说明。不得使用买卖指令性动词（买入/卖出/加仓/减仓/止损/清仓/抄底/建仓/满仓）。",
            [resolve_entity, get_realtime_quote, get_technical_indicators, get_kline, get_intraday],
        ),
        spec(
            "fundamentals-synthesis",
            "基本面综合：公司概况+多年财务+新闻+公开信息交叉分析，产出完整的基本面研判。",
            "你是基本面研究专家。所有数字必须来自本次工具返回值并注明数据时点与来源，"
            "一字不差，不得用记忆补位；工具返回 error 字段表示该项取不到，如实说明并跳过；"
            "区分事实与推断；不编造任何数据。"
            "不得使用买入/卖出/加仓/减仓/止损/清仓/抄底/建仓/满仓等指令性动词。",
            [get_company_profile, get_financial_indicators, search_news, web_search],
        ),
        # 命名 general-purpose 压制库默认注入（实测有效）：兜底场景
        {
            "name": "general-purpose",
            "description": "通用兜底：仅当上述场景都不匹配的复杂任务才用。",
            "system_prompt": "完成委派任务：数字必须来自工具返回，缺失就如实说明。",
            "tools": CHAT_TOOLS,
            "middleware": cap_and_trim,
        },
    ]


def build_chat_agent(
    *,
    llm_config: Optional[dict] = None,
    profile: Optional[dict] = None,
    context_note: str = "",
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
            context_note.strip(),
        ]
    )

    backend = StateBackend()
    # 父级内联 = 全部单次调用工具（一次 tool call 即可回答，不需要多步组合）。
    # 速度关键：内联 = 2 次 LLM 调用（决策+作答）；委派 = 4+ 次。
    # 只有"多源取数+交叉综合"才值得付委派税（→ market-analysis / fundamentals-synthesis）。
    parent_tools = [
        resolve_entity,           # 确认代码
        get_realtime_quote,       # 单点价格/涨跌
        get_kline,                # K 线序列
        get_intraday,             # 分时摘要
        get_technical_indicators, # 技术指标
        search_industry,          # 行业估值
        search_memory,            # 记忆检索
        read_memory,              # 记忆深读
    ]

    # 传字符串 system_prompt 是前置到 deepagents 内置提示词之前（graph.py:863
    # `system_prompt + "\n\n" + base_prompt`），不是顶掉——内置那段讲 todo/文件工具怎么用，
    # 必须留着。
    return create_deep_agent(
        model=model,
        system_prompt=system_prompt,
        backend=backend,
        checkpointer=checkpointer,
        # middleware= 是纯追加，插在基础栈之后、tail 之前——正好满足"排在注入工具的
        # 中间件（filesystem/todo）之后"这个前提，过滤时它们已经注入完毕。
        # 用户 middleware 位于默认 SummarizationMiddleware 的内侧（更贴近模型），
        # 恰是超窗报错转换必须发生的位置（转换后外层摘要中间件才能捕到并压缩重试）。
        middleware=[
            ExcludeToolsMiddleware(excluded=_EXCLUDED_TOOLS),
            RewriteToolDescriptionMiddleware(rewrites={"task": _TASK_TOOL_DESCRIPTION}),
            ContextOverflowAdapterMiddleware(),
        ],
        subagents=_scenario_subagents(),
        tools=parent_tools,
    )
