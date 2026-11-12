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
from concurrent.futures import ThreadPoolExecutor
import functools
import logging
import re
import time

logger = logging.getLogger(__name__)

# 外部行情/财报 IO 独立有界线程池（A-15）：避免慢请求超时后悬挂线程占满 asyncio 默认
# ThreadPoolExecutor，拖慢共用 to_thread 的 SQLite / 知识库读写。
_DATA_IO_EXECUTOR = ThreadPoolExecutor(max_workers=10, thread_name_prefix="fin-data-io")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"'<>|]+")
_UNIX_PATH_RE = re.compile(r"(?:/(?:Users|home|var|tmp|etc|opt|usr)/[^\s\"'<>|]+)")


def sanitize_err_text(msg: object) -> str:
    """脱敏外部异常文本：剥离本地文件路径与换行堆栈，避免流入 SSE / API / 工具输出。"""
    s = str(msg or "").strip()
    if not s:
        return "unknown_error"
    s = _WIN_PATH_RE.sub("<path>", s)
    s = _UNIX_PATH_RE.sub("<path>", s)
    s = re.sub(r"(?i)\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b", "<redacted>", s)
    s = s.splitlines()[0].strip()
    return s[:240]


async def _timed_call(fn, *args, timeout: float = 8.0, label: str = "", fallback=None, **kwargs):
    """带超时地在独立数据线程池跑同步函数；超时/失败返回 (fallback, elapsed_ms, error)，绝不抛异常。"""
    t = time.perf_counter()
    try:
        loop = asyncio.get_running_loop()
        call = functools.partial(fn, *args, **kwargs)
        result = await asyncio.wait_for(
            loop.run_in_executor(_DATA_IO_EXECUTOR, call), timeout=timeout
        )
        return result, int((time.perf_counter() - t) * 1000), None
    except asyncio.TimeoutError:
        logger.warning("%s 超时（%ss）", label or getattr(fn, "__name__", "fn"), timeout)
        return fallback, int((time.perf_counter() - t) * 1000), "timeout"
    except Exception as e:  # noqa: BLE001
        logger.warning("%s 失败：%s", label or getattr(fn, "__name__", "fn"), e)
        return fallback, int((time.perf_counter() - t) * 1000), sanitize_err_text(e)
