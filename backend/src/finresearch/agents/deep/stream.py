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
from typing import Any, AsyncIterator

from ...api import sse

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
    """从 chunk 提取文本（兼容 str / list content）。

    与 routes.py:_extract_piece 同形；此处独立一份是因为 routes 那份是 legacy 链路的
    私有实现，两条链路不互相依赖，等 legacy 下线时可一并收敛。
    """
    content = getattr(msg, "content", None)
    if content is None:
        return ""
    if isinstance(content, list):
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    return content or ""


def _tool_failed(content: Any) -> bool:
    """工具返回是否是失败。

    deep/tools.py:_fail 约定失败时带显式 error 字段（而非空值），这里据此判定。
    非 JSON 内容一律当成功——宁可漏报也不要把正常结果标成红的。
    """
    if not isinstance(content, str):
        return False
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and bool(data.get("error"))


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

    custom 事件（report.py 里 get_stream_writer 推的分段进度/摘要快照）转成 tool/section/
    summary 三种 SSE，字段与旧固定管线路由发的逐字段一致——实测 get_stream_writer 在 agent
    的工具里照常工作，所以分段进度 UI 不用改。
    """
    async for mode, data in agent.astream(
        inputs, config=config, stream_mode=["updates", "messages", "custom"]
    ):
        if mode == "custom":
            if not isinstance(data, dict):
                continue
            kind = data.get("type")
            if kind == "report_progress":
                text = data.get("text", "")
                if text:
                    yield sse.sse({"type": "tool", "text": text, "report_stage": True}), ""
            elif kind == "section_start":
                # 真流式：某板块开始撰写，前端把它置为 streaming、清空 body
                yield sse.sse({"type": "section_start", "title": data.get("title", "")}), ""
            elif kind == "section_token":
                # 真流式：板块逐 token 吐字，前端追加到该板块 body（打字机）
                yield sse.sse({
                    "type": "section_token",
                    "title": data.get("title", ""),
                    "text": data.get("text", ""),
                }), ""
            elif kind == "report_section":
                yield sse.sse({
                    "type": "section",
                    "title": data.get("title", ""),
                    "body": data.get("body", ""),
                    "source": data.get("source", ""),
                }), ""
            elif kind == "research_summary":
                # 摘要是完整事实快照，不伪装成模型 token：前端用替换语义渲染，
                # 重连/回放时不会被再次 append 成重复内容。
                yield sse.sse({
                    "type": "summary",
                    "text": data.get("text", ""),
                    "name": data.get("name", ""),
                    "ticker": data.get("ticker", ""),
                    "price": data.get("price"),
                    "change": data.get("change"),
                    "changePct": data.get("changePct"),
                }), ""
            continue

        if mode == "messages":
            msg, meta = data
            node = (meta or {}).get("langgraph_node")

            if node == "tools":
                name = getattr(msg, "name", "") or ""
                if name:
                    status = "error" if _tool_failed(getattr(msg, "content", None)) else "ok"
                    yield sse.ev_tool_call(name, tool_label(name), status), ""
                continue

            if node != "model":
                continue

            for call in getattr(msg, "tool_calls", None) or []:
                name = call.get("name") or ""
                if name:
                    yield sse.ev_tool_call(name, tool_label(name), "start"), ""
                    yield sse.ev_chat_stage("tool", tool_label(name)), ""

            piece = _extract_text(msg)
            if piece and emit_tokens:
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
