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


def _rows(level_name: str, fn_name: str, retries: int = 2) -> list:
    """取单个层级的全部行业行。整表按层级缓存——同一次研究里多次搜索共享。

    带重试：新浪的申万接口不稳，单次失败率不低。失败时 akshare 抛的是
    `'NoneType' object has no attribute 'find_all'`——看着像"页面结构变了"的永久错误，
    实际是请求拿回了非预期内容（限流页/空响应）让 BeautifulSoup 找不到表，**重试能自愈**。
    实测把这类错误当永久失败跳过重试，三个层级会从 1/3 可用直接掉到 0/3。

    数据日频更新，一次拉稳、缓存 30 分钟远比每次赌运气值——不重试的话行业命中数会飘忽
    （同一查询时而 5 个时而 1 个），甚至误报"无匹配行业"。
    """
    cache_key = f"sw_industry:{fn_name}"
    cached = slow_cache.get(cache_key)
    if cached is not None:
        return cached

    import time as _time

    import akshare as ak

    last_err: Optional[Exception] = None
    df = None
    for attempt in range(retries + 1):
        try:
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
            "code": str(r.get("行业代码", "") or ""),
            "name": str(r.get("行业名称", "") or ""),
            "parent": str(r.get("上级行业", "") or ""),
            "constituents": int(r.get("成份个数") or 0),
            "pe_static": _f(r.get("静态市盈率")),
            "pe_ttm": _f(r.get("TTM(滚动)市盈率")),
            "pb": _f(r.get("市净率")),
            "dividend_yield": _f(r.get("静态股息率")),
        })
    # 行业估值日频更新，缓存久一点（slow_cache 的 TTL 由调用方给）
    slow_cache.set(cache_key, out, ttl=1800)
    return out


def _f(v) -> Optional[float]:
    try:
        f = float(v)
        return None if f != f else f  # NaN → None，不要把 nan 塞进 JSON
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
