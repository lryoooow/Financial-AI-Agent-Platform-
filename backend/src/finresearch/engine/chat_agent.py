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

import asyncio
import json
import logging
from typing import Any, Optional

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from ..agents._utils import sanitize_err_text
from .model_client import build_llm, context_window_tokens
from ..agents.prompts import AGENT_CHAT_SYSTEM_PROMPT, AGENT_KNOWLEDGE_GUIDE
from ..tools.registry import CHAT_TOOLS, KNOWLEDGE_TOOLS, PROFILE_TOOLS, mcp_call
from .tools_adapter import compile_tools
from .turn_context import current_turn_state
from .context_middleware import EphemeralContextMiddleware

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
_EXCLUDED_TOOLS = frozenset({
    "execute", "task", "ls", "read_file", "write_file", "edit_file", "glob", "grep",
})


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
        state = current_turn_state()
        blocked = state.precondition_blocks if state else {}
        kept = sorted(
            (t for t in request.tools if _tool_name(t) not in self._excluded and _tool_name(t) not in blocked),
            key=lambda tool: _tool_name(tool) or "",
        )
        return request.override(tools=kept)

    def wrap_model_call(self, request, handler):
        return self._clean_response(handler(self._filter(request)))

    async def awrap_model_call(self, request, handler):
        return self._clean_response(await handler(self._filter(request)))

    def _clean_response(self, response):
        state = current_turn_state()
        for msg in getattr(response, "result", ()):
            if getattr(msg, "type", None) != "ai":
                continue
            has_tool_calls = bool(getattr(msg, "tool_calls", None) or getattr(msg, "invalid_tool_calls", None))
            if not has_tool_calls:
                for key in ("reasoning", "reasoning_content", "thinking"):
                    msg.additional_kwargs.pop(key, None)
                    msg.response_metadata.pop(key, None)
                if isinstance(msg.content, list):
                    msg.content = [
                        block for block in msg.content
                        if not isinstance(block, dict) or block.get("type") not in {"reasoning", "thinking"}
                    ]
            else:
                for key in ("reasoning", "reasoning_content", "thinking"):
                    msg.response_metadata.pop(key, None)
            if msg.invalid_tool_calls and state is not None:
                for call in msg.invalid_tool_calls:
                    call_id = str(call.get("id") or "")
                    if call_id:
                        state.invalid_call_ids.add(call_id)
                        msg.tool_calls.append({
                            "id": call_id, "name": call.get("name") or "__invalid_tool_call__",
                            "args": {}, "type": "tool_call",
                        })
                msg.invalid_tool_calls = []
        return response

    def _error_tool_msg(self, request, exc: Exception) -> ToolMessage:
        call = getattr(request, "tool_call", None) or {}
        name = (call.get("name") if isinstance(call, dict) else getattr(call, "name", None)) or "tool"
        call_id = (call.get("id") if isinstance(call, dict) else getattr(call, "id", None)) or ""
        safe = sanitize_err_text(exc)
        logger.warning("工具调用未捕获异常 name=%s：%s", name, safe)
        return ToolMessage(
            content=json.dumps({"error": f"工具执行异常：{safe}"}, ensure_ascii=False),
            name=str(name),
            tool_call_id=str(call_id),
            status="error",
        )

    def wrap_tool_call(self, request, handler):
        call = getattr(request, "tool_call", None) or {}
        name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
        call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", "")
        if name in self._excluded:
            return ToolMessage("本工具不可用。", name=str(name), tool_call_id=str(call_id), status="error")
        try:
            return handler(request)
        except Exception as exc:  # noqa: BLE001
            return self._error_tool_msg(request, exc)

    async def awrap_tool_call(self, request, handler):
        from pydantic import ValidationError

        state = current_turn_state()
        call = request.tool_call
        name = call["name"]
        if name in self._excluded:
            return ToolMessage("本工具不可用。", name=name, tool_call_id=call["id"], status="error")
        reserved = False
        validated_args: dict[str, Any] = {}
        try:
            if state is not None:
                if call["id"] in state.invalid_call_ids:
                    return ToolMessage(
                        "参数 JSON 未完整，请重新生成一个合法的参数对象。",
                        name=name, tool_call_id=call["id"], status="error",
                    )
                if name in state.precondition_blocks:
                    return ToolMessage(
                        state.precondition_blocks[name], name=name, tool_call_id=call["id"], status="error",
                    )
                schema = getattr(request.tool, "args_schema", None)
                allowed_fields = set(schema.model_fields.keys()) if hasattr(schema, "model_fields") else None
                arguments = state.arguments_for(name, call.get("args") or {}, allowed_fields=allowed_fields)
                if hasattr(schema, "model_validate"):
                    try:
                        arguments = schema.model_validate(arguments).model_dump()
                    except ValidationError as val_exc:
                        detail = "; ".join(
                            f"{'.'.join(str(x) for x in err.get('loc', ()))}: {err.get('msg', '')}"
                            for err in val_exc.errors()[:3]
                        )
                        return ToolMessage(
                            f"[INVALID_ARGUMENTS] 参数不合法（{detail}），请按工具定义传参。",
                            name=name, tool_call_id=call["id"], status="error",
                        )
                validated_args = arguments
                fp = state.fingerprint(name, arguments)
                allowed, message = state.check_and_mark_fingerprint(name, arguments)
                if not allowed:
                    cached = state.cached_results.get(fp)
                    if cached:
                        return ToolMessage(
                            f"[复用本轮已获取结果] {cached}",
                            name=name, tool_call_id=call["id"],
                        )
                    return ToolMessage(message or "工具受限", name=name, tool_call_id=call["id"], status="error")
                allowed, message = state.try_reserve(name)
                if not allowed:
                    state.seen_call_fingerprints.discard(fp)
                    return ToolMessage(message or "工具受限", name=name, tool_call_id=call["id"], status="error")
                reserved = True
                request = request.override(tool_call={**call, "args": arguments})
            result = await handler(request)
            if isinstance(result, ToolMessage):
                failed = result.status == "error"
                if isinstance(result.artifact, dict):
                    if result.artifact.get("error_code") or result.artifact.get("error"):
                        result.status = "error"
                        failed = True
                    prov = result.artifact.get("provenance")
                    if isinstance(prov, dict) and prov.get("error") and not result.artifact.get("data") and not result.artifact.get("financials"):
                        failed = True
                    if state is not None and name == "resolve_entity":
                        candidates = result.artifact.get("results") or []
                        if len(candidates) == 1 and candidates[0].get("code"):
                            state.accept_resolved_symbol(str(candidates[0]["code"]))
                if state is not None and reserved:
                    if failed:
                        state.release_failed_call(name, validated_args)
                    elif isinstance(result.content, str) and result.content:
                        state.remember_result(name, validated_args, result.content)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if state is not None and reserved:
                state.release_failed_call(name, validated_args)
            return self._error_tool_msg(request, exc)


def build_chat_agent(
    *,
    llm_config: Optional[dict] = None,
    profile: Optional[dict] = None,
    checkpointer: Optional[Any] = None,
    learned_block: str = "",
    knowledge_enabled: bool = True,
    learn_enabled: bool = True,
    context_window: Optional[int] = None,
    mcp_enabled: bool = False,
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

    # v1.8.0：把上下文窗口写进 model.profile —— 中转/国产模型名没有 profile 时，deepagents
    # 内置摘要回落到固定 170k 才触发（远超 64k/128k 窗口，长对话先撞窗口报错）。写入后它按
    # 窗口 85% 触发，作为本项目 Codex 式压缩（compaction.py，默认 70%）之后的第二道保险。
    window = int(context_window or context_window_tokens(getattr(model, "model_name", "") or ""))
    try:
        prof = dict(getattr(model, "profile", None) or {})
        if context_window or not isinstance(prof.get("max_input_tokens"), int):
            prof["max_input_tokens"] = window
            model.profile = prof
    except Exception as e:  # noqa: BLE001
        logger.debug("设置 model.profile 失败（不影响对话）：%s", e)

    # 段序：身份/纪律/输出规范 → 知识库用法 → 手动画像 → 已学习偏好。全部是低频变化的内容，
    # 系统提示跨轮稳定，厂商 prompt cache 可命中。v1.8.0：时间锚点从这里移到每轮用户消息
    # 前缀（它每分钟都变，放在系统提示里会让整段前缀缓存每轮失效）。
    system_prompt = "\n".join((AGENT_CHAT_SYSTEM_PROMPT, AGENT_KNOWLEDGE_GUIDE))
    tools = list(CHAT_TOOLS)
    if knowledge_enabled:
        tools += KNOWLEDGE_TOOLS
    if learn_enabled:
        tools += PROFILE_TOOLS
    if mcp_enabled:
        tools.append(mcp_call)

    backend = StateBackend()

    # 传字符串 system_prompt 是前置到 deepagents 内置提示词之前（graph.py:863
    # `system_prompt + "\n\n" + base_prompt`），不是顶掉——内置那段讲 todo/文件工具怎么用，
    # 必须留着。
    return create_deep_agent(
        model=model,
        tools=compile_tools(tools),
        system_prompt=system_prompt,
        backend=backend,
        checkpointer=checkpointer,
        # middleware= 是纯追加，插在基础栈之后、tail 之前——正好满足"排在注入工具的
        # 中间件（filesystem/todo）之后"这个前提，过滤时它们已经注入完毕。
        middleware=[EphemeralContextMiddleware(), ExcludeToolsMiddleware(excluded=_EXCLUDED_TOOLS)],
    )
