"""行业维度数据（申万一级/二级/三级行业估值）。

为什么是申万而不是东方财富的行业板块：东财的行业接口（stock_board_industry_name_em /
stock_board_industry_cons_em）实测在本机网络下必失败（RemoteDisconnected，与
push2.eastmoney 全市场表同一个症状）；申万三个层级的接口走新浪，实测全通
（一级 31 行 / 二级 131 行 / 三级 336 行，6s 上下）。

层级选择很重要：一级太粗（31 个，"光伏"只能归到"电力设备"），二级才有"光伏设备"、
"半导体"、"白酒Ⅱ"这种可用粒度，三级更细（"光伏发电"、"半导体材料"）。
所以查询时三级全搜，把匹配到的都返回，由调用方/模型挑。

字段里带静态市盈率/TTM市盈率/市净率/静态股息率——这是本项目目前唯一拿得到的估值数据
（个股级的 PE/PB 只有东财提供，而东财不可达）。
"""

from __future__ import annotations

import logging
import math
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from . import make_provenance
from ..cache import slow_cache

logger = logging.getLogger(__name__)

_LEVELS = (
    ("申万一级", "sw_index_first_info"),
    ("申万二级", "sw_index_second_info"),
    ("申万三级", "sw_index_third_info"),
)
_LEGU_DIV_MAP = {
    "sw_index_first_info": ("申万一级", "level1Items"),
    "sw_index_second_info": ("申万二级", "level2Items"),
    "sw_index_third_info": ("申万三级", "level3Items"),
}
_LEGU_LOCK = threading.Lock()
_LEGU_URL = "https://legulegu.com/stockdata/sw-industry-overview"


def _parse_legu_div(soup, div_id: str, level_name: str) -> list[dict]:
    container = soup.find(name="div", attrs={"id": div_id})
    if container is None:
        return []
    code_raw = container.find_all(name="div", attrs={"class": "lg-industries-item-chinese-title"})
    name_raw = container.find_all(name="div", attrs={"class": "lg-industries-item-number"})
    value_raw = container.find_all(name="div", attrs={"class": "lg-sw-industries-item-value"})
    n = min(len(code_raw), len(name_raw), len(value_raw))
    out: list[dict] = []
    for idx in range(n):
        c_item = code_raw[idx]
        n_item = name_raw[idx]
        v_item = value_raw[idx]
        code = _s(c_item.get_text())
        n_text = _s(n_item.get_text())
        name = n_text.split("(", 1)[0].strip() if "(" in n_text else n_text
        num_str = n_text.split("(", 1)[1].split(")", 1)[0] if "(" in n_text and ")" in n_text else "0"
        parent = ""
        span = n_item.find("span")
        if span is not None:
            sp_txt = _s(span.get_text()).strip("[]（）() ")
            parent = sp_txt.split("(", 1)[0].split("（", 1)[0].strip("[]（）() ")
        vals = [s.get_text().strip() for s in v_item.find_all("span", attrs={"class": "value"})]
        out.append({
            "level": level_name,
            "code": code,
            "name": name,
            "parent": parent,
            "constituents": _i(num_str),
            "pe_static": _f(vals[0] if len(vals) > 0 else None),
            "pe_ttm": _f(vals[1] if len(vals) > 1 else None),
            "pb": _f(vals[2] if len(vals) > 2 else None),
            "dividend_yield": _f(vals[3] if len(vals) > 3 else None),
        })
    return out


def _populate_all_levels_from_legu(retries: int = 2) -> bool:
    """单次请求拉取乐咕乐股申万一级/二级/三级全表并同时写入缓存。

    根因修复：ak.sw_index_first_info / second_info / third_info 底层全部抓取同一个 1.16MB HTML 页面
    (https://legulegu.com/stockdata/sw-industry-overview)，分别解析 #level1Items / #level2Items /
    #level3Items。旧实现用 3 线程同时打同一 URL 3 次，极易触发对端限流/截断返回，导致后半页的
    申万二级/三级报 `'NoneType' object has no attribute 'find_all'`。
    改为加锁使用 requests（自动启用 gzip 压缩传输，~0.9s）单次拉取同页解析三层，彻底消除并发自撞与截断。
    """
    import requests
    from bs4 import BeautifulSoup
    from .market_provider import configure_market_network

    with _LEGU_LOCK:
        if all(slow_cache.get(f"sw_industry:{fn}") is not None for _, fn in _LEVELS):
            return True
        configure_market_network()
        html_text = ""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
            ),
        }
        for attempt in range(retries + 1):
            try:
                resp = requests.get(_LEGU_URL, headers=headers, timeout=8)
                resp.raise_for_status()
                html_text = resp.text
                if "level1Items" in html_text and "level2Items" in html_text and "level3Items" in html_text:
                    break
            except Exception as e:  # noqa: BLE001
                logger.debug("申万行业全表直连尝试 %d 失败：%s", attempt + 1, e)
                if attempt < retries:
                    _time.sleep(0.5)
        if not html_text:
            return False
        soup = BeautifulSoup(html_text, features="lxml")
        ok_count = 0
        for fn_name, (lv_name, div_id) in _LEGU_DIV_MAP.items():
            parsed = _parse_legu_div(soup, div_id, lv_name)
            if parsed:
                slow_cache.set(f"sw_industry:{fn_name}", parsed, ttl=1800)
                ok_count += 1
        return ok_count == len(_LEVELS)


def _rows(level_name: str, fn_name: str, retries: int = 2) -> list:
    """取单个层级的全部行业行。整表按层级缓存——同一次研究里多次搜索共享。"""
    cache_key = f"sw_industry:{fn_name}"
    cached = slow_cache.get(cache_key)
    if cached is not None:
        return cached

    import akshare as ak

    if getattr(ak, "__file__", None):
        try:
            _populate_all_levels_from_legu(retries=retries)
            cached = slow_cache.get(cache_key)
            if cached is not None:
                return cached
        except Exception as e:  # noqa: BLE001
            logger.debug("申万单页三层同解失败，回退 AkShare：%s", e)

    last_err: Optional[Exception] = None
    df = None
    for attempt in range(retries + 1):
        try:
            with _LEGU_LOCK:
                cached = slow_cache.get(cache_key)
                if cached is not None:
                    return cached
                df = getattr(ak, fn_name)()
            if df is not None and not df.empty:
                break
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                _time.sleep(0.6)
    if df is None or df.empty:
        raise ValueError(f"{level_name} 未返回数据" + (f"：{last_err}" if last_err else ""))
    out = []
    for _, r in df.iterrows():
        out.append({
            "level": level_name,
            "code": _s(r.get("行业代码")),
            "name": _s(r.get("行业名称")),
            "parent": _s(r.get("上级行业")),
            "constituents": _i(r.get("成份个数")),
            "pe_static": _f(r.get("静态市盈率")),
            "pe_ttm": _f(r.get("TTM(滚动)市盈率")),
            "pb": _f(r.get("市净率")),
            "dividend_yield": _f(r.get("静态股息率")),
        })
    slow_cache.set(cache_key, out, ttl=1800)
    return out


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and not math.isfinite(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "<na>") else s


def _i(v) -> int:
    f = _f(v)
    return int(f) if f is not None else 0


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None  # NaN / Inf → None，不要塞进 JSON
    except (TypeError, ValueError):
        return None


def search_industry(name: str, limit: int = 8) -> dict:
    """按名称搜行业（申万一级/二级/三级全搜），返回匹配行业及其估值。

    子串匹配：搜"光伏"能同时命中二级"光伏设备"与三级"光伏发电"，由调用方挑。
    """
    q = (name or "").strip()
    if not q:
        return {
            "items": [],
            "provenance": make_provenance(source="sina", endpoint="sw_index_info", error="空查询"),
        }

    # 三个层级并行拉：它们互不依赖，串行要 11s+（各约 4s），并行 ~4s。
    # 别指望调大超时了事——那是财务指标那个 bug 的思路，治标不治本。
    hits, errors = [], []
    with ThreadPoolExecutor(max_workers=len(_LEVELS)) as pool:
        futures = {pool.submit(_rows, ln, fn): ln for ln, fn in _LEVELS}
        for fut in as_completed(futures):
            level_name = futures[fut]
            try:
                for row in fut.result():
                    if q in row["name"]:
                        hits.append(row)
            except Exception as e:  # noqa: BLE001
                logger.info("%s 取数失败：%s", level_name, e)
                errors.append(f"{level_name}: {e}")

    # 粒度细的排前面（三级 > 二级 > 一级）：搜"光伏"时"光伏设备"比"电力设备"更有用
    order = {"申万三级": 0, "申万二级": 1, "申万一级": 2}
    hits.sort(key=lambda h: (order.get(h["level"], 9), -(h["constituents"] or 0)))

    # 部分层级失败必须如实上报，不能静默。实测三个层级各自会偶发失败（新浪不稳），
    # 此时搜"光伏"可能只剩 1 个命中而不是 5 个——若不标注，调用方与模型会把残缺结果
    # 当成完整结果，以为该行业就这么点数据。这与 deep/tools.py:_normalize 要防的是同一类事：
    # 残缺数据伪装成完整数据比直接失败更危险。
    err = None
    if errors:
        missing = "、".join(e.split(":")[0] for e in errors)
        err = f"部分层级取数失败（{missing}），结果可能不完整：{'; '.join(errors)[:160]}"

    return {
        "items": hits[: max(1, int(limit))],
        "levels_ok": len(_LEVELS) - len(errors),
        "levels_total": len(_LEVELS),
        "provenance": make_provenance(
            source="sina", endpoint="sw_index_info", raw_value=q, error=err
        ),
    }


def list_industries(level: str = "申万一级") -> dict:
    """列出某层级的全部行业（用于"有哪些行业"这类问题）。"""
    match = next((x for x in _LEVELS if x[0] == level), None)
    if match is None:
        return {
            "items": [],
            "provenance": make_provenance(
                source="sina", endpoint="sw_index_info", raw_value=level,
                error=f"未知层级 {level}，可选：{'/'.join(x[0] for x in _LEVELS)}",
            ),
        }
    try:
        return {
            "items": _rows(*match),
            "provenance": make_provenance(source="sina", endpoint=match[1], raw_value=level),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "items": [],
            "provenance": make_provenance(
                source="sina", endpoint=match[1], raw_value=level, error=str(e)
            ),
        }
