"""Incremental separation of inline reasoning and guarded answer text."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .compliance import sanitize_reasoning

__all__ = [
    "AnswerStreamSanitizer",
    "InlineReasoning",
    "ReasoningStreamGuard",
    "SanitizedDelta",
    "sanitize_reasoning",
    "strip_inline_reasoning",
]

_OPEN = "<think>"
_CLOSE = "</think>"
_TAG = re.compile(r"</?think>", re.IGNORECASE)
_NUMERIC_TAIL = re.compile(r"(?<![A-Za-z0-9])[+-]?\d[\d.,]*[ \t\u00a0]*$")
_SENTENCE_BREAK = re.compile(r"[。！？；\n]")
_COMMA_BREAK = re.compile(r"[，,、]")
_ASCII_TOKEN_CHAR = re.compile(r"[A-Za-z0-9_.:/\\-]")


def _safe_reasoning_cut(buf: str) -> int:
    """Find a safe emission boundary in buffered reasoning that never splits secrets, paths, or clauses."""
    last_sent = -1
    for m in _SENTENCE_BREAK.finditer(buf):
        last_sent = m.end()
    if last_sent > 0:
        return last_sent
    if len(buf) <= 160:
        return 0
    limit = len(buf) - 72
    last_comma = -1
    for m in _COMMA_BREAK.finditer(buf, 0, limit):
        last_comma = m.end()
    if last_comma > 0:
        return last_comma
    if len(buf) <= 320:
        return 0
    # Extreme fallback for punctuation-free run-on text: step back out of any ASCII/path token.
    cut = len(buf) - 96
    while cut > 0 and _ASCII_TOKEN_CHAR.match(buf[cut - 1]):
        cut -= 1
    return cut


class ReasoningStreamGuard:
    """Clause-aligned streaming sanitizer for reasoning CoT chunks (immune to multi-token splitting)."""

    def __init__(self) -> None:
        self._buf = ""

    @property
    def has_pending(self) -> bool:
        return bool(self._buf)

    def feed(self, chunk: str, *, flush_all: bool = False) -> str:
        if chunk:
            self._buf += chunk
        if not self._buf:
            return ""
        if flush_all:
            raw, self._buf = self._buf, ""
            return sanitize_reasoning(raw)
        cut = _safe_reasoning_cut(self._buf)
        if cut <= 0:
            return ""
        candidate, self._buf = self._buf[:cut], self._buf[cut:]
        return sanitize_reasoning(candidate)

    def flush(self) -> str:
        return self.feed("", flush_all=True)


class InlineReasoning:
    """Keep partial tags buffered and support multiple reasoning spans."""

    def __init__(self) -> None:
        self._pending = ""
        self._inside = False
        self.orphan_think_closed = False
        self._saw_tag = False
        self._saw_visible = False

    def feed(self, text: str) -> tuple[str, str]:
        pending = self._pending + text
        self._pending = ""
        visible: list[str] = []
        reasoning: list[str] = []
        cursor = 0
        for match in _TAG.finditer(pending):
            part = pending[cursor:match.start()]
            opening = match[0].lower() == _OPEN
            if self._inside:
                reasoning.append(part)
            elif not opening and not self._saw_tag:
                # Some compatible gateways prefill the opening tag.
                reasoning.append(part)
                self.orphan_think_closed = self._saw_visible
            else:
                visible.append(part)
            self._inside = opening
            self._saw_tag = True
            cursor = match.end()

        tail = pending[cursor:]
        tail_lower = tail.lower()
        for size in range(min(len(tail), len(_CLOSE) - 1), 0, -1):
            if _OPEN.startswith(tail_lower[-size:]) or _CLOSE.startswith(tail_lower[-size:]):
                self._pending = tail[-size:]
                tail = tail[:-size]
                break
        (reasoning if self._inside else visible).append(tail)
        answer = "".join(visible)
        self._saw_visible |= bool(answer.strip())
        return answer, "".join(reasoning)

    def flush(self) -> tuple[str, str]:
        tail = self._pending
        self._pending = ""
        if not tail:
            return "", ""
        if self._inside:
            return "", tail
        self._saw_visible |= bool(tail.strip())
        return tail, ""


def strip_inline_reasoning(text: str) -> str:
    parser = InlineReasoning()
    answer, _ = parser.feed(text)
    tail, _ = parser.flush()
    combined = answer + tail
    return combined.lstrip() if _TAG.search(text) else combined


@dataclass(slots=True)
class SanitizedDelta:
    visible_text: str = ""
    reasoning_text: str = ""
    compliance_triggered: bool = False


class AnswerStreamSanitizer:
    """Hold back the boundary of every emitted token until both visible text and inline CoT are checked.

    ``compliance_checker`` violations (investment-advice wording etc.) still hard-stop the stream.
    ``numeric_locator`` (optional) returns unverifiable number spans; those are replaced in place by
    ``REDACTION`` so one unprovable figure never truncates an otherwise grounded answer. Only when a
    bad number already reached the user, or redactions exceed ``max_redactions`` (a hallucination
    burst), does the numeric path fall back to a hard stop (``numeric_violated``).
    """

    REDACTION = "〔数值待核〕"

    def __init__(
        self,
        compliance_checker: Callable[..., list[str]] | None = None,
        lookahead_chars: int = 16,
        *,
        numeric_locator: Callable[..., list] | None = None,
        max_redactions: int = 8,
        sentence_hold_chars: int = 160,
    ) -> None:
        self._parser = InlineReasoning()
        self._reasoning_guard = ReasoningStreamGuard()
        self._checker = compliance_checker
        self._locator = numeric_locator
        self._lookahead = max(0, lookahead_chars)
        self._max_redactions = max_redactions
        self._sentence_hold = sentence_hold_chars
        self._visible = ""
        self._pending = ""
        self.violated = False
        self.numeric_violated = False
        self.redactions = 0

    @property
    def visible_text(self) -> str:
        return self._visible

    def feed(self, chunk: str) -> SanitizedDelta:
        if self.violated:
            return SanitizedDelta()
        answer, reasoning = self._parser.feed(chunk)
        if self._parser.orphan_think_closed:
            reasoning = self._pending + reasoning
            self._pending = ""
            self._parser.orphan_think_closed = False
        return self._filter(answer, reasoning, final=False)

    def flush(self) -> SanitizedDelta:
        if self.violated:
            return SanitizedDelta()
        answer, reasoning = self._parser.flush()
        return self._filter(answer, reasoning, final=True)

    def _redact_numbers(self, *, final: bool) -> int | None:
        """Redact closed-sentence numeric issues in pending text; return a hold position or None.

        Returns -1 when the numeric path must hard-stop.
        """
        base = len(self._visible)
        issues = self._locator(self._visible + self._pending, streaming=not final) if self._locator else []
        if not issues:
            return None
        hold_at: int | None = None
        spans: list[tuple[int, int]] = []
        for issue in issues:
            start, end = issue[0], issue[1]
            if start < base:
                return -1
            rel_start, rel_end = start - base, end - base
            if not final:
                tail = self._pending[rel_end:]
                if not _SENTENCE_BREAK.search(tail) and len(tail) < self._sentence_hold:
                    hold_at = rel_start if hold_at is None else min(hold_at, rel_start)
                    continue
            if spans and rel_start < spans[-1][1]:
                continue
            spans.append((rel_start, rel_end))
        if self.redactions + len(spans) > self._max_redactions:
            return -1
        shift = 0
        for rel_start, rel_end in reversed(spans):
            self._pending = self._pending[:rel_start] + self.REDACTION + self._pending[rel_end:]
        for rel_start, rel_end in spans:
            if hold_at is not None and rel_end <= hold_at:
                shift += len(self.REDACTION) - (rel_end - rel_start)
        self.redactions += len(spans)
        return None if hold_at is None else hold_at + shift

    def _filter(self, answer: str, reasoning: str, *, final: bool) -> SanitizedDelta:
        flush_reasoning = final or not self._parser._inside
        safe_reasoning = self._reasoning_guard.feed(reasoning, flush_all=flush_reasoning)
        self._pending += answer
        candidate = self._visible + self._pending
        if self._checker and self._checker(candidate, streaming=not final):
            self.violated = True
            self._pending = ""
            return SanitizedDelta(reasoning_text=safe_reasoning, compliance_triggered=True)
        hold_at = self._redact_numbers(final=final)
        if hold_at == -1:
            self.violated = True
            self.numeric_violated = True
            self._pending = ""
            return SanitizedDelta(reasoning_text=safe_reasoning, compliance_triggered=True)
        cut = len(self._pending) if final else max(0, len(self._pending) - self._lookahead)
        if hold_at is not None:
            cut = min(cut, hold_at)
        if (self._checker or self._locator) and not final:
            if tail := _NUMERIC_TAIL.search(self._pending):
                cut = min(cut, tail.start())
            while cut > 0 and self._pending[cut - 1] in "0123456789.,+-":
                cut -= 1
        out, self._pending = self._pending[:cut], self._pending[cut:]
        self._visible += out
        return SanitizedDelta(out, safe_reasoning)
