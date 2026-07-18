"""简单 TTL 内存缓存。

家用单进程足够；可选 PG upsert 留接口位。
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any, Dict, Optional, Tuple


class TTLCache:
    def __init__(self) -> None:
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._lock = Lock()

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
            return val

    def set(self, key: str, value: Any, ttl: float = 5.0) -> None:
        with self._lock:
            self._store[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# 行情类缓存（秒级，避免高频打 akshare）
quote_cache = TTLCache()
# 基本面/新闻类缓存（分钟级）
slow_cache = TTLCache()
