"""审计 B5：时区统一（naive-UTC vs 本地 naive 混算在东八区恒偏 8 小时）。"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finresearch.api.routes import _iso_utc, _task_age_seconds  # noqa: E402


def test_naive_utc_age_not_skewed_by_local_timezone():
    """SQLite func.now() 存 naive-UTC；东八区机器上 age 必须仍接近真实秒数。"""
    naive_utc_now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    assert abs(_task_age_seconds(naive_utc_now)) < 5  # 旧实现此处 ≈ +8h(28800s)


def test_aware_input_compatible():
    aware = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30)
    assert 25 <= _task_age_seconds(aware) <= 35


def test_none_and_legacy_guard():
    assert _task_age_seconds(None) == 9999.0


def test_iso_utc_appends_offset():
    naive = dt.datetime(2026, 8, 28, 1, 0, 0)  # UTC 存储
    out = _iso_utc(naive)
    assert out.endswith("+00:00")
    assert _iso_utc(None) == ""
    aware = dt.datetime(2026, 8, 28, 1, 0, 0, tzinfo=dt.UTC)
    assert _iso_utc(aware) == "2026-08-28T01:00:00+00:00"
