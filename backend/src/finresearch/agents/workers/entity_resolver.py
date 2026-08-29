"""entity 节点：归一化 ticker + 补公司名。"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from ...tools.entity import normalize_ticker, get_company_name
from ..state import ResearchState

logger = logging.getLogger(__name__)


async def entity_node(state: ResearchState) -> dict:
    code = state.get("code", "")
    norm = normalize_ticker(code)
    # v1.6.0（审计 E2）：种子清单首次加载/过期重载是同步读盘（种子缺失时还会同步拉
    # 全市场），直接调用会卡住事件循环 —— 挪进线程池。
    name = await asyncio.to_thread(get_company_name, norm) or ""
    logger.info("[entity] %s -> %s (%s)", code, norm, name)
    return {
        "normalized_ticker": norm,
        "data_as_of": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "findings": [
            {
                "key": "entity",
                "title": "标的归一化",
                "code": norm,
                "name": name,
            }
        ],
    }
