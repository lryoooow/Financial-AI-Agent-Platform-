"""对话文本清洗与管线节点登记。

_strip_md 旧实现最后三行是无差别字符删除（`.replace("*","")` 等），
把 3*5 变成 35、把「排名 #1」变成「排名 1」。这里锁住：语法删掉、正文留下。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from finresearch.api import routes
from finresearch.api.routes import _strip_md


# ────────────────────────────────────────────────────────────
# 删掉的必须是语法
# ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("**核心结论**", "核心结论"),
        ("__加粗__", "加粗"),
        ("这里 *斜体* 结束", "这里 斜体 结束"),
        ("`get_quote()`", "get_quote()"),
        ("## 投资摘要", "投资摘要"),
        ("### 风险", "风险"),
        ("#标题无空格", "标题无空格"),
        ("- 第一点", "· 第一点"),
        ("* 第二点", "· 第二点"),
        ("+ 第三点", "· 第三点"),
        ("```python\nx = 1\n```\n", "x = 1\n"),
    ],
)
def test_markdown_syntax_is_removed(raw, expected):
    assert _strip_md(raw) == expected


# ────────────────────────────────────────────────────────────
# 留下的必须是正文（旧实现在这几条上全错）
# ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw",
    [
        "3*5 等于 15",              # 乘号
        "毛利率 3*5*7 的拆解",       # 连乘
        "排名 #1 的标的",           # 井号编号
        "话题 #A股 #白酒",          # 话题标签
        "变量名 snake_case_here",   # 下划线标识符
        "区间 [2*x, 3*y]",
        "C# 与 F# 的区别",
        "2 ** 10 = 1024",           # 幂运算（无闭合 **）
    ],
)
def test_plain_text_survives_untouched(raw):
    assert _strip_md(raw) == raw


def test_mixed_line_keeps_math_and_strips_bold():
    assert _strip_md("**结论**：3*5 的口径为 15") == "结论：3*5 的口径为 15"


def test_bullet_dash_does_not_eat_negative_number():
    # 行首 "-5%" 不是列表项（减号后无空格）
    assert _strip_md("-5% 的跌幅") == "-5% 的跌幅"


def test_trailing_partial_marker_is_trimmed():
    """流式过程中最后一个 token 可能只到半个标记。"""
    assert _strip_md("正在生成 **") == "正在生成"
    assert _strip_md("代码 `") == "代码"


def test_empty_and_none_like():
    assert _strip_md("") == ""
    assert _strip_md("纯文本") == "纯文本"


def test_structured_labels_are_preserved():
    """【核心结论】这类中文方括号标签是我们自己的结构，绝不能被清洗掉。"""
    text = "【核心结论】💡 现金流稳健\n【关键依据】📊 事实：营收同比 +12.3%"
    assert _strip_md(text) == text


def test_vision_fallback_removes_current_image_from_unseeded_history(monkeypatch):
    from finresearch.agents.deep import chat_agent

    captured = {}

    class FakeAgent:
        async def aget_state(self, _config):
            return SimpleNamespace(values={})

    async def fake_stream(_agent, inputs, _config):
        captured.update(inputs)
        yield routes.sse.ev_token("请补充文字描述"), "请补充文字描述"

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda **kwargs: FakeAgent())
    monkeypatch.setattr(routes, "get_checkpointer", lambda: (None, "memory"))
    monkeypatch.setattr(routes, "build_llm", lambda **kwargs: object())
    monkeypatch.setattr(routes, "_probe_vision", lambda _llm, _images: asyncio.sleep(0, result=False))
    monkeypatch.setattr(routes, "stream_agent_sse", fake_stream)

    async def scenario():
        events = []
        async for line, piece in routes._deepagent_chat_stream(
            conv_id="conv-vision",
            text="",
            history=[{"role": "user", "content": "", "images": ["data:image/png;base64,AA=="]}],
            profile={},
            llm_cfg={"api_key": "test"},
            images=["data:image/png;base64,AA=="],
            user_msg={"role": "user", "content": "", "images": ["data:image/png;base64,AA=="]},
        ):
            events.append((line, piece))
        return events

    events = asyncio.run(scenario())

    assert '"type": "warning"' in events[0][0]
    assert isinstance(captured["messages"][0]["content"], str)
    assert "补充文字描述" in captured["messages"][0]["content"]


# 原本这里有 test_node_labels_cover_every_graph_node，守的是"漏登记节点标签会让该节点
# 静默通过、进度条不推进"。固定图已由 agent 取代，node_labels 不复存在，该不变量作废。
# 它的等价物在 test_deep_agent.py:test_every_exposed_tool_has_a_chinese_label——
# 工具漏登记标签同样会静默失真（把函数名甩给用户看）。




def test_utc_iso_normalizes_naive_utc_timestamps():
    """SQLite CURRENT_TIMESTAMP 是 UTC naive，裸 isoformat() 前端会按本地时区解析（差 8 小时）。"""
    import datetime as dt

    naive = dt.datetime(2026, 9, 28, 5, 30, 0)  # UTC naive（DB 读回形态）
    assert routes._utc_iso(naive) == "2026-09-28T05:30:00+00:00"
    aware = dt.datetime(2026, 9, 28, 13, 30, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert routes._utc_iso(aware) == "2026-09-28T05:30:00+00:00"  # 东八区 → UTC 归一
    assert routes._utc_iso(None) == ""
