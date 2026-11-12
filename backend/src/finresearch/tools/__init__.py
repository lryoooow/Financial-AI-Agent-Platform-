"""工具层（纯函数，返回 JSON 可序列化 dict，带 provenance）。

数字零幻觉：所有数值由 akshare/pandas-ta-classic 计算；
LLM 只引用，不手算。
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")


def clean_for_json(obj: Any) -> Any:
    """递归净化任意对象为严格 RFC 8259 兼容 JSON 值（剔除 NaN/Infinity、拆箱 NumPy/Pandas 标量）。"""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    # 先拦截 pandas.NA / NaT（注意 pd.NaT 是 datetime.datetime 子类，必须早于 datetime 判断）
    tname = type(obj).__name__
    if tname in ("NAType", "NaTType"):
        return None
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [clean_for_json(x) for x in obj]
    item_fn = getattr(obj, "item", None)
    if callable(item_fn):
        try:
            return clean_for_json(item_fn())
        except Exception:  # noqa: BLE001
            pass
    return str(obj)


def now_cst() -> dt.datetime:
    """当前北京时间（naive datetime，统一 A 股交易日/时段/锚点口径）。"""
    return dt.datetime.now(_CST).replace(tzinfo=None)


def now_iso() -> str:
    return now_cst().strftime("%Y-%m-%d %H:%M:%S")


def make_provenance(
    *,
    source: str,
    endpoint: str,
    fetched_at: Optional[str] = None,
    raw_value: Any = None,
    error: Optional[str] = None,
) -> dict:
    """统一的溯源元数据。"""
    return {
        "source": source,
        "endpoint": endpoint,
        "fetched_at": fetched_at or now_iso(),
        "raw_value": raw_value,
        "error": error,
    }
