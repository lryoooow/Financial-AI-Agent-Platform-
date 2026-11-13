"""Stable event identifiers and durable, bounded turn metadata."""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..agents._utils import sanitize_err_text
from ..core.guardrails.sanitizer import sanitize_reasoning


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


async def watch_disconnect(request: Any, token: CancellationToken, work: asyncio.Task) -> None:
    while not token.is_cancelled:
        if await request.is_disconnected():
            token.cancel()
            work.cancel()
            return
        await asyncio.sleep(0.2)


@dataclass(slots=True)
class EventBridge:
    parent_run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    agent_runs: dict[str, str] = field(default_factory=dict)
    started: dict[str, tuple[dict, float]] = field(default_factory=dict)

    def _ids(self, agent: str) -> dict[str, str]:
        agent_run = self.agent_runs.setdefault(agent, uuid.uuid4().hex)
        return {
            "parent_run_id": self.parent_run_id, "agent_run_id": agent_run,
            "child_run_id": uuid.uuid4().hex,
        }

    def start_tool(self, agent: str, call_id: str, name: str, label: str, args: dict) -> dict:
        public_args = {
            key: value for key, value in args.items()
            if key.lower().replace("_", "") not in {"apikey", "key", "token", "path", "authorization", "password"}
        }
        event = {
            **self._ids(agent), "type": "tool_call", "event_type": "agent_status",
            "name": name, "label": label, "call_id": call_id, "status": "start",
            "stage": "tool_execution_started",
            "args_summary": sanitize_err_text(json.dumps(public_args, ensure_ascii=False, default=str)),
        }
        self.started[call_id] = (event, time.perf_counter())
        return event

    def finish_tool(self, agent: str, call_id: str, name: str, label: str, status: str, artifact: Any) -> list[dict]:
        existing = self.started.pop(call_id, None)
        if existing is None:
            event = self.start_tool(agent, call_id, name, label, {})
            _, started_at = self.started.pop(call_id)
        else:
            event, started_at = existing
        provenance = artifact.get("provenance") if isinstance(artifact, dict) else None
        end = {
            **event, "status": status,
            "stage": "tool_execution_completed" if status == "ok" else "tool_execution_failed",
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000),
            "provenance": provenance,
        }
        events = [end]
        if isinstance(artifact, dict):
            events.append({
                **{key: event[key] for key in ("parent_run_id", "agent_run_id", "child_run_id", "call_id")},
                "type": "tool_artifact", "event_type": "tool_artifact", "stage": "artifact_ready",
                "name": name, "label": label, "artifact": artifact,
            })
        return events


@dataclass(slots=True)
class TurnTranscript:
    trace: list[dict] = field(default_factory=list)
    calls: dict[str, dict] = field(default_factory=dict)
    artifacts: list[dict] = field(default_factory=list)
    reasoning_started_at: float | None = None
    reasoning_elapsed_ms: int = 0
    reasoning_chunks: list[str] = field(default_factory=list)
    reasoning_chars: int = 0

    def record(self, event: dict | None) -> None:
        if not event:
            return
        kind = event.get("type")
        if kind == "reasoning":
            if self.reasoning_started_at is None:
                self.reasoning_started_at = time.monotonic()
            raw = str(event.get("text") or "")
            if raw and self.reasoning_chars < 8000:
                take = raw[: 8000 - self.reasoning_chars]
                self.reasoning_chunks.append(take)
                self.reasoning_chars += len(take)
        elif kind in {"token", "done"} and self.reasoning_started_at is not None:
            self._finish_reasoning()
        elif kind == "tool_call":
            if self.reasoning_started_at is not None:
                self._finish_reasoning()
            if event.get("call_id"):
                self.trace.append({key: value for key, value in event.items() if key != "artifact"})
                self.calls[event["call_id"]] = event
        elif kind == "tool_artifact" and len(self.artifacts) < 24:
            self.artifacts.append(event)

    def _finish_reasoning(self) -> None:
        if self.reasoning_started_at is not None:
            self.reasoning_elapsed_ms += round((time.monotonic() - self.reasoning_started_at) * 1000)
            self.reasoning_started_at = None

    def fields(self, status: str) -> dict:
        self._finish_reasoning()
        calls = [
            {**call, "status": "aborted"} if call.get("status") == "start" else call
            for call in self.calls.values()
        ]
        summary = sanitize_reasoning("".join(self.reasoning_chunks)).strip()[:6000]
        if not summary and self.reasoning_elapsed_ms:
            summary = "模型完成资料分析与回答组织。"
        return {
            "reasoning_summary": summary,
            "thinking_elapsed_ms": self.reasoning_elapsed_ms,
            "agent_trace": self.trace,
            "tool_invocations": calls,
            "artifacts": self.artifacts,
            "turn_status": status,
        }
