"""v1.6.3 P1-3：同年财务行必须取年报口径（旧实现取最新报告期=中报混排，同比全错）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import finresearch.tools.fundamentals as fundamentals  # noqa: E402


def _row(date, roe):
    return {"_date": date, "_year": date[:4], "净资产收益率(%)": roe,
            "扣除非经常性损益后的净利润(元)": 100.0e8, "净利润增长率(%)": 10.0,
            "主营业务收入增长率(%)": 8.0, "资产负债率(%)": 20.0}


def _seed():
    """2024 全年报；2025 年报 + 中报（应取年报）；2026 只有中报（回落并标注）。"""
    return [
        _row("2024-12-31", 30.0),
        _row("2025-06-30", 18.0),   # 中报
        _row("2025-12-31", 28.0),   # 年报 —— 必须胜出
        _row("2026-06-30", 15.0),   # 当年仅中报
    ]


def _run_with_seed(monkeypatch, seed):
    """桩在 akshare 层（不是 _last_n_years_indicator）——让真实的年报过滤逻辑跑起来。"""
    import akshare as ak
    import pandas as pd

    def fake_indicator(symbol, start_year):
        df = pd.DataFrame(seed)
        df = df.rename(columns={"_date": "日期"}).drop(columns=["_year"])
        return df

    monkeypatch.setattr(ak, "stock_financial_analysis_indicator", fake_indicator)
    fundamentals.slow_cache.clear()
    return fundamentals.get_financial_indicators("600519.SH")


def test_annual_report_wins_over_interim(monkeypatch):
    result = _run_with_seed(monkeypatch, _seed())
    fins = result["financials"]
    years = fins["years"]
    roe = next(r for r in fins["rows"] if r["name"] == "ROE")["values"]

    assert years.count("2025") == 1, "同年必须去重为一行"
    idx = years.index("2025")
    assert roe[idx] == "28.0%", f"2025 必须取年报 ROE 28.0（旧实现会取中报 18.0），实际 {roe[idx]}"
    assert roe[years.index("2024")] == "30.0%"


def test_interim_fallback_is_flagged(monkeypatch):
    result = _run_with_seed(monkeypatch, _seed())
    note = (result.get("provenance") or {}).get("note") or ""
    assert "2026" in note and "中期报告" in note, f"当年无年报必须如实标注，note={note!r}"
    # 2026 行回落中报 ROE 15.0；v1.7.1（审计 A01）表头如实标注报告期，不再伪装成整年
    roe = next(r for r in result["financials"]["rows"] if r["name"] == "ROE")["values"]
    assert roe[result["financials"]["years"].index("2026中报")] == "15.0%"
    assert result["financials"]["periods"][-1] == "2026-06-30"


def test_interim_yoy_uses_same_period_not_prior_annual(monkeypatch):
    """审计 A01：2026 中报 ROE 15 只能与 2025 中报 18 比（-3pct），不能与 2025 年报 28 比（-13pct）。"""
    import finresearch.tools.facts as facts

    result = _run_with_seed(monkeypatch, _seed())
    fins = result["financials"]
    assert fins["yoy_base"]["period"] == "2025-06-30"
    assert facts.delta_pp_latest(fins, "ROE") == -3.0
    text = facts.render_financial_facts(result)
    assert "同比变动 -3.0pct" in text and "-13.0pct" not in text
    assert "中期报告" in text and "2025-06-30" in text


def test_interim_without_same_period_hides_yoy(monkeypatch):
    """上年同期缺失时比率类同比不展示（宁缺不错），绝不退回「中报 − 年报」。"""
    import finresearch.tools.facts as facts

    seed = [_row("2024-12-31", 30.0), _row("2025-12-31", 28.0), _row("2026-06-30", 15.0)]
    fins = _run_with_seed(monkeypatch, seed)["financials"]
    assert fins["yoy_base"] == {}
    assert facts.delta_pp_latest(fins, "ROE") is None
    assert "-13.0pct" not in facts.render_financial_facts(fins)
    # 绝对值序列推算同比同样被口径闸拦下
    assert facts.yoy_pct_latest(fins, "净利润") is None


def test_income_supplement_matches_report_period_not_publish_date(monkeypatch):
    """审计 A02：利润表按「报告日」对齐；公告日期（次年 3 月）绝不能决定年份。"""
    import akshare as ak
    import pandas as pd

    income = [
        {"报告日": "20251231", "营业收入": 100e8, "营业成本": 40e8, "公告日期": "2026-03-30"},
        {"报告日": "20241231", "营业收入": 90e8, "营业成本": 36e8, "公告日期": "2025-03-30"},
    ]
    monkeypatch.setattr(ak, "stock_financial_report_sina", lambda **kw: pd.DataFrame(income))
    seed = [{k: v for k, v in _row(d, 30.0).items()} for d in ("2024-12-31", "2025-12-31")]
    fins = _run_with_seed(monkeypatch, seed)["financials"]
    rev = next(r for r in fins["rows"] if r["name"] == "营业收入")["values"]
    gm = next(r for r in fins["rows"] if r["name"] == "毛利率")["values"]
    assert rev[fins["years"].index("2025")] == "100.00亿"
    assert rev[fins["years"].index("2024")] == "90.00亿"
    assert gm[fins["years"].index("2025")] == "60.0%"


def test_income_supplement_without_report_period_column_is_skipped(monkeypatch):
    """只有公告日期、没有报告期列时宁可不补，也不拿公告日期顶替。"""
    import akshare as ak
    import pandas as pd

    income = [{"公告日期": "2026-03-30", "营业收入": 100e8, "营业成本": 40e8}]
    monkeypatch.setattr(ak, "stock_financial_report_sina", lambda **kw: pd.DataFrame(income))
    fins = _run_with_seed(monkeypatch, [_row("2025-12-31", 28.0)])["financials"]
    rev = next(r for r in fins["rows"] if r["name"] == "营业收入")["values"]
    assert rev == ["—"]


def test_no_interim_years_no_note(monkeypatch):
    result = _run_with_seed(monkeypatch, [_row("2024-12-31", 30.0), _row("2025-12-31", 28.0)])
    assert "中期报告" not in ((result.get("provenance") or {}).get("note") or "")
