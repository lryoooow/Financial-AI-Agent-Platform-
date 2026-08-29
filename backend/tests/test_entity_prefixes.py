"""审计 B7/D8：北交所 920 前缀全线修正 + normalize_ticker 去掉 zfill 猜测。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finresearch.tools.entity import (  # noqa: E402
    _classify_exchange,
    normalize_ticker,
    to_market_ticker,
)
from finresearch.tools.market import _is_bj  # noqa: E402
from finresearch.tools.market_provider import _eastmoney_secid, _sina_symbol  # noqa: E402


def test_bj_920_codes():
    assert _is_bj("920118")
    assert to_market_ticker("920000") == "920000.BJ"
    assert _classify_exchange("920000") == "北证"
    assert _sina_symbol("920000") == "bj920000"
    # 东财 secid 沿用 0.（深证/北交所同市场号，实测 0.920000 可用）
    assert _eastmoney_secid("920000") == "0.920000"


def test_legacy_bj_prefixes_kept():
    assert _is_bj("430047")
    assert to_market_ticker("830799") == "830799.BJ"


def test_sh_sz_regression():
    assert to_market_ticker("600519") == "600519.SH"
    assert to_market_ticker("000001") == "000001.SZ"
    assert to_market_ticker("300750") == "300750.SZ"
    assert to_market_ticker("688981") == "688981.SH"
    assert _sina_symbol("600519") == "sh600519"
    assert _sina_symbol("000001") == "sz000001"
    assert _classify_exchange("600519") == "上证"


def test_normalize_ticker_strips_but_never_guesses():
    """v1.6.0（审计 D8）：zfill 静默补齐曾把 "600" 变 000600（建投能源）—— 张冠李戴。"""
    assert normalize_ticker("600519.SH") == "600519"
    assert normalize_ticker("sh600519") == "600519"
    assert normalize_ticker("SZ000001") == "000001"
    assert normalize_ticker("600") == "600", "不再 zfill 猜测成另一只股票"
    assert normalize_ticker("") == ""
    assert normalize_ticker("贵州茅台") == "贵州茅台"


def test_is_bj_false_for_sz_sh():
    assert not _is_bj("000001")
    assert not _is_bj("600519")
