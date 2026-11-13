"""Per-turn limits, validated tool arguments and retrieval cache."""

from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..core.context.container import ContextContainer

QUOTA_LIMITS = {"market_io": 36, "web_search": 4, "doc_parse": 4, "mcp_call": 8}
TOOL_BUCKETS = {
    **dict.fromkeys((
        "get_realtime_quote", "get_kline", "get_financial_indicators",
        "get_company_profile", "get_technical_indicators", "compute_risk_metrics",
        "search_news", "search_industry", "prefetch_basics", "prefetch_industry",
        "market_overview", "batch_radar",
    ), "market_io"),
    "web_search": "web_search",
    "extract_article_text": "web_search",
}
SYMBOL_TOOLS = frozenset({
    "get_realtime_quote", "get_kline", "get_financial_indicators",
    "get_company_profile", "get_technical_indicators", "compute_risk_metrics",
    "search_news", "prefetch_basics",
})


@dataclass(slots=True)
class TurnToolState:
    conv_id: str = ""
    active_symbol: str = ""
    allow_default_symbol: bool = True
    remaining_steps: int = 40
    quota_limits: dict[str, int] = field(default_factory=lambda: QUOTA_LIMITS.copy())
    reservations: dict[str, int] = field(default_factory=dict)
    trusted_tool_arguments: dict[str, dict[str, Any]] = field(default_factory=dict)
    precondition_blocks: dict[str, str] = field(default_factory=dict)
    seen_call_fingerprints: set[str] = field(default_factory=set)
    cached_results: dict[str, str] = field(default_factory=dict)
    failed_retries: dict[str, int] = field(default_factory=dict)
    invalid_call_ids: set[str] = field(default_factory=set)
    retrieval_cache: dict[tuple, Any] = field(default_factory=dict)
    context: ContextContainer = field(default_factory=ContextContainer)
    context_budget: int = 0

    def _bucket_for(self, tool_name: str) -> str | None:
        if tool_name.startswith("mcp_"):
            return "mcp_call"
        return tool_name if tool_name in self.quota_limits else TOOL_BUCKETS.get(tool_name)

    def try_reserve(self, tool_name: str) -> tuple[bool, str | None]:
        """Check and reserve in one event-loop turn, before invoking a tool."""
        if tool_name in self.precondition_blocks:
            return False, self.precondition_blocks[tool_name]
        if self.remaining_steps <= 0:
            return False, "[STEP_LIMIT] 已达到本轮工具调用上限，请根据已取得的数据回答。"
        bucket = self._bucket_for(tool_name)
        if bucket and bucket in self.quota_limits:
            used = self.reservations.get(bucket, 0)
            if used >= self.quota_limits[bucket]:
                return False, f"[QUOTA_EXCEEDED] 本轮 {bucket} 类工具已用完，请使用已取得的数据。"
            self.reservations[bucket] = used + 1
        self.remaining_steps -= 1
        return True, None

    def release_failed_call(self, tool_name: str, args: dict[str, Any]) -> None:
        """Refund quota and allow up to one retry when a provider call fails transiently."""
        fp = self.fingerprint(tool_name, args)
        retries = self.failed_retries.get(fp, 0)
        if retries >= 1:
            return
        self.failed_retries[fp] = retries + 1
        self.seen_call_fingerprints.discard(fp)
        self.remaining_steps += 1
        bucket = self._bucket_for(tool_name)
        if bucket and self.reservations.get(bucket, 0) > 0:
            self.reservations[bucket] -= 1

    def fingerprint(self, tool_name: str, args: dict[str, Any]) -> str:
        return f"{tool_name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"

    def check_and_mark_fingerprint(self, tool_name: str, args: dict[str, Any]) -> tuple[bool, str | None]:
        fingerprint = self.fingerprint(tool_name, args)
        if fingerprint in self.seen_call_fingerprints:
            cached = self.cached_results.get(fingerprint)
            if cached:
                return False, f"[DUPLICATE_CALL] 本轮相同参数已取到结果，请直接引用：{cached}"
            return False, "[DUPLICATE_CALL] 请使用本轮相同参数的前次结果。"
        self.seen_call_fingerprints.add(fingerprint)
        return True, None

    def remember_result(self, tool_name: str, args: dict[str, Any], summary: str) -> None:
        if summary:
            self.cached_results[self.fingerprint(tool_name, args)] = summary

    def arguments_for(
        self, tool_name: str, model_args: dict[str, Any] | None, *, allowed_fields: set[str] | None = None,
    ) -> dict[str, Any]:
        raw = dict(model_args or {})
        accepts_code = (
            ("code" in allowed_fields or "symbol" in allowed_fields)
            if allowed_fields is not None else (tool_name in SYMBOL_TOOLS)
        )
        for scope in ("*", tool_name):
            for key, value in self.trusted_tool_arguments.get(scope, {}).items():
                if allowed_fields is not None and key not in allowed_fields:
                    continue
                if key in {"code", "symbol"} and (not accepts_code or raw.get(key)):
                    continue
                raw[key] = value
        if self.active_symbol and accepts_code:
            code = str(raw.get("code") or raw.get("symbol") or "").strip()
            digits = "".join(char for char in code if char.isdigit())
            if (not code and self.allow_default_symbol) or (digits and digits == self.active_symbol.split(".")[0]):
                target_key = "symbol" if (allowed_fields and "symbol" in allowed_fields and "code" not in allowed_fields) else "code"
                if target_key == "code":
                    raw.pop("symbol", None)
                raw[target_key] = self.active_symbol
        return raw

    def accept_resolved_symbol(self, symbol: str) -> None:
        if not self.active_symbol:
            self.active_symbol = symbol
        # An explicit different symbol remains valid for a multi-stock question.


_TURN_STATE: contextvars.ContextVar[TurnToolState | None] = contextvars.ContextVar("turn_tool_state", default=None)


def current_turn_state() -> TurnToolState | None:
    return _TURN_STATE.get()


@contextmanager
def bind_turn_state(state: TurnToolState) -> Iterator[TurnToolState]:
    token = _TURN_STATE.set(state)
    try:
        yield state
    finally:
        _TURN_STATE.reset(token)
