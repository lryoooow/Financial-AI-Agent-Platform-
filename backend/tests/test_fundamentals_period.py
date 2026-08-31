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
    # 2026 行回落中报 ROE 15.0
    roe = next(r for r in result["financials"]["rows"] if r["name"] == "ROE")["values"]
    assert roe[result["financials"]["years"].index("2026")] == "15.0%"


def test_no_interim_years_no_note(monkeypatch):
    result = _run_with_seed(monkeypatch, [_row("2024-12-31", 30.0), _row("2025-12-31", 28.0)])
    assert "中期报告" not in ((result.get("provenance") or {}).get("note") or "")
