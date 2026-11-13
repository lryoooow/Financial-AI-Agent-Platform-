"""Apply per-turn context to provider calls without changing checkpoint input."""

import re
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.constants import END

from ..core.context.assembler import assemble
from ..core.context.budget import estimate_tokens
from ..core.guardrails.sanitizer import strip_inline_reasoning
from ..core.messages import DomainMessage
from .turn_context import current_turn_state

_LEGACY_CONTEXT = re.compile(
    r"^(?:【本轮上下文】.*?【本轮上下文结束】|<turn_context>.*?</turn_context>)\s*",
    re.DOTALL,
)


def _clean_human(content: Any) -> Any:
    if isinstance(content, str):
        return _LEGACY_CONTEXT.sub("", content)
    return [
        {**block, "text": _LEGACY_CONTEXT.sub("", str(block.get("text") or ""))}
        if isinstance(block, dict) and block.get("type") == "text" else block
        for block in content or []
    ]


def _to_domain(message, *, preserve_reasoning: bool = False) -> DomainMessage | None:
    msg_id = getattr(message, "id", None)
    match getattr(message, "type", ""):
        case "human":
            return DomainMessage("user", _clean_human(message.content), id=msg_id)
        case "ai":
            content = message.content
            extra: dict[str, Any] = {}
            if preserve_reasoning and getattr(message, "tool_calls", None):
                for key in ("reasoning_content", "reasoning", "thinking"):
                    if key in (getattr(message, "additional_kwargs", None) or {}):
                        extra[key] = message.additional_kwargs[key]
            if isinstance(content, str):
                content = strip_inline_reasoning(content)
            elif not preserve_reasoning:
                content = [
                    block for block in content
                    if not isinstance(block, dict) or block.get("type") not in ("reasoning", "thinking")
                ]
            return DomainMessage(
                "assistant", content, tool_calls=list(message.tool_calls or []),
                id=msg_id, additional_kwargs=extra,
            )
        case "tool":
            return DomainMessage(
                "tool", message.content,
                tool_call_id=message.tool_call_id,
                name=message.name,
                id=msg_id,
                status=getattr(message, "status", None),
            )
    return None


def _framework_message(message: DomainMessage):
    if message.role == "assistant":
        kwargs: dict[str, Any] = {"content": message.content, "tool_calls": message.tool_calls}
        if message.id:
            kwargs["id"] = message.id
        if message.additional_kwargs:
            kwargs["additional_kwargs"] = dict(message.additional_kwargs)
        return AIMessage(**kwargs)
    if message.role == "tool":
        kwargs = {
            "content": message.content,
            "tool_call_id": message.tool_call_id or "",
            "name": message.name,
        }
        if message.id:
            kwargs["id"] = message.id
        if message.status:
            kwargs["status"] = message.status
        return ToolMessage(**kwargs)
    if message.role == "system":
        return SystemMessage(content=message.content)
    kwargs = {"content": message.content}
    if message.id:
        kwargs["id"] = message.id
    return HumanMessage(**kwargs)


class EphemeralContextMiddleware(AgentMiddleware):
    def _prepare(self, request):
        state = current_turn_state()
        if state is None:
            return request
        raw_msgs = list(request.messages or [])
        last_human = next(
            (idx for idx in range(len(raw_msgs) - 1, -1, -1) if getattr(raw_msgs[idx], "type", "") == "human"),
            len(raw_msgs),
        )
        messages = [
            converted
            for idx, msg in enumerate(raw_msgs)
            if (converted := _to_domain(msg, preserve_reasoning=(idx >= last_human))) is not None
        ]
        # LangChain handles the static system prefix separately from message history.
        system = getattr(request.system_message, "content", "")
        window = state.context_budget or 64000
        assembled = assemble(
            messages, state.context,
            budget=max(256, window - min(4096, window // 5)),
            system_tokens=estimate_tokens(str(system)) + 8,
        )
        return request.override(messages=[_framework_message(msg) for msg in assembled])

    def wrap_model_call(self, request, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._prepare(request))


async def read_thread_messages(agent, config: dict) -> list:
    snapshot = await agent.aget_state(config)
    return list((snapshot.values or {}).get("messages") or [])


async def repair_thread(
    agent, config: dict, messages: list, *, drop_latest_images: bool = False, closing_text: str = "",
) -> None:
    """Close outstanding tool calls and dangling human/tool turns so the thread stays valid."""
    pending: dict[str, str] = {}
    latest_human = None
    for msg in messages:
        if getattr(msg, "type", "") == "human":
            latest_human = msg
        for call in getattr(msg, "tool_calls", None) or []:
            if call.get("id"):
                pending[call["id"]] = call.get("name") or ""
        if getattr(msg, "type", "") == "tool":
            pending.pop(msg.tool_call_id, None)
    fixes: list = [
        ToolMessage("[用户已中止本步调用]", tool_call_id=call_id, name=name, status="error")
        for call_id, name in pending.items()
    ]
    if drop_latest_images and latest_human is not None and isinstance(latest_human.content, list):
        text = [
            block for block in latest_human.content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        fixes.insert(0, latest_human.model_copy(update={"content": text or "（图片未发送）"}))
    if fixes:
        await agent.aupdate_state(config, {"messages": fixes}, as_node="tools")
    last_type = "tool" if pending else (str(getattr(messages[-1], "type", "")).lower() if messages else "")
    if last_type in {"human", "tool"} and not drop_latest_images:
        await agent.aupdate_state(
            config,
            {"messages": [AIMessage(content=closing_text or "[上一轮回复已中止]")]},
            as_node="model",
        )
    await agent.aupdate_state(config, None, as_node=END)
