"""Keep recent tool exchanges whole and inject only the current context."""

from dataclasses import replace

from ..messages import DomainMessage
from .budget import clip, estimate_tokens
from .container import ContextContainer

_ARCHIVED = "[历史工具输出已归档]"
_ABORTED = "[用户已中止本步调用]"
_KEEP_PRIOR_TURNS = 2


def tool_groups(messages: list[DomainMessage]) -> list[list[DomainMessage]]:
    groups: list[list[DomainMessage]] = []
    for message in messages:
        if message.role == "tool":
            if groups and any(call.get("id") == message.tool_call_id for call in groups[-1][0].tool_calls):
                groups[-1].append(message)
            else:
                groups.append([message])
        else:
            groups.append([message])
    for group in groups:
        if group[0].role != "assistant" or not group[0].tool_calls:
            continue
        responses = {message.tool_call_id for message in group[1:]}
        for call in group[0].tool_calls:
            call_id = call.get("id")
            if call_id and call_id not in responses:
                group.append(DomainMessage("tool", _ABORTED, tool_call_id=call_id, name=call.get("name"), status="error"))
    return groups


def _cost(message: DomainMessage) -> int:
    content = message.content
    if isinstance(content, str):
        cost = estimate_tokens(content)
    else:
        cost = sum(
            768 if block.get("type") in {"image", "image_url"} else estimate_tokens(str(block.get("text") or ""))
            for block in content if isinstance(block, dict)
        )
    return cost + estimate_tokens(str(message.tool_calls)) + 8


def _aged_prior_indices(prior: list[DomainMessage], keep_turns: int = _KEEP_PRIOR_TURNS) -> set[int]:
    """Age tool outputs only in older completed turns; never touch recent turns or partial groups."""
    user_starts = [idx for idx, msg in enumerate(prior) if msg.role == "user"]
    if len(user_starts) <= keep_turns:
        return set()
    cutoff = user_starts[-keep_turns]
    return {idx for idx in range(cutoff) if prior[idx].role == "tool"}


def assemble(
    history: list[DomainMessage], blocks: ContextContainer, *, budget: int, system_tokens: int,
) -> list[DomainMessage]:
    current = next((i for i in range(len(history) - 1, -1, -1) if history[i].role == "user"), len(history))
    prior_raw = history[:current]
    aged = _aged_prior_indices(prior_raw)
    prior_messages = [
        replace(msg, content=_ARCHIVED) if idx in aged else msg
        for idx, msg in enumerate(prior_raw)
    ]
    # Current active turn (`history[current:]`) is NEVER archived so parallel tool bursts remain intact.
    latest = [msg for group in tool_groups(history[current:]) for msg in group]
    available = max(0, budget - system_tokens - max(128, sum(map(_cost, latest))))

    selected: list[tuple[int, str]] = []
    for block in sorted(blocks.blocks(), key=lambda entry: -entry.priority):
        if available <= 0 and block.priority < 100:
            continue
        floor = 24 if block.priority >= 100 else 0
        cap = block.budget_tokens if block.budget_tokens is not None else max(floor, available - 8)
        allowance = max(floor, min(max(floor, available - 8), cap))
        text = clip(block.content, allowance)
        if text:
            rendered = f"【{block.name}】\n{text}"
            available = max(0, available - estimate_tokens(rendered) - 4)
            selected.append((block.priority, rendered))

    context_text = "\n\n".join(rendered for _, rendered in sorted(selected, key=lambda entry: -entry[0]))
    transient = [DomainMessage("system", f"<turn_context>\n{context_text}\n</turn_context>")] if context_text else []

    kept: list[list[DomainMessage]] = []
    for group in reversed(tool_groups(prior_messages)):
        cost = sum(map(_cost, group))
        if cost > available:
            break
        available -= cost
        kept.append(group)
    return [
        *(msg for group in reversed(kept) for msg in group),
        *transient,
        *latest,
    ]
