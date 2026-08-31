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
    # v1.6.3：隔离换源（网络）——本用例只验行名统一，营收补全行为另测
    monkeypatch.setattr(fundamentals, "_income_statement_annual", lambda raw, years: {})
    result = fundamentals.get_financial_indicators("600519.SH")
    fins = result["financials"]

    names = [r["name"] for r in fins["rows"]]
    assert "净利润" in names, "行名必须统一为「净利润」供消费方查询"
    assert "扣非净利润" not in names
    net_row = next(r for r in fins["rows"] if r["name"] == "净利润")
    assert net_row["values"][-1] == "744.50亿"

    # 营收绝对值主源无列 + 换源也不可用 → 如实「—」（零幻觉），但不得让整表崩
    rev_row = next(r for r in fins["rows"] if r["name"] == "营业收入")
    assert rev_row["values"][-1] == "—"


def test_income_statement_fills_revenue_and_gross_margin(monkeypatch):
    """v1.6.3 B2：营收/毛利率由新浪利润表（年报口径）补全 + note 留痕。"""
    import finresearch.tools.fundamentals as fundamentals

    # 变体夹具：2024 行去掉毛利率列（模拟真实上游「销售毛利率」列全量 NaN 的实测形态）
    rows_variant = []
    for r in _ROWS:
        r2 = dict(r)
        if r2["_year"] == "2024":
            r2.pop("销售毛利率(%)", None)
        rows_variant.append(r2)

    monkeypatch.setattr(fundamentals, "_last_n_years_indicator", lambda raw, n=6: rows_variant)
    monkeypatch.setattr(fundamentals, "_income_statement_annual", lambda raw, years: {
        "2024": {"revenue": 1241.0e8, "gross_margin_pct": 91.9},
    })
    result = fundamentals.get_financial_indicators("600519.SH")
    fins = result["financials"]
    rev = next(r for r in fins["rows"] if r["name"] == "营业收入")
    gm = next(r for r in fins["rows"] if r["name"] == "毛利率")
    i = fins["years"].index("2024")
    assert rev["values"][i] == "1241.00亿", f"营收应由利润表补全，实际 {rev['values'][i]}"
    assert gm["values"][i] == "91.9%", f"毛利率应补全，实际 {gm['values'][i]}"
    note = (result.get("provenance") or {}).get("note") or ""
    assert "新浪利润表" in note and "补全" in note, f"换源必须留痕：{note!r}"

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
