"""审计 E2：节假日兜底表未来年份补齐（2027 起静态表原本空白）。"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finresearch.tools.context import _cn_holiday, _dynamic_holidays, _HOLIDAYS  # noqa: E402


def test_known_2026_holidays():
    assert _cn_holiday(dt.datetime(2026, 10, 1)) is True
    assert _cn_holiday(dt.datetime(2026, 1, 1)) is True
    # 国庆后的周五（2026-10-09）是交易日
    assert _cn_holiday(dt.datetime(2026, 10, 9)) is False


def test_2027_covered_when_package_supports():
    """chinese_calendar 覆盖 2027 时，兜底表必须被动态补齐（旧实现 2027 全空白）。"""
    generated = _dynamic_holidays([2027])
    if not generated:
        import pytest

        pytest.skip("chinese_calendar 未覆盖 2027（包数据未更新），静态表兜底")
    assert "2027-10-01" in _HOLIDAYS or "2027-10-01" in generated
    assert _cn_holiday(dt.datetime(2027, 10, 1)) is True


def test_normal_weekday_not_holiday():
    assert _cn_holiday(dt.datetime(2026, 8, 28)) is False  # 周五，非节假日
