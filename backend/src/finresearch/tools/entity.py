"""实体解析：模糊匹配 A 股标的。

搜索路径必须快：这里只加载全市场代码/名称清单，不在搜索阶段拉全市场实时快照。
实时价格和涨跌幅由用户选中股票后的 quote 接口获取。
"""

from __future__ import annotations

import json
import logging
import re
import time
from importlib import resources
from typing import Optional

from rapidfuzz import process, fuzz

from . import make_provenance, now_iso

logger = logging.getLogger(__name__)
_SEED_RESOURCE = "data/a_code_name_seed.json"

# 交易所后缀约定（前端用 SH/SZ）
_EXCHANGE_MAP = {
    "sh": "上证",
    "sz": "深证",
    "bj": "北证",
}


def _classify_exchange(code: str) -> str:
    """按代码前缀判断交易所（akshare 仅给 code，不给交易所）。"""
    c = code.strip()
    if c.startswith(("60", "68", "90", "11", "51", "56")):
        return _EXCHANGE_MAP["sh"]
    if c.startswith(("00", "30", "20", "12", "15", "16")):
        return _EXCHANGE_MAP["sz"]
    if c.startswith(("43", "83", "87", "88")):
        return _EXCHANGE_MAP["bj"]
    return "A股"


def normalize_ticker(code: str) -> str:
    """归一化前端传入的 ticker 到纯 6 位代码。

    前端用 "600519.SH"，akshare 大多数接口要纯代码 "600519"。
    """
    if not code:
        return ""
    c = code.strip().upper()
    for sep in (".", ":"):
        if sep in c:
            c = c.split(sep)[0]
    # 去掉前缀 SH/SZ
    for prefix in ("SH", "SZ", "BJ"):
        if c.startswith(prefix):
            c = c[len(prefix):]
    return c.zfill(6) if c.isdigit() else c


def to_market_ticker(code: str) -> str:
    """纯代码 → 前端格式（600519.SH）。"""
    c = normalize_ticker(code)
    if c.startswith(("60", "68", "90", "11", "51", "56")):
        return f"{c}.SH"
    if c.startswith(("43", "83", "87", "88")):
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


def _load_code_name_list():
    """加载全市场股票清单（带缓存）。"""
    from ..cache import slow_cache

    cached = slow_cache.get("a_code_name")
    if cached is not None:
        # 1.2.3：种子数据过期检查（>7天则后台静默更新）
        last_update = slow_cache.get("a_code_name_updated_at")
        if last_update and (time.time() - last_update) < 7 * 86400:
            return cached
    rows = _load_code_name_seed()
    source = "bundled-seed"
    if not rows:
        try:
            rows = _load_code_name_from_akshare()
            source = "akshare"
        except Exception as e:  # noqa: BLE001
            logger.exception("load A-share code/name list failed")
            rows = []
            source = f"failed:{e}"
    slow_cache.set("a_code_name", rows, ttl=12 * 3600)
    slow_cache.set("a_code_name_source", source, ttl=12 * 3600)
    slow_cache.set("a_code_name_updated_at", time.time(), ttl=12 * 3600)
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


# ────────────────────────────────────────────────────────────
# 自然语言里的标的检测（对话事实注入用）
#
# resolve_entity 拿整句话做模糊匹配会命中噪声；而"公司全称必须是问题的子串"这条守卫
# 又太严——"茅台现在多少钱"里没有"贵州茅台"，最常见的问法反而不触发。
# 这里改为：从问题中切出 2–6 字的候选片段，去别名索引里查"该片段是某公司名的子串"。
# ────────────────────────────────────────────────────────────
_CJK_RUN = re.compile(r"[一-鿿]{2,}")
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# 高频通用词：要么是行业通名（出现在大量公司名里），要么是日常/行情词汇，
# 单独出现时不构成标的提及。"今天大盘怎么样"不该命中"今天国际"。
_ALIAS_STOPWORDS = {
    # 行业通名
    "公司", "股份", "集团", "科技", "控股", "中国", "国际", "实业", "发展", "电子",
    "信息", "网络", "数据", "智能", "新材", "材料", "能源", "电力", "银行", "证券",
    "保险", "医药", "生物", "食品", "环保", "机械", "设备", "制造", "有限", "通信",
    # 日常与行情词汇
    "今天", "明天", "昨天", "现在", "目前", "最近", "未来", "大盘", "行情", "走势",
    "股票", "股市", "市场", "板块", "指数", "基金", "投资", "风险", "收益", "估值",
    "财报", "业绩", "分析", "建议", "帮我", "这家", "那家", "怎么", "什么", "如何",
    "多少", "价格", "情况", "表现", "前景", "机会", "请问", "一下", "可以", "应该",
}
# 一个片段匹配到超过这么多家公司，说明它是通用词而非标的名
_ALIAS_MAX_FANOUT = 8


def _name_affixes(name: str) -> list[str]:
    """公司名的前缀与后缀（长度 ≥2），不含中间子串。

    "贵州茅台" → 贵州/贵州茅/贵州茅台/州茅台/茅台
    只收前后缀是精确率的关键：中间子串（如"德时"之于"宁德时代"）不是任何人的简称。
    """
    out = []
    n = len(name)
    for size in range(2, n + 1):
        out.append(name[:size])       # 前缀
        if size < n:
            out.append(name[n - size:])  # 后缀（size == n 时与前缀重复）
    return out


def _load_alias_index() -> dict:
    """{片段: [row, ...]}，片段为公司名的前后缀。构建一次，缓存 12h。"""
    from ..cache import slow_cache

    cached = slow_cache.get("a_alias_index")
    if cached is not None:
        return cached
    alias: dict[str, list] = {}
    for r in _load_code_name_list():
        name = _compact_text(str(r.get("name", "")))
        if not name:
            continue
        for tok in _name_affixes(name):
            alias.setdefault(tok, []).append(r)
    slow_cache.set("a_alias_index", alias, ttl=12 * 3600)
    return alias




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
