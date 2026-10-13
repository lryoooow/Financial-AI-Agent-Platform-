"""实体解析：模糊匹配 A 股标的。

搜索路径必须快：这里只加载全市场代码/名称清单，不在搜索阶段拉全市场实时快照。
实时价格和涨跌幅由用户选中股票后的 quote 接口获取。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from importlib import resources
from typing import Optional

from rapidfuzz import fuzz, process

from . import make_provenance

logger = logging.getLogger(__name__)
_SEED_RESOURCE = "data/a_code_name_seed.json"

# 交易所后缀约定（前端用 SH/SZ）
_EXCHANGE_MAP = {
    "sh": "上证",
    "sz": "深证",
    "bj": "北证",
}


# ────────────────────────────────────────────────────────────
# 市场指数目录（行情页大盘条 / 初始视图）
#
# 000001 与平安银行同码：只有带市场后缀的完整形式（"000001.SH"）才按指数处理，
# 裸 6 位输入仍按股票规则（000001 → 深市平安银行）。前端指数入口必须传完整形式。
# ────────────────────────────────────────────────────────────
INDEX_CATALOG: dict[str, str] = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
    "000300.SH": "沪深300",
}
# 名称/别名 → 指数（resolve 用）
_INDEX_ALIASES: dict[str, str] = {
    "上证指数": "000001.SH", "上证": "000001.SH", "大盘": "000001.SH", "A股大盘": "000001.SH",
    "沪市": "000001.SH",
    "深证成指": "399001.SZ", "深成指": "399001.SZ", "深市": "399001.SZ",
    "创业板指": "399006.SZ", "创业板": "399006.SZ",
    "科创50": "000688.SH", "科创板50": "000688.SH", "科创版50": "000688.SH",
    "沪深300": "000300.SH", "沪深三百": "000300.SH",
}


def is_index(code: str) -> bool:
    """完整形式的代码是否是目录内指数（如 "000001.SH"）。"""
    if not code:
        return False
    c = str(code).strip().upper()
    if c in INDEX_CATALOG:
        return True
    # 前缀形式 "sh000001" / "SH000001"（c 已 upper）
    for prefix in ("SH", "SZ"):
        if c.startswith(prefix) and f"{c[len(prefix):]}.{prefix}" in INDEX_CATALOG:
            return True
    return False


# 上交所号段：60 主板 / 68 科创板 / 90 B股 / 11 可转债 /
# 50 封闭式基金·LOF、51、52、56、58 ETF（588xxx 为科创板 ETF）。
SH_PREFIXES = ("60", "68", "90", "11", "50", "51", "52", "56", "58")
# 深交所号段：00 主板 / 30 创业板 / 20 B股 / 12 可转债 / 15、16、18 基金。
SZ_PREFIXES = ("00", "30", "20", "12", "15", "16", "18")
# 北交所号段（含新 920 段）。
BJ_PREFIXES = ("43", "83", "87", "88", "92")


def is_sh(code: str) -> bool:
    """纯 6 位代码是否属于上交所。"""
    return code.startswith(SH_PREFIXES)


def is_bj(code: str) -> bool:
    """纯 6 位代码是否属于北交所。"""
    return code.startswith(BJ_PREFIXES)


def _classify_exchange(code: str) -> str:
    """按代码前缀判断交易所（akshare 仅给 code，不给交易所）。"""
    c = code.strip()
    if is_sh(c):
        return _EXCHANGE_MAP["sh"]
    if c.startswith(SZ_PREFIXES):
        return _EXCHANGE_MAP["sz"]
    if is_bj(c):
        return _EXCHANGE_MAP["bj"]
    return "A股"


def normalize_ticker(code: str) -> str:
    """归一化前端传入的 ticker 到纯 6 位代码。

    前端用 "600519.SH"，akshare 大多数接口要纯代码 "600519"。
    例外：目录内指数保留完整形式（"000001.SH"）——剥掉后缀会与同码股票
    （000001 平安银行）混淆，后续 is_index/符号映射都依赖完整形式判指数。
    """
    if not code:
        return ""
    c = code.strip().upper()
    if c in INDEX_CATALOG:
        return c
    for sep in (".", ":"):
        if sep in c:
            c = c.split(sep)[0]
    # 去掉前缀 SH/SZ
    for prefix in ("SH", "SZ", "BJ"):
        if c.startswith(prefix):
            c = c[len(prefix):]
    return c.zfill(6) if c.isdigit() else c


def to_market_ticker(code: str) -> str:
    """纯代码 → 前端格式（600519.SH）；目录内指数原样返回。"""
    c = normalize_ticker(code)
    if c in INDEX_CATALOG:
        return c
    if is_sh(c):
        return f"{c}.SH"
    if is_bj(c):
        return f"{c}.BJ"
    return f"{c}.SZ"


def _compact_text(value: str) -> str:
    return "".join(str(value or "").upper().split())


def _normalize_rows(rows: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for r in rows or []:
        code = str(r.get("code", "")).strip().zfill(6)
        name = str(r.get("name", "")).strip()
        if not (code.isdigit() and len(code) == 6 and name) or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": name})
    return out


def _load_code_name_seed() -> list[dict]:
    try:
        text = resources.files("finresearch").joinpath(_SEED_RESOURCE).read_text(encoding="utf-8")
        return _normalize_rows(json.loads(text))
    except Exception as e:  # noqa: BLE001
        logger.warning("load bundled A-share seed failed: %s", e)
        return []


def _load_code_name_from_akshare() -> list[dict]:
    import akshare as ak

    df = ak.stock_info_a_code_name()
    df = df[["code", "name"]].dropna().copy()
    return _normalize_rows(df.to_dict(orient="records"))


_REFRESH_LOCK = threading.Lock()
_REFRESH_RUNNING = False
_REFRESH_LAST_ATTEMPT = 0.0
_REFRESH_INTERVAL = 7 * 86400
_REFRESH_RETRY_DELAY = 3600
_CACHE_TTL = 8 * 86400


def _publish_code_name_rows(rows: list[dict], source: str, updated_at: float) -> None:
    """原子更新清单与搜索索引，在线刷新失败时不会覆盖种子数据。"""
    from ..cache import slow_cache

    normalized = _normalize_rows(rows)
    if not normalized:
        return
    code_map = {str(row["code"]).zfill(6): row for row in normalized}
    candidates = [
        f"{str(row['code']).zfill(6)} {row['name']} {_compact_text(row['name'])}"
        for row in normalized
    ]
    slow_cache.set("a_code_name", normalized, ttl=_CACHE_TTL)
    slow_cache.set("a_code_name_source", source, ttl=_CACHE_TTL)
    slow_cache.set("a_code_name_updated_at", updated_at, ttl=_CACHE_TTL)
    slow_cache.set(
        "a_code_name_index",
        {"rows": normalized, "code_map": code_map, "candidates": candidates},
        ttl=_CACHE_TTL,
    )


def _refresh_code_name_in_background() -> None:
    """后台刷新新股/更名清单；失败时继续使用内置种子。"""
    global _REFRESH_LAST_ATTEMPT, _REFRESH_RUNNING
    with _REFRESH_LOCK:
        now = time.time()
        if _REFRESH_RUNNING or now - _REFRESH_LAST_ATTEMPT < _REFRESH_RETRY_DELAY:
            return
        _REFRESH_RUNNING = True
        _REFRESH_LAST_ATTEMPT = now

    def run() -> None:
        global _REFRESH_RUNNING
        try:
            rows = _load_code_name_from_akshare()
            _publish_code_name_rows(rows, "akshare", time.time())
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh A-share code/name list failed; keep bundled seed: %s", exc)
        finally:
            with _REFRESH_LOCK:
                _REFRESH_RUNNING = False

    threading.Thread(target=run, name="a-share-index-refresh", daemon=True).start()


def _load_code_name_list():
    """加载全市场股票清单，并按七天周期后台刷新。"""
    from ..cache import slow_cache

    cached = slow_cache.get("a_code_name")
    if cached is not None:
        last_update = slow_cache.get("a_code_name_updated_at")
        if not last_update or (time.time() - float(last_update)) >= _REFRESH_INTERVAL:
            _refresh_code_name_in_background()
        return cached

    rows = _load_code_name_seed()
    if not rows:
        try:
            rows = _load_code_name_from_akshare()
            _publish_code_name_rows(rows, "akshare", time.time())
            return rows
        except Exception:  # noqa: BLE001
            logger.exception("load A-share code/name list failed")
            return []

    _publish_code_name_rows(rows, "bundled-seed", 0.0)
    _refresh_code_name_in_background()
    return rows


def _load_search_index() -> dict:
    """加载搜索索引。

    rows 来自 slow_cache；索引同样缓存，避免每次输入都重建 candidates/code_map。
    """
    from ..cache import slow_cache

    cached = slow_cache.get("a_code_name_index")
    if cached is not None:
        return cached
    rows = _load_code_name_list()
    code_map = {str(r["code"]).zfill(6): r for r in rows}
    candidates = [f"{str(r['code']).zfill(6)} {r['name']} {_compact_text(r['name'])}" for r in rows]
    index = {"rows": rows, "code_map": code_map, "candidates": candidates}
    slow_cache.set("a_code_name_index", index, ttl=12 * 3600)
    return index


def _hit_row(r: dict) -> dict:
    raw = str(r["code"]).zfill(6)
    return {
        "code": to_market_ticker(raw),
        "name": str(r.get("name", "")),
        "exchange": _classify_exchange(raw),
        "industry": "",
        "price": 0.0,
        "changePct": 0.0,
    }


def resolve_entity(query: str, limit: int = 8) -> dict:
    # 指数别名优先：搜"上证指数/大盘/创业板"等必须命中指数而不是某只同码股票
    # （"上证"若走模糊股票匹配会命中"上海XX"类公司）。
    q = _compact_text(query)
    if q in _INDEX_ALIASES:
        code = _INDEX_ALIASES[q]
        return {"results": [{"code": code, "name": INDEX_CATALOG[code]}]}
    q = (query or "").strip()
    if not q:
        return {"results": [], "provenance": make_provenance(source="local", endpoint="a_code_name_seed")}

    exact_code = normalize_ticker(q)
    if exact_code.isdigit() and len(exact_code) == 6:
        try:
            idx = _load_search_index()
            r = idx["code_map"].get(exact_code)
            if r:
                return {
                    "results": [_hit_row(r)],
                    "provenance": make_provenance(
                        source="local",
                        endpoint="a_code_name_seed",
                        raw_value=f"exact={exact_code}",
                    ),
                }
        except Exception as e:  # noqa: BLE001
            logger.warning("exact code lookup index failed: %s", e)

        return {
            "results": [_hit_row({"code": exact_code, "name": exact_code})],
            "provenance": make_provenance(
                source="local",
                endpoint="ticker-normalize",
                raw_value=f"exact={exact_code}",
            ),
        }

    try:
        idx = _load_search_index()
        rows = idx["rows"]
        candidates = idx["candidates"]
        limit = max(1, int(limit or 8))
        q_compact = _compact_text(q)
        seen = set()
        picked = []

        for r in rows:
            code = str(r["code"]).zfill(6)
            name_compact = _compact_text(str(r.get("name", "")))
            if q_compact and (q_compact in code or q_compact in name_compact):
                seen.add(code)
                picked.append(_hit_row(r))
                if len(picked) >= limit:
                    break

        matches = process.extract(
            q_compact or q,
            candidates,
            scorer=fuzz.WRatio,
            limit=limit * 3,
            score_cutoff=45,
        )
        for _m_text, _score, idx in matches:
            r = rows[idx]
            code = str(r["code"]).zfill(6)
            if code in seen:
                continue
            seen.add(code)
            picked.append(_hit_row(r))
            if len(picked) >= limit:
                break

        return {
            "results": picked,
            "provenance": make_provenance(
                source="local",
                endpoint="a_code_name_seed",
                raw_value=f"query={q}, matches={len(picked)}",
            ),
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("resolve_entity failed")
        return {
            "results": [],
            "provenance": make_provenance(
                source="local",
                endpoint="a_code_name_seed",
                error=str(e),
            ),
        }


def get_company_name(code: str) -> Optional[str]:
    """单股票名（best-effort）。"""
    try:
        rows = _load_code_name_list()
        raw = normalize_ticker(code)
        for r in rows:
            if r["code"] == raw:
                return r["name"]
    except Exception:  # noqa: BLE001
        return None
    return None
