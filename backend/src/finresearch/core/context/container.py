"""Named context blocks owned by a single turn."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextBlock:
    name: str
    content: str
    priority: int
    budget_tokens: int | None = None


class ContextContainer:
    def __init__(self) -> None:
        self._blocks: dict[str, ContextBlock] = {}

    def upsert_block(self, name: str, content: str, *, priority: int, budget_tokens: int | None = None) -> None:
        content = content.strip()
        if content:
            self._blocks[name] = ContextBlock(name, content, priority, budget_tokens)
        else:
            self._blocks.pop(name, None)

    def blocks(self) -> tuple[ContextBlock, ...]:
        return tuple(self._blocks.values())
