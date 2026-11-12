"""entity 节点：归一化 ticker + 补公司名。"""

from __future__ import annotations

import asyncio
import logging

from ...tools import now_cst
from ...tools.entity import detect_index_code, get_company_name, normalize_ticker
from ..state import ResearchState

logger = logging.getLogger(__name__)


async def entity_node(state: ResearchState) -> dict:
    code = state.get("code", "")
    idx_hit = detect_index_code(code)
    if idx_hit is not None:
        idx_code, idx_name = idx_hit
        msg = (
            f"「{idx_name}（{idx_code}）」为市场指数而非单只股票，个股研究模板不支持直接传入指数代码；"
            "如需研究板块或行业，请切换为「行业研究」模板。"
        )
        logger.warning("[entity] rejected index code: %s -> %s (%s)", code, idx_code, idx_name)
        return {
            "normalized_ticker": idx_code,
            "data_as_of": now_cst().strftime("%Y-%m-%d %H:%M"),
            "error": msg,
            "findings": [],
        }

    norm = normalize_ticker(code)
    # v1.6.0（审计 E2）：种子清单首次加载/过期重载是同步读盘（种子缺失时还会同步拉
    # 全市场），直接调用会卡住事件循环 —— 挪进线程池。
    name = await asyncio.to_thread(get_company_name, norm) or ""
    logger.info("[entity] %s -> %s (%s)", code, norm, name)
    return {
        "normalized_ticker": norm,
        "data_as_of": now_cst().strftime("%Y-%m-%d %H:%M"),
        "findings": [
            {
                "key": "entity",
                "title": "标的归一化",
                "code": norm,
                "name": name,
            }
        ],
    }
