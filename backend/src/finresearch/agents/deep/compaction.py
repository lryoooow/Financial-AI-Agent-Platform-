"""对话上下文自动压缩（v1.8.0，参考 Codex 的 auto-compact）。

Codex 的做法：上下文用量逼近窗口阈值时，让模型写一份「交接摘要」，然后用
「摘要 + 最近的用户消息」作为新的历史继续对话，旧的长历史不再进入模型。

本项目的映射：
- agent 的历史在 LangGraph 检查点线程里（thread_id），UI 历史在 ChatDelta 里，两者独立；
- 压缩 = 生成中文交接摘要 → 线程代际 +1（thread_id = conv 或 conv:gN）→ 新线程以
  「前情摘要 + 预算内的最近几轮原文」播种。旧线程原样保留（可回放、删除会话时一并清理）；
- 摘要写入 ChatContextSummary（会话的当前代际与摘要），并进入本地知识库（跨会话记忆）。

估算用本项目的中文经验估算（tools.context.estimate_tokens）—— langchain 的
count_tokens_approximately 按 4 字符/token 估，中文会低估 2-3 倍，阈值形同虚设。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

from ...tools.context import estimate_tokens

logger = logging.getLogger(__name__)

# deepagents 内置系统提示 + 工具说明书的体量（实测约 6-8k token），计入用量
SYSTEM_OVERHEAD_TOKENS = 8_000
SUMMARY_MAX_CHARS = 1_600
_TOOL_RESULT_PREVIEW = 300

COMPACT_SYSTEM_PROMPT = (
    "你是对话交接助手。下面是一段金融研究对话的记录（可能附带上一轮的前情摘要）。"
    "请写一份交接摘要，让另一位分析师只读摘要就能无缝接着与用户对话。\n"
    "必须包含以下小节（没有内容的小节写「无」）：\n"
    "【用户目标】用户想解决的问题与关注重点；\n"
    "【涉及标的与关键数据】讨论过的标的（名称+代码），以及对话中出现过的关键数字——"
    "每个数字必须保留其数据时点或来源，不得改写数值、不得补充记录里没有的数字；\n"
    "【已形成的结论】已经给出的判断与依据；\n"
    "【未决问题】尚未回答或用户打算追问的问题；\n"
    "【用户偏好与约束】用户表达过的偏好、风险承受、投资周期、表述要求等。\n"
    f"要求：中文纯文本、不用 Markdown 符号、总长不超过 {SUMMARY_MAX_CHARS} 字；"
    "只依据记录，禁止编造；不得出现买入/卖出等指令性词汇。"
)

SUMMARY_PREFIX = "【会话前情摘要（较早的对话已自动压缩，以下为要点，非完整原文）】"
SUMMARY_ACK = "好的，我已了解此前的对话要点，会在此基础上继续。"


def thread_id_for(conversation_id: str, generation: int) -> str:
    """第 0 代沿用会话 id（兼容 v1.8 之前已存在的检查点线程）。"""
    return conversation_id if int(generation or 0) <= 0 else f"{conversation_id}:g{int(generation)}"


def all_thread_ids(conversation_id: str, generation: int) -> list[str]:
    return [thread_id_for(conversation_id, g) for g in range(0, max(0, int(generation or 0)) + 1)]


# ── 消息文本化 ───────────────────────────────────────────
def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(str(b.get("text") or ""))
                elif b.get("type") in ("image_url", "image"):
                    parts.append("[图片]")
            else:
                parts.append(str(b))
        return " ".join(parts)
    return str(content or "")


def _msg_role(m: Any) -> str:
    t = getattr(m, "type", None) or (m.get("role") if isinstance(m, dict) else "")
    return {"human": "user", "ai": "assistant", "tool": "tool", "system": "system"}.get(t, t or "user")


def message_tokens(m: Any) -> int:
    content = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
    n = estimate_tokens(_content_text(content)) + 4
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("image_url", "image", "input_image"):
                n += 1_100  # 视觉块固定计入约 1100 token，防止多图会话低估窗口不触发压缩
    for tc in (getattr(m, "tool_calls", None) or []):
        n += estimate_tokens(str(tc.get("name", ""))) + estimate_tokens(str(tc.get("args", "")))
    return n


def messages_tokens(messages: Iterable[Any]) -> int:
    return sum(message_tokens(m) for m in messages or [])


def render_transcript(messages: list[Any], max_chars: int) -> str:
    """把线程消息渲染成摘要输入：工具结果只留预览；超预算时保留最新部分。"""
    lines: list[str] = []
    for m in messages or []:
        role = _msg_role(m)
        content = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        text = _content_text(content).strip()
        if role == "user":
            lines.append(f"用户：{text}")
        elif role == "assistant":
            calls = getattr(m, "tool_calls", None) or []
            if calls:
                names = "、".join(str(c.get("name", "")) for c in calls)
                lines.append(f"助手（调用工具：{names}）" + (f"：{text}" if text else ""))
            elif text:
                lines.append(f"助手：{text}")
        elif role == "tool":
            name = getattr(m, "name", "") or "工具"
            preview = re.sub(r"\s+", " ", text)[:_TOOL_RESULT_PREVIEW]
            lines.append(f"[工具结果 {name}] {preview}{'…' if len(text) > _TOOL_RESULT_PREVIEW else ''}")
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) + 1 > max_chars:
            kept.append("（更早的对话已省略）")
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(reversed(kept))


def fallback_summary(messages: list[Any], previous: str = "") -> str:
    """模型摘要失败时的确定性兜底：保留用户提问与助手的【核心结论】行。"""
    parts: list[str] = []
    if previous:
        parts.append(previous[: SUMMARY_MAX_CHARS // 2])
    asks = [_content_text(getattr(m, "content", "")).strip() for m in messages if _msg_role(m) == "user"]
    if asks:
        parts.append("【用户提问】" + "；".join(a[:80] for a in asks[-8:] if a))
    concl = []
    for m in messages:
        if _msg_role(m) == "assistant":
            hit = re.search(r"【核心结论】([^\n【]*)", _content_text(getattr(m, "content", "")))
            if hit:
                concl.append(hit.group(1).strip()[:90])
    if concl:
        parts.append("【已形成的结论】" + "；".join(concl[-6:]))
    return "\n".join(parts)[:SUMMARY_MAX_CHARS] or "（较早对话已压缩，无可提取要点）"


# ── 判定与执行 ───────────────────────────────────────────
@dataclass
class CompactDecision:
    used_tokens: int
    window: int
    threshold: int

    @property
    def needed(self) -> bool:
        return self.used_tokens >= self.threshold


def decide(messages: list[Any], incoming_text: str, window: int, threshold_pct: int) -> CompactDecision:
    used = SYSTEM_OVERHEAD_TOKENS + messages_tokens(messages) + estimate_tokens(incoming_text or "")
    threshold = int(window * max(40, min(90, int(threshold_pct))) / 100)
    return CompactDecision(used_tokens=used, window=window, threshold=threshold)


async def summarize(llm: Any, messages: list[Any], previous: str, window: int, timeout: float) -> tuple[str, bool]:
    """生成交接摘要。返回 (摘要, 是否模型生成)。失败回落确定性摘要，绝不抛错。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from ...llm import strip_think

    budget_chars = max(4_000, int(window * 0.45 * 1.4))  # 约 45% 窗口的中文字符量
    transcript = render_transcript(messages, budget_chars)
    user = (f"【上一轮前情摘要】\n{previous}\n\n" if previous else "") + f"【对话记录】\n{transcript}"
    if llm is None:
        return fallback_summary(messages, previous), False
    try:
        resp = await asyncio.wait_for(llm.ainvoke([SystemMessage(COMPACT_SYSTEM_PROMPT), HumanMessage(user)]), timeout)
        text = strip_think(_content_text(getattr(resp, "content", resp))).strip()
        if len(text) < 20:
            raise ValueError("摘要过短")
        return text[: SUMMARY_MAX_CHARS + 200], True
    except Exception as e:  # noqa: BLE001
        logger.warning("对话压缩摘要失败，使用确定性兜底：%s", e)
        return fallback_summary(messages, previous), False


def seed_messages(summary: str, recent: list[dict], budget_tokens: int) -> list[dict]:
    """新线程的播种消息：前情摘要 + 预算内的最近几轮（从最新往回取，保证以用户消息开头）。"""
    out: list[dict] = []
    if summary:
        out.append({"role": "user", "content": f"{SUMMARY_PREFIX}\n{summary}"})
        out.append({"role": "assistant", "content": SUMMARY_ACK})
    picked: list[dict] = []
    used = 0
    for m in reversed(recent or []):
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = str(m.get("content") or "").strip()
        imgs = m.get("images") if isinstance(m.get("images"), list) else []
        if imgs and role == "user":
            img_note = f"[用户上传了 {len(imgs)} 张图片]"
            text = f"{text}\n{img_note}" if text else img_note
        if not text:
            continue
        cost = estimate_tokens(text) + 4
        if used + cost > budget_tokens:
            break
        picked.append({"role": role, "content": text})
        used += cost
    picked.reverse()
    return out + normalize_turns(picked)


def normalize_turns(msgs: list[dict]) -> list[dict]:
    """严格的 user/assistant 交替：以 user 开头、以 assistant 结尾（本轮提问随后追加），
    相邻同角色合并。deepseek-reasoner 等端点拒绝连续同角色消息（中断的提问没有回复时常见）。"""
    merged: list[dict] = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1] = {"role": m["role"], "content": f"{merged[-1]['content']}\n\n{m['content']}"}
        else:
            merged.append(dict(m))
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    while merged and merged[-1]["role"] != "assistant":
        merged.pop()
    return merged
