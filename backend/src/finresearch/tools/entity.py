"""实体解析：模糊匹配 A 股标的。

搜索路径必须快：这里只加载全市场代码/名称清单，不在搜索阶段拉全市场实时快照。
实时价格和涨跌幅由用户选中股票后的 quote 接口获取。
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from functools import lru_cache
from importlib import resources
from typing import Optional

from rapidfuzz import process, fuzz

from . import make_provenance, now_iso

logger = logging.getLogger(__name__)
_SEED_RESOURCE = "data/a_code_name_seed.json"

# 交易所后缀约定（前端用 SH/SZ/BJ）
_EXCHANGE_MAP = {
    "sh": "上证",
    "sz": "深证",
    "bj": "北证",
}

# 上证：主板 60 / 科创板 68 / B股 90 / 转债 11 / 基金与科创50ETF 50,51,52,56,58
SH_PREFIXES = ("60", "68", "90", "11", "50", "51", "52", "56", "58")
SZ_PREFIXES = ("00", "30", "20", "12", "15", "16")
BJ_PREFIXES = ("43", "83", "87", "88", "92")

# 常见宽基/市场指数表：用于拦截把指数代码（如 000001.SH 上证指数 / 399006 创业板指）
# 静默改写成同名号段个股（000001.SZ 平安银行）的张冠李戴问题。
_SH_INDEX_NAMES = {
    "000001": "上证指数",
    "000002": "上证A指",
    "000003": "上证B指",
    "000010": "上证180",
    "000016": "上证50",
    "000300": "沪深300",
    "000688": "科创50",
    "000852": "中证1000",
    "000905": "中证500",
    "000985": "中证全指",
}
_SZ_INDEX_NAMES = {
    "399001": "深证成指",
    "399005": "中小100",
    "399006": "创业板指",
    "399102": "创业板综",
    "399300": "沪深300",
    "399905": "中证500",
}
_BJ_INDEX_NAMES = {
    "899050": "北证50",
}

# GB2312 一级汉字拼音首字母分段点（按拼音严格升序排列）
_GB2312_PINYIN_ANCHORS = [
    (int.from_bytes(ch.encode("gb2312"), "big"), letter)
    for letter, ch in (
        ("A", "啊"), ("B", "芭"), ("C", "擦"), ("D", "搭"), ("E", "蛾"),
        ("F", "发"), ("G", "噶"), ("H", "哈"), ("J", "击"), ("K", "喀"),
        ("L", "垃"), ("M", "妈"), ("N", "拿"), ("O", "哦"), ("P", "啪"),
        ("Q", "期"), ("R", "然"), ("S", "撒"), ("T", "塌"), ("W", "挖"),
        ("X", "昔"), ("Y", "压"), ("Z", "匝"),
    )
]
_GB2312_PINYIN_END = 0xD7F9

# A 股简称常见多音字 / 二级生僻字首字母修正
_CHAR_PINYIN_OVERRIDES = {
    "重": "C", "长": "C", "厦": "X", "藏": "Z", "乐": "L", "奇": "Q",
    "泸": "L", "汾": "F", "兖": "Y", "赣": "G", "锂": "L", "钴": "G",
    "钼": "M", "钛": "T", "锆": "Z", "钽": "T", "铌": "N", "铍": "P",
    "鑫": "X", "淼": "M", "焱": "Y", "垚": "Y", "睿": "R", "昊": "H",
    "昱": "Y", "晖": "H", "晟": "S", "宸": "C", "瀚": "H", "澜": "L",
    "祺": "Q", "祯": "Z", "麒": "Q", "麟": "L", "鹭": "L", "鲲": "K",
    "珀": "P", "莱": "L", "薇": "W", "诺": "N", "迈": "M", "瑞": "R",
    "恒": "H", "药": "Y", "茅": "M", "粮": "L", "液": "Y", "比": "B",
    "亚": "Y", "迪": "D", "格": "G", "美": "M", "招": "Z", "商": "S",
}


def _pinyin_initials(text: str) -> str:
    """零外部依赖生成中文简称拼音首字母（如「贵州茅台」→「GZMT」、「平安银行」→「PAYH」）。"""
    norm = unicodedata.normalize("NFKC", str(text or "")).upper()
    norm = norm.replace("银行", "YH").replace("重庆", "CQ").replace("长春", "CC").replace("厦门", "XM")
    out: list[str] = []
    for ch in norm:
        if ch.isascii():
            if ch.isalnum():
                out.append(ch)
            continue
        if ch in _CHAR_PINYIN_OVERRIDES:
            out.append(_CHAR_PINYIN_OVERRIDES[ch])
            continue
        try:
            raw = ch.encode("gb2312")
        except UnicodeEncodeError:
            continue
        if len(raw) != 2:
            continue
        val = int.from_bytes(raw, "big")
        if val < _GB2312_PINYIN_ANCHORS[0][0] or val > _GB2312_PINYIN_END:
            continue
        picked = "A"
        for threshold, letter in _GB2312_PINYIN_ANCHORS:
            if val >= threshold:
                picked = letter
            else:
                break
        out.append(picked)
    return "".join(out)


def extract_exchange_hint(code: str) -> str:
    """提取调用方显式给出的交易所标识（SH/SZ/BJ），无显式标识返回空串。"""
    if not code:
        return ""
    c = unicodedata.normalize("NFKC", str(code)).strip().upper()
    for sep in (".", ":"):
        if sep in c:
            left, right = c.split(sep, 1)
            if right in ("SH", "SZ", "BJ"):
                return right
            if left in ("SH", "SZ", "BJ"):
                return left
    for prefix in ("SH", "SZ", "BJ"):
        if c.startswith(prefix) and c[len(prefix):].isdigit():
            return prefix
    return ""


def detect_index_code(code: str) -> Optional[tuple[str, str]]:
    """识别常见市场/宽基指数代码，返回 (标准指数代码, 指数名称)；非指数返回 None。"""
    if not code:
        return None
    hint = extract_exchange_hint(code)
    raw = normalize_ticker(code)
    if not (raw.isdigit() and len(raw) == 6):
        return None
    if hint == "SH" and raw.startswith("000"):
        return f"{raw}.SH", _SH_INDEX_NAMES.get(raw, "上证系列指数")
    if raw.startswith("399") and hint in ("", "SZ"):
        return f"{raw}.SZ", _SZ_INDEX_NAMES.get(raw, "深证系列指数")
    if raw.startswith("899") and hint in ("", "BJ"):
        return f"{raw}.BJ", _BJ_INDEX_NAMES.get(raw, "北证系列指数")
    return None


def _classify_exchange(code: str) -> str:
    """按代码前缀（或显式后缀）判断交易所。"""
    hint = extract_exchange_hint(code)
    if hint.lower() in _EXCHANGE_MAP:
        return _EXCHANGE_MAP[hint.lower()]
    c = normalize_ticker(code)
    if c.startswith(SH_PREFIXES):
        return _EXCHANGE_MAP["sh"]
    if c.startswith(SZ_PREFIXES):
        return _EXCHANGE_MAP["sz"]
    if c.startswith(BJ_PREFIXES):
        return _EXCHANGE_MAP["bj"]
    return "A股"


def normalize_ticker(code: str) -> str:
    """归一化前端传入的 ticker 到纯 6 位代码。

    前端用 "600519.SH"，akshare 大多数接口要纯代码 "600519"。
    v1.6.0（审计 D8）：去掉 zfill 静默补齐，归一化只做剥离，不做猜测。
    """
    if not code:
        return ""
    c = unicodedata.normalize("NFKC", str(code)).strip().upper()
    for sep in (".", ":"):
        if sep in c:
            left, right = c.split(sep, 1)
            c = right if left in ("SH", "SZ", "BJ") and right.isdigit() else left
            break
    for prefix in ("SH", "SZ", "BJ"):
        if c.startswith(prefix):
            c = c[len(prefix):]
            break
    return c.strip()


def to_market_ticker(code: str) -> str:
    """纯代码（或带后缀代码）→ 前端格式（600519.SH）。保留显式合法后缀（如 000001.SH）。"""
    c = normalize_ticker(code)
    if not c:
        return ""
    hint = extract_exchange_hint(code)
    if hint in ("SH", "SZ", "BJ"):
        return f"{c}.{hint}"
    if c.startswith(SH_PREFIXES):
        return f"{c}.SH"
    if c.startswith(BJ_PREFIXES):
        return f"{c}.BJ"
    return f"{c}.SZ"


def stock_ticker(code: str) -> str:
    """Validate an equity/ETF identifier before passing it to a stock adapter."""
    raw = normalize_ticker(code)
    if len(raw) != 6 or not raw.isascii() or not raw.isdigit():
        raise ValueError("请输入有效的 A 股或 ETF 代码。")
    if detect_non_ashare_code(code) or detect_index_code(code):
        raise ValueError("此行情接口支持 A 股和 ETF，请通过指数接口查询宽基指数。")
    expected = to_market_ticker(raw)
    if extract_exchange_hint(code) and to_market_ticker(code) != expected:
        raise ValueError("股票代码与交易所后缀不匹配。")
    return expected


def _compact_text(value: str) -> str:
    norm = unicodedata.normalize("NFKC", str(value or "")).upper()
    return "".join(norm.split())


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


@lru_cache(maxsize=1)
def _seed_name_map() -> dict[str, str]:
    return {str(r["code"]).zfill(6): str(r["name"]) for r in _load_code_name_seed()}


def lookup_stock_name(code: str) -> str:
    """零网络、O(1) 本地种子表反查股票简称；指数或非 6 位代码返回空串。"""
    if not code or detect_index_code(code) is not None:
        return ""
    raw = normalize_ticker(code)
    if not (raw.isdigit() and len(raw) == 6):
        return ""
    return _seed_name_map().get(raw, "")


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
    ttl = 12 * 3600 if rows else 30
    slow_cache.set("a_code_name", rows, ttl=ttl)
    slow_cache.set("a_code_name_source", source, ttl=ttl)
    slow_cache.set("a_code_name_updated_at", time.time(), ttl=ttl)
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
    initials_list = [_pinyin_initials(r["name"]) for r in rows]
    candidates = [
        f"{str(r['code']).zfill(6)} {r['name']} {_compact_text(r['name'])} {ini}"
        for r, ini in zip(rows, initials_list)
    ]
    index = {"rows": rows, "code_map": code_map, "candidates": candidates, "initials": initials_list}
    slow_cache.set("a_code_name_index", index, ttl=(12 * 3600 if rows else 30))
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


_NON_ASHARE_SUFFIX_RE = re.compile(
    r"(?:^|[.:])(HK|US|OQ|NYSE|NASDAQ|N|O|AMEX|LSE|TSE|TW)$",
    re.IGNORECASE,
)


def detect_non_ashare_code(code: str) -> Optional[str]:
    """识别显式带有港股/美股等非 A 股市场后缀的代码（如 00700.HK / AAPL.US / BABA.N）。"""
    if not code:
        return None
    c = unicodedata.normalize("NFKC", str(code)).strip().upper()
    if "." in c or ":" in c:
        sep = "." if "." in c else ":"
        left, right = c.split(sep, 1)
        if left and right and _NON_ASHARE_SUFFIX_RE.match(right):
            return right
        if left and right and _NON_ASHARE_SUFFIX_RE.match(left) and right.isalnum():
            return left
    if c.startswith("HK") and len(c) in (6, 7) and c[2:].isdigit():
        return "HK"
    return None


def resolve_entity(query: str, limit: int = 8) -> dict:
    q = unicodedata.normalize("NFKC", str(query or "")).strip()
    if not q:
        return {"results": [], "provenance": make_provenance(source="local", endpoint="a_code_name_seed")}

    non_a_market = detect_non_ashare_code(q)
    if non_a_market is not None:
        return {
            "results": [],
            "provenance": make_provenance(
                source="local",
                endpoint="market-guard",
                raw_value=f"non_ashare={q}",
                error=f"暂仅支持 A 股标的（SH/SZ/BJ），不支持非 A 股市场代码（{q}）",
            ),
        }

    idx_hit = detect_index_code(q)
    if idx_hit is not None:
        idx_code, idx_name = idx_hit
        return {
            "results": [],
            "provenance": make_provenance(
                source="local",
                endpoint="index-guard",
                raw_value=f"index={idx_code}",
                error=f"{idx_name}（{idx_code}）为市场指数而非个股标的",
            ),
        }

    exact_code = normalize_ticker(q)
    exact_hint = extract_exchange_hint(q)
    if exact_code.isdigit() and len(exact_code) == 6:
        try:
            idx = _load_search_index()
            r = idx["code_map"].get(exact_code)
            if r:
                hit = _hit_row(r)
                # 若调用方显式指定了不匹配的交易所后缀（如 600519.SZ），不静默篡改也不张冠李戴
                expected_suffix = hit["code"].split(".")[-1]
                if exact_hint and exact_hint != expected_suffix:
                    return {
                        "results": [],
                        "provenance": make_provenance(
                            source="local",
                            endpoint="a_code_name_seed",
                            raw_value=f"mismatch={q}",
                            error=f"代码 {exact_code} 归属 {hit['code']}（{hit['name']}），与后缀 .{exact_hint} 不一致",
                        ),
                    }
                return {
                    "results": [hit],
                    "provenance": make_provenance(
                        source="local",
                        endpoint="a_code_name_seed",
                        raw_value=f"exact={exact_code}",
                    ),
                }
        except Exception as e:  # noqa: BLE001
            logger.warning("exact code lookup index failed: %s", e)

        return {
            "results": [],
            "provenance": make_provenance(
                source="local",
                endpoint="a_code_name_seed",
                raw_value=f"unmatched_code={exact_code}",
                error=f"未在 A 股代码清单中找到 {exact_code}",
            ),
        }

    try:
        idx = _load_search_index()
        rows = idx["rows"]
        candidates = idx["candidates"]
        initials = idx.get("initials") or [""] * len(rows)
        limit = max(1, int(limit or 8))
        q_compact = _compact_text(q)
        q_alpha_only = re.sub(r"[^A-Z0-9]", "", q_compact)
        is_pure_ascii = bool(q_compact) and q_compact.isascii()
        seen = set()
        picked = []

        for r, ini in zip(rows, initials):
            code = str(r["code"]).zfill(6)
            name_compact = _compact_text(str(r.get("name", "")))
            if q_compact and (
                q_compact in code
                or q_compact in name_compact
                or (len(q_alpha_only) >= 2 and ini and q_alpha_only == ini)
            ):
                seen.add(code)
                picked.append(_hit_row(r))
                if len(picked) >= limit:
                    break

        if len(picked) < limit and len(q_alpha_only) >= 2:
            for r, ini in zip(rows, initials):
                code = str(r["code"]).zfill(6)
                if code in seen:
                    continue
                if ini and ini.startswith(q_alpha_only):
                    seen.add(code)
                    picked.append(_hit_row(r))
                    if len(picked) >= limit:
                        break

        # 纯 ASCII 短串若非拼音首字母/数字子串（如 NVDA/AAPL），不走低阈值中文模糊匹配，防止误命中
        if len(picked) < limit and not (is_pure_ascii and not q_compact.isdigit() and len(q_alpha_only) >= 3):
            matches = process.extract(
                q_compact or q,
                candidates,
                scorer=fuzz.WRatio,
                limit=limit * 3,
                score_cutoff=70,
            )
            for _m_text, _score, match_idx in matches:
                r = rows[match_idx]
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
