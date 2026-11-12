"""deepagents 流 → SSE 事件。

流的形状是实测出来的（scratchpad/probe_stream.py），不是照文档猜的：

stream_mode=["updates", "messages"] 下 astream 产出 (mode, data) 元组——
- ("messages", (msg, meta))：
    · AIMessage/AIMessageChunk，meta["langgraph_node"] == "model"
      - 带 tool_calls  → 模型决定调工具（此时 content 通常为空）
      - 不带 tool_calls → 正文，逐块流给前端
    · ToolMessage，meta["langgraph_node"] == "tools"，msg.name 是工具名，content 是 JSON 字符串
- ("updates", {node: payload})：
    · payload 可能是 None（中间件钩子节点，如 TodoListMiddleware.after_model）——必须容忍，
      直接 .items() 会 AttributeError
    · payload 含 "todos" 时是待办快照

关于正文流式：node == "model" 的文本一律流出去。工具调用轮的 content 实测是空的，
所以不会把"中间思考"混进答案；即便某些模型在调工具前先说一句"我查一下行情"，
那句话流给用户也是自然的，不算污染。
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Optional

from ...api import sse
from ...llm import ThinkTagSplitter

logger = logging.getLogger(__name__)

# 工具名 → 前端展示标签。缺失时回落工具名本身，不硬编码兜底文案。
# 新增工具务必在此登记：漏登记不会报错，只会把 prefetch_basics 这种函数名直接甩给用户看。
TOOL_LABELS = {
    # 研究链路
    "prefetch_basics": "取数（行情/财务/新闻/K线）",
    "prefetch_industry": "取行业估值与资讯",
    "compose_report": "撰写报告",
    # 对话链路
    "resolve_entity": "识别标的",
    "search_industry": "查行业估值",
    "get_realtime_quote": "拉取实时行情",
    "get_technical_indicators": "计算技术指标",
    "get_kline": "拉取 K 线",
    "get_company_profile": "查公司概况",
    "get_financial_indicators": "查财务指标",
    "search_news": "检索新闻",
    "web_search": "联网搜索",
    "search_knowledge": "检索本地知识库",
    "remember_user_preference": "记住偏好",
    "write_todos": "梳理任务",
    "read_file": "读取归档",
    "write_file": "写入归档",
    "ls": "浏览归档",
    "glob": "查找归档",
    "grep": "检索归档",
    "edit_file": "编辑归档",
}


def tool_label(name: str) -> str:
    return TOOL_LABELS.get(name, name)


def _extract_text(msg: Any) -> str:
    """从 chunk 提取正文文本（兼容 str / list content，自动剥离 thinking/reasoning 结构化块）。"""
    content = getattr(msg, "content", None)
    if content is None:
        return ""
    if isinstance(content, list):
        out: list[str] = []
        for p in content:
            if isinstance(p, dict):
                btype = str(p.get("type") or "").lower()
                if btype in ("thinking", "reasoning", "thought", "redacted_thinking"):
                    continue
                if "text" in p and p.get("text"):
                    out.append(str(p["text"]))
            elif isinstance(p, str):
                out.append(p)
        return "".join(out)
    return content or ""


def _extract_reasoning(msg: Any) -> str:
    """提取 delta.reasoning_content 或结构化 content list 中的 thinking/reasoning 块。"""
    parts: list[str] = []
    rc = (getattr(msg, "additional_kwargs", None) or {}).get("reasoning_content")
    if isinstance(rc, str) and rc:
        parts.append(rc)
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict):
                btype = str(p.get("type") or "").lower()
                if btype in ("thinking", "reasoning", "thought"):
                    val = p.get("thinking") or p.get("reasoning") or p.get("text")
                    if isinstance(val, str) and val:
                        parts.append(val)
    return "".join(parts)


def _tool_failed(content: Any, msg: Any = None) -> bool:
    """工具返回是否是失败（兼容 ToolMessage.status=='error' 及显式 JSON error 字段）。"""
    if msg is not None and getattr(msg, "status", None) == "error":
        return True
    if isinstance(content, dict):
        return bool(content.get("error"))
    if not isinstance(content, str):
        return False
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and bool(data.get("error"))


def custom_event_to_sse(data: Any) -> Optional[str]:
    """report.py 推送的 custom 事件 → SSE 行（研究链路两种编排共用：LangGraph custom 流 /
    确定性编排的 PUSH_SINK）。不认识的事件返回 None。"""
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    if kind == "report_progress":
        text = data.get("text", "")
        return sse.sse({"type": "tool", "text": text, "report_stage": True}) if text else None
    if kind in ("section_queued", "section_start"):
        # section_queued：段落已排上（等并发闸）；section_start：真正开始撰写
        return sse.sse({"type": kind, "title": data.get("title", "")})
    if kind == "section_thinking":
        # 思维链快照（替换语义，尾部预览 + 累计字数），重连回放不会重复累加
        return sse.sse({
            "type": "section_thinking",
            "title": data.get("title", ""),
            "text": data.get("text", ""),
            "chars": data.get("chars", 0),
        })
    if kind == "section_token":
        # 真流式：板块逐 token 吐字，前端追加到该板块 body（打字机）
        return sse.sse({
            "type": "section_token",
            "title": data.get("title", ""),
            "text": data.get("text", ""),
        })
    if kind == "report_section":
        return sse.sse({
            "type": "section",
            "title": data.get("title", ""),
            "body": data.get("body", ""),
            "source": data.get("source", ""),
        })
    if kind == "summary_token":
        # v1.8.0 单次流式：摘要逐 token 追加（重连回放时由 routes 合并成一帧）
        return sse.sse({"type": "summary_token", "text": data.get("text", "")})
    if kind == "report_thinking":
        # 单次流式的思维链快照（替换语义：尾部预览 + 累计字数）
        return sse.sse({"type": "report_thinking", "text": data.get("text", ""), "chars": data.get("chars", 0)})
    if kind == "sections_reset":
        return sse.sse({"type": "sections_reset"})
    if kind == "research_summary_text":
        # 权威摘要正文（替换语义；不带标的头信息，前端只替换摘要文字）
        return sse.sse({"type": "summary", "text": data.get("text", "")})
    if kind == "research_summary":
        # 摘要是完整事实快照，不伪装成模型 token：前端用替换语义渲染，
        # 重连/回放时不会被再次 append 成重复内容。
        return sse.sse({
            "type": "summary",
            "text": data.get("text", ""),
            "name": data.get("name", ""),
            "ticker": data.get("ticker", ""),
            "price": data.get("price"),
            "change": data.get("change"),
            "changePct": data.get("changePct"),
        })
    return None


async def stream_agent_sse(
    agent: Any,
    inputs: dict,
    config: dict,
    *,
    emit_tokens: bool = True,
) -> AsyncIterator[tuple[str, str]]:
    """把 agent 的流转成 SSE 行。

    产出 (sse_line, text_piece)：text_piece 非空时是正文片段，调用方据此累计最终回复
    以便落库——避免调用方再解析一遍 SSE。

    emit_tokens=False 用于研究链路：那里 agent 的正文是"好的，开始研究"这类过程话术，
    报告正文另有来路（compose_report 的 custom 事件），把它当 token 流给前端会污染报告区。

    custom 事件（report.py 里 get_stream_writer 推的分段进度/摘要快照）经
    custom_event_to_sse 转成 SSE —— 实测 get_stream_writer 在 agent 的工具里照常工作。
    """
    # v1.7.1 + v1.8.1：按 (轮次, 消息 id) 隔离 <think> 拆分器，避免中转站空 id 或跨轮同 id
    # 在第 2 轮工具返回后失效；同时去重流式 tool_calls 并为跨轮正文补换行防【标签】粘连。
    splitters: dict[str, ThinkTagSplitter] = {}
    turn_idx = 0
    saw_tool_output = False
    emitted_calls: set[tuple[int, str]] = set()
    emitted_any_text = False
    last_text_ended_nl = True
    turn_started_text: set[int] = set()

    async for mode, data in agent.astream(
        inputs, config=config, stream_mode=["updates", "messages", "custom"]
    ):
        if mode == "custom":
            line = custom_event_to_sse(data)
            if line:
                yield line, ""
            continue

        if mode == "messages":
            msg, meta = data
            node = (meta or {}).get("langgraph_node")

            if node == "tools":
                # 只认 ToolMessage（工具结果）。研究工具内部的分段撰写 LLM 也会以
                # node=="tools" 的 AIMessageChunk 出现在 messages 流里，必须忽略。
                if getattr(msg, "type", "") != "tool":
                    continue
                saw_tool_output = True
                name = getattr(msg, "name", "") or ""
                if name:
                    status = "error" if _tool_failed(getattr(msg, "content", None), msg) else "ok"
                    yield sse.ev_tool_call(name, tool_label(name), status), ""
                continue

            if node != "model":
                continue

            if saw_tool_output:
                turn_idx += 1
                saw_tool_output = False
                if emit_tokens:
                    yield sse.ev_chat_stage("composing", "正在汇总分析"), ""

            for idx, call in enumerate(getattr(msg, "tool_calls", None) or []):
                name = call.get("name") or ""
                if name:
                    call_key = (turn_idx, str(call.get("id") or f"{idx}:{name}"))
                    if call_key not in emitted_calls:
                        emitted_calls.add(call_key)
                        yield sse.ev_tool_call(name, tool_label(name), "start"), ""
                        yield sse.ev_chat_stage("tool", tool_label(name)), ""

            # v1.7.0+：思维链增量透出（delta.reasoning_content 或结构化 thinking block）。
            rc = _extract_reasoning(msg)
            if rc and emit_tokens:
                yield sse.ev_reasoning(rc), ""

            raw = _extract_text(msg)
            if not raw:
                continue
            key = f"{turn_idx}:{getattr(msg, 'id', '') or ''}"
            sp = splitters.setdefault(key, ThinkTagSplitter())
            piece, inline_rc = sp.feed(raw)
            if inline_rc and emit_tokens:
                yield sse.ev_reasoning(inline_rc), ""
            if piece and emit_tokens:
                if turn_idx > 0 and turn_idx not in turn_started_text:
                    turn_started_text.add(turn_idx)
                    if emitted_any_text and not last_text_ended_nl and not piece.startswith("\n"):
                        piece = "\n\n" + piece
                else:
                    turn_started_text.add(turn_idx)
                emitted_any_text = True
                last_text_ended_nl = piece.endswith("\n")
                yield sse.ev_token(piece), piece
            continue

        if mode == "updates":
            for _node, payload in (data or {}).items():
                # 中间件钩子节点的 payload 是 None，直接 .get 会炸。
                if not isinstance(payload, dict):
                    continue
                todos = payload.get("todos")
                if todos:
                    yield sse.ev_todo(_render_todos(todos)), ""

    # 流结束：把仍在缓冲里的尾巴吐出（只有「疑似 <think> 前缀」或未闭合 think 会留缓冲）
    for sp in splitters.values():
        piece, inline_rc = sp.flush()
        if inline_rc and emit_tokens:
            yield sse.ev_reasoning(inline_rc), ""
        if piece and emit_tokens:
            yield sse.ev_token(piece), piece


def _render_todos(todos: Any) -> list:
    """把 deepagents 的 todos 规整成前端可直接渲染的形状。"""
    out = []
    for t in todos or []:
        if isinstance(t, dict):
            out.append(
                {
                    "content": t.get("content") or t.get("title") or "",
                    "status": t.get("status") or "pending",
                }
            )
    return out
