"""entity 节点：归一化 ticker + 补公司名。"""

from __future__ import annotations

import datetime as dt
import logging

from ...tools.entity import normalize_ticker, get_company_name
from ..state import ResearchState

logger = logging.getLogger(__name__)


async def entity_node(state: ResearchState) -> dict:
    code = state.get("code", "")
    norm = normalize_ticker(code)
    name = get_company_name(norm) or ""
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
