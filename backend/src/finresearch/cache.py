"""简单 TTL 内存缓存。

家用单进程足够；可选 PG upsert 留接口位。

v1.6.0（审计 E2）：加 max_entries 容量上限 —— 旧实现过期项仅在被再次访问时清除、
无总量上限，market.py 还会把 3MB 全市场 DataFrame 整个入缓存，长期运行的桌面进程
内存缓慢增长。超限时先清过期项，仍超则按插入序淘汰最旧的。
"""

from __future__ import annotations

import copy
import time
from threading import Lock
from typing import Any, Dict, Optional, Tuple


def _clone_cached(val: Any) -> Any:
    """对可变容器返回浅拷贝（含 provenance 子字典），防止调用方原地修改污染进程内缓存；
    DataFrame 等重型对象保持零拷贝引用。"""
    if isinstance(val, dict):
        out = copy.copy(val)
        prov = out.get("provenance")
        if isinstance(prov, dict):
            out["provenance"] = copy.copy(prov)
        return out
    if isinstance(val, list):
        return list(val)
    return val


class TTLCache:
    def __init__(self, max_entries: int = 256) -> None:
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._lock = Lock()
        self._max_entries = max(16, int(max_entries))

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            exp, val = item
            if exp < now:
                self._store.pop(key, None)
                return None
            return _clone_cached(val)

    def set(self, key: str, value: Any, ttl: float = 5.0) -> None:
        now = time.time()
        stored = _clone_cached(value)
        with self._lock:
            self._store[key] = (now + ttl, stored)
            if len(self._store) > self._max_entries:
                # 先清过期项
                expired = [k for k, (exp, _v) in self._store.items() if exp < now]
                for k in expired:
                    self._store.pop(k, None)
                # 仍超则按插入序淘汰最旧（dict 保序）
                while len(self._store) > self._max_entries:
                    self._store.pop(next(iter(self._store)), None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# 行情类缓存（秒级，避免高频打 akshare）
quote_cache = TTLCache()
# 基本面/新闻类缓存（分钟级；条目少但单条可大——财务 rows / 新闻 items）
slow_cache = TTLCache()
