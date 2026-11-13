"""Record narrowly scoped long-term facts from the user's own words."""

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from ..guardrails.untrusted import looks_like_instruction

MemoryType = Literal["fact", "preference", "constraint", "project"]

_RULES: tuple[tuple[MemoryType, str, re.Pattern, float], ...] = (
    ("constraint", "analysis_scope", re.compile(r"(?:以后|一直|每次|别给我|不要给我).{0,14}(?:不要|别|只看|请用|只关注|讲).+"), 0.85),
    ("preference", "investment_horizon", re.compile(r"(?:我(?:比较)?(?:只做|偏好|主要做|倾向)|投资周期).{0,12}(?:长线|短线|中期|长期|短期).*"), 0.8),
    ("preference", "metrics", re.compile(r"(?:我(?:比较|平时|一直)?(?:更看重|看重|偏好|喜欢|在意|关注)|我长期关注).+"), 0.7),
    ("project", "research_project", re.compile(r"(?:我的研究项目|我在长期跟踪|我正在持续跟踪|我(?:目前|长期)?持有).+"), 0.7),
    ("fact", "market_focus", re.compile(r"(?:我只研究|我主要研究|我熟悉).{1,40}"), 0.5),
)
_PRIVATE = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]+|\b\d{9,}\b|(?:密码|密钥|账户号|持仓金额|银行卡)")


@dataclass(frozen=True, slots=True)
class MemoryDecision:
    memory_type: MemoryType
    content: str
    importance: float
    tag: str


def classify(text: str) -> MemoryDecision | None:
    if looks_like_instruction(text) or _PRIVATE.search(text):
        return None
    for sentence in re.split(r"[。！？\r\n]", text.strip()[:600]):
        sentence = sentence.strip()
        if len(sentence) > 100 or sentence.endswith(("吗", "么", "呢", "?")):
            continue
        for kind, tag, pattern, score in _RULES:
            if pattern.fullmatch(sentence):
                return MemoryDecision(kind, sentence, score, tag)
    return None


def _save(decision: MemoryDecision) -> None:
    from ...knowledge.store import connect

    memory_id = uuid4().hex
    tags = json.dumps([decision.tag], ensure_ascii=False)
    now = time.time()
    with connect() as conn:
        active = conn.execute(
            "SELECT id, content FROM kb_memories WHERE memory_type=? AND tags_json=? AND superseded_by IS NULL",
            (decision.memory_type, tags),
        ).fetchall()
        if any(row["content"] == decision.content for row in active):
            return
        conn.execute(
            "INSERT INTO kb_memories(id, memory_type, content, importance, tags_json, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (memory_id, decision.memory_type, decision.content, decision.importance, tags, now, now),
        )
        for row in active:
            conn.execute("UPDATE kb_memories SET superseded_by=?, updated_at=? WHERE id=?", (memory_id, now, row["id"]))


async def judge(text: str) -> bool:
    decision = classify(text)
    if decision is None or decision.importance < 0.4:
        return False
    await asyncio.to_thread(_save, decision)
    return True


def render_memories() -> str:
    from ...knowledge.store import connect

    with connect() as conn:
        return "\n".join(
            row["content"] for row in conn.execute(
                "SELECT content FROM kb_memories WHERE superseded_by IS NULL"
                " ORDER BY importance DESC, updated_at DESC LIMIT 6"
            )
        )
