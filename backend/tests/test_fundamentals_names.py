"""审计 P0-A3/A4：财务行名与列名错配。

- A3：营收 pick 候选键与接口真实 86 列不符 → 「营业收入」恒为「—」。
- A4：生产行名「扣非净利润」与全部消费方查询的「净利润」精确匹配不上 →
  事实块/基本面段恒「未获取」，而风险段引用真值，同报告自相矛盾。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 用审计实测的接口真实列名构造假数据（列全缺营收绝对值，仅有增长率）
_ROWS = [
    {
        "_year": "2023",
        "扣除非经常性损益后的净利润(元)": 614.0e8,
        "主营业务收入增长率(%)": 18.7,
        "净利润增长率(%)": 19.2,
        "净资产收益率(%)": 34.1,
        "销售毛利率(%)": 91.8,
        "资产负债率(%)": 21.4,
    },
    {
        "_year": "2024",
        "扣除非经常性损益后的净利润(元)": 744.5e8,
        "主营业务收入增长率(%)": 15.6,
        "净利润增长率(%)": 12.8,
        "净资产收益率(%)": 36.2,
        "销售毛利率(%)": 91.2,
        "资产负债率(%)": 20.7,
    },
]


def test_financial_rows_use_unified_net_profit_name(monkeypatch):
    import finresearch.tools.fundamentals as fundamentals

    monkeypatch.setattr(fundamentals, "_last_n_years_indicator", lambda raw, n=6: _ROWS)
    result = fundamentals.get_financial_indicators("600519.SH")
    fins = result["financials"]

    names = [r["name"] for r in fins["rows"]]
    assert "净利润" in names, "行名必须统一为「净利润」供消费方查询"
    assert "扣非净利润" not in names
    net_row = next(r for r in fins["rows"] if r["name"] == "净利润")
    assert net_row["values"][-1] == "744.50亿"

    # 营收绝对值上游确实无列 → 如实「—」（零幻觉），但不得让整表崩
    rev_row = next(r for r in fins["rows"] if r["name"] == "营业收入")
    assert rev_row["values"][-1] == "—"

    # 同比行有真值
    yoy_row = next(r for r in fins["rows"] if r["name"] == "净利润同比")
    assert yoy_row["values"][-1] == "12.8%"

    # 口径溯源：实际命中的列名留痕
    assert "扣除非经常性损益后的净利润(元)" in (result["provenance"].get("note") or "")


def test_render_financial_facts_reads_dedicated_yoy_rows(monkeypatch):
    import finresearch.tools.facts as facts
    import finresearch.tools.fundamentals as fundamentals

    monkeypatch.setattr(fundamentals, "_last_n_years_indicator", lambda raw, n=6: _ROWS)
    fins = fundamentals.get_financial_indicators("600519.SH")["financials"]

    text = facts.render_financial_facts(fins)
    assert "净利润 744.50亿" in text
    assert "同比 12.8%" in text, "同比必须来自「净利润同比」专用行真值"
    assert "净利润 " + facts.MISSING not in text, "修复前：净利润行因行名错配恒未获取"


def test_row_vals_accepts_alias_list():
    import finresearch.tools.facts as facts

    fins = {"rows": [{"name": "扣非净利润", "values": ["1亿"]}, {"name": "净利润同比", "values": ["5%"]}]}
    assert facts.row_vals(fins, "净利润") == []
    assert facts.row_vals(fins, ["净利润", "扣非净利润"]) == ["1亿"]
    assert facts.row_vals(fins, ["不存在", "净利润同比"]) == ["5%"]
    assert facts.row_vals(fins, []) == []
    assert facts.row_vals(fins, "不存在") == []


def test_row_vals_single_name_backward_compatible():
    import finresearch.tools.facts as facts

    fins = {"rows": [{"name": "ROE", "values": ["30%"]}]}
    assert facts.row_vals(fins, "ROE") == ["30%"]
