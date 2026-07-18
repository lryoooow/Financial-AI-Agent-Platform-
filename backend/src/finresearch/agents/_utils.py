"""agents 公共工具：统一的带超时同步调用包装。

research 与 market 节点此前各自重复实现 _timed_call（签名与返回值不同），
此处提取为单一实现，消除重复。

返回值统一为 (result, elapsed_ms, error)：
- 成功：error 为 None
- 超时/失败：返回 (fallback, elapsed_ms, error_str)，绝不抛异常

market 旧版已用 (data, ms, error) 形态，零适配；research 旧版用 ok_bool 形态，
调用方用 `ok = error is None` 适配。
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


async def _timed_call(fn, *args, timeout: float = 8.0, label: str = "", fallback=None, **kwargs):
    """带超时地在线程池跑同步函数；超时/失败返回 (fallback, elapsed_ms, error)，绝不抛异常。"""
    t = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs), timeout=timeout
        )
        return result, int((time.perf_counter() - t) * 1000), None
    except asyncio.TimeoutError:
        logger.warning("%s 超时（%ss）", label or getattr(fn, "__name__", "fn"), timeout)
        return fallback, int((time.perf_counter() - t) * 1000), "timeout"
    except Exception as e:  # noqa: BLE001
        logger.warning("%s 失败：%s", label or getattr(fn, "__name__", "fn"), e)
        return fallback, int((time.perf_counter() - t) * 1000), str(e)
