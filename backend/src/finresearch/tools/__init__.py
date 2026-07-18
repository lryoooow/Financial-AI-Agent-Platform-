"""工具层（纯函数，返回 JSON 可序列化 dict，带 provenance）。

数字零幻觉：所有数值由 akshare/pandas-ta-classic 计算；
LLM 只引用，不手算。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
