"""v1.6.3 P1-2：A 股周末从不开市——调休补班的周六不得判为交易日。

旧实现用 chinese_calendar.is_workday（补班周六返 True），导致 _ensure_fresh 在
补班周六全天把所有行情源判为"陈旧"而拒绝 → 行情/分时/K线全挂（每年 3-6 天）。
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finresearch.tools.context import is_trading_day  # noqa: E402


def _find_makeup_saturday(year: int):
    """从 chinese_calendar 找一个'补班周六'（is_workday=True 的周末）；找不到返回 None。"""
    try:
        import chinese_calendar as cc

        d = dt.date(year, 1, 1)
        while d.year == year:
            if d.weekday() >= 5 and cc.is_workday(d):
                return d
            d += dt.timedelta(days=1)
    except Exception:
        pass
    return None


def test_makeup_saturday_is_not_trading_day():
    """补班周六：办公日但 A 股休市 —— is_trading_day 必须为 False。"""
    for year in (2025, 2026):
        sat = _find_makeup_saturday(year)
        if sat is None:
            continue
        assert not is_trading_day(dt.datetime(sat.year, sat.month, sat.day, 11, 0)), \
            f"{sat}（补班周六）不得判为交易日"


def test_makeup_saturday_no_freshness_rejection():
    """补班周六盘中：_requires_today_* 必须为 False（不要求今日分时/K线）。"""
    from finresearch.tools.market import _requires_today_intraday, _requires_today_kline

    sat = _find_makeup_saturday(2026) or _find_makeup_saturday(2025)
    if sat is None:
        return  # 日历数据不可用时跳过（无补班日可测）
    n = dt.datetime(sat.year, sat.month, sat.day, 11, 0)
    assert not _requires_today_intraday(n)
    assert not _requires_today_kline(n)


def test_normal_weekday_and_holiday():
    # 2026-08-26 周三：交易日
    assert is_trading_day(dt.datetime(2026, 8, 26, 11, 0)) is True
    # 2026-10-01 国庆（周四）：休市
    assert is_trading_day(dt.datetime(2026, 10, 1, 11, 0)) is False
    # 普通周六：休市
    assert is_trading_day(dt.datetime(2026, 8, 29, 11, 0)) is False
