"""对话文本清洗与管线节点登记。

_strip_md 旧实现最后三行是无差别字符删除（`.replace("*","")` 等），
把 3*5 变成 35、把「排名 #1」变成「排名 1」。这里锁住：语法删掉、正文留下。
"""

from __future__ import annotations

import pytest

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


# 原本这里有 test_node_labels_cover_every_graph_node，守的是"漏登记节点标签会让该节点
# 静默通过、进度条不推进"。固定图已由 agent 取代，node_labels 不复存在，该不变量作废。
# 它的等价物在 test_deep_agent.py:test_every_exposed_tool_has_a_chinese_label——
# 工具漏登记标签同样会静默失真（把函数名甩给用户看）。


