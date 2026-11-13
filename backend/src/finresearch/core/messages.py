"""Message and tool contracts shared by the engine and application."""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class DomainMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    id: str | None = None
    status: str | None = None
    additional_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolRunResult:
    content: str
    artifact: dict[str, Any] | None = None
    status: Literal["success", "error"] = "success"
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedAIConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str = ""
    fallback_models: tuple[str, ...] = ()
