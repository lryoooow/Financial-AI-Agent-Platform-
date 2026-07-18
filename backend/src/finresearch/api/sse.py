"""SSE 事件格式化（text/event-stream）。

事件 type：
- node：节点切换 {type:'node', name, label}
- tool：工具进度 {type:'tool', text}
- token：流式文本片段（研究摘要 / 对话回复）{type:'token', text}
- stage：对话思考阶段 {type:'stage', stage, label}（understanding/thinking/composing）
- tool_call：agent 真实工具调用 {type:'tool_call', name, label, status}（deepagent 链路）
- todo：agent 待办列表快照 {type:'todo', items:[{content, status}]}（deepagent 链路）
- done：研究 {type:'done', report}；对话用 sse({"type":"done",...}) 自定义负载
- error：{type:'error', message}

tool_call / todo 是 deepagents 链路新增的。前端 Workspace.tsx 的 STAGES 早就给每个阶段
声明了 tools: [...]，但旧的固定管线从不发工具事件——那些字段一直是装饰。这两个事件就是
用来把它们兑现成真的。
"""

from __future__ import annotations

import json
from typing import Any


def sse(event: dict) -> str:
    """格式化为 SSE 一行：`data: {json}\n\n`。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def ev_node(name: str, label: str) -> str:
    return sse({"type": "node", "name": name, "label": label})


def ev_tool(text: str) -> str:
    return sse({"type": "tool", "text": text})


def ev_token(text: str) -> str:
    return sse({"type": "token", "text": text})


def ev_chat_stage(stage: str, label: str) -> str:
    """对话思考阶段提示（前端据此展示'正在理解问题/正在思考/正在组织回复'）。"""
    return sse({"type": "stage", "stage": stage, "label": label})


def ev_tool_call(name: str, label: str, status: str = "start") -> str:
    """agent 真实工具调用。status：start / ok / error。"""
    return sse({"type": "tool_call", "name": name, "label": label, "status": status})


def ev_todo(items: list) -> str:
    """agent 待办列表快照（deepagents write_todos 的状态）。"""
    return sse({"type": "todo", "items": items})


def ev_done(report: Any) -> str:
    return sse({"type": "done", "report": report})


def ev_error(message: str) -> str:
    return sse({"type": "error", "message": message})
