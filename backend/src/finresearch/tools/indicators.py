"""技术指标（封装层，复用 market._compute_indicators_df）。

对外提供 compute_ta(klines: list[dict]) → list[{name,value,signal}]，
供 agent 节点直接调用。
"""

from __future__ import annotations

import logging

import pandas as pd

from . import make_provenance
from .market import _compute_indicators_df

logger = logging.getLogger(__name__)


def compute_ta(klines: list) -> dict:
    """从 K 线字典列表计算技术指标（至少需要 15 根有效 K 线，避免单根 K 线产出失真指标）。"""
    try:
        if not klines:
            raise ValueError("K 线为空")
        if len(klines) < 15:
            raise ValueError(f"K 线样本不足（当前 {len(klines)} 根，至少需 15 根）")
        df = pd.DataFrame(klines)
        data = _compute_indicators_df(df)
        return {
            "data": data,
            "provenance": make_provenance(
                source="pandas-ta-classic", endpoint="compute_ta", raw_value=f"n={len(klines)}"
            ),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "data": [],
            "provenance": make_provenance(
                source="pandas-ta-classic", endpoint="compute_ta", error=str(e)
            ),
        }
