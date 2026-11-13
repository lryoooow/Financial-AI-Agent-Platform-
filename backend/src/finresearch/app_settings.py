"""应用偏好（v1.8.0 设置页）：存 LocalSetting「app-settings」，读取时逐字段回填默认值并校验。

只放后端真正消费的偏好；桌面外壳的偏好（托盘/自启）由 Electron 自己存，不经后端。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

APP_SETTINGS_KEY = "app-settings"

DEFAULTS: dict[str, Any] = {
    # 研究撰写模式：stream=单次流式（默认）/ parallel=分段并行
    "research_write_mode": "stream",
    # 研究默认模板（前端研究模式初始选中）
    "default_template": "equity_deep",
    # 上下文窗口（K tokens）；0 = 按模型名自动推断
    "context_window_k": 0,
    # 自动压缩阈值（占上下文窗口的百分比）
    "compact_threshold_pct": 70,
    # 对话 agent 可检索本地知识库（研究报告 + 历史对话摘要）
    "knowledge_enabled": True,
    # 从使用行为与对话中学习用户偏好并注入上下文
    "learn_profile": True,
}

_CHOICES = {
    "research_write_mode": {"stream", "parallel"},
    "default_template": {"equity_deep", "industry", "earnings", "risk", "market_recap"},
}
_RANGES = {
    "context_window_k": (0, 2000),
    "compact_threshold_pct": (40, 90),
}


def _defaults() -> dict[str, Any]:
    out = dict(DEFAULTS)
    try:  # 撰写模式默认跟随服务端配置（.env REPORT_WRITE_MODE），用户在设置页显式选择后以用户为准
        from .config import settings

        mode = str(getattr(settings, "report_write_mode", "stream") or "stream").strip().lower()
        out["research_write_mode"] = mode if mode in _CHOICES["research_write_mode"] else "stream"
    except Exception:  # noqa: BLE001
        pass
    return out


def normalize(payload: Any) -> dict[str, Any]:
    """逐字段回填默认值；非法值丢弃回落默认（不让坏数据进入运行链路）。"""
    out = _defaults()
    if not isinstance(payload, dict):
        return out
    for key, default in DEFAULTS.items():
        if key not in payload:
            continue
        val = payload[key]
        if isinstance(default, bool):
            if isinstance(val, bool):
                out[key] = val
        elif isinstance(default, int):
            try:
                num = int(val)
            except (TypeError, ValueError):
                continue
            lo, hi = _RANGES.get(key, (num, num))
            out[key] = min(hi, max(lo, num))
        elif key in _CHOICES:
            if isinstance(val, str) and val in _CHOICES[key]:
                out[key] = val
    return out


async def load_app_settings() -> dict[str, Any]:
    """从本地库读取（DB 不可用时返回默认值，绝不抛错）。"""
    try:
        from .db.models import LocalSetting
        from .db.session import session_scope

        async with session_scope() as s:
            if s is None:
                return _defaults()
            row = await s.get(LocalSetting, APP_SETTINGS_KEY)
            return normalize(row.payload if row else None)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取应用偏好失败，使用默认值：%s", e)
        return _defaults()
