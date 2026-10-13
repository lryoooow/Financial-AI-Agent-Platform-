"""基本面：公司概况 + 财务指标。

akshare：
- stock_individual_info_em：公司概况（市值/行业/上市日期等）。
- stock_financial_analysis_indicator：财务分析指标（ROE/毛利率/资产负债率等）。
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Optional

from ..cache import slow_cache
from . import make_provenance
from .entity import is_sh, normalize_ticker, to_market_ticker

logger = logging.getLogger(__name__)


def _company_profile_direct(raw: str) -> dict:
    """直接解析东方财富 data 字典，绕过 AkShare 对返回列数的固定假设。"""
    import requests

    from .market_provider import configure_market_network

    configure_market_network()
    market = 1 if is_sh(raw) else 0
    fields = "f57,f58,f84,f85,f127,f116,f117,f189"
    resp = requests.get(
        "https://push2.eastmoney.com/api/qt/stock/get",
        params={"fltt": "2", "invt": "2", "fields": fields, "secid": f"{market}.{raw}"},
        headers={"User-Agent": "Mozilla/5.0 JingYuan/1.0", "Referer": "https://quote.eastmoney.com/"},
        timeout=6,
    )
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    return {
        "股票代码": data.get("f57"),
        "股票简称": data.get("f58"),
        "行业": data.get("f127"),
        "总市值": data.get("f116"),
        "流通市值": data.get("f117"),
        "上市时间": data.get("f189"),
    }


def get_company_profile(code: str) -> dict:
    """公司概况。"""
    raw = normalize_ticker(code)
    try:
        # 直连优先（带 UA）：akshare 的 push2 包装裸 requests 无 UA，实测被远端掐断，
        # 走它只会白挨一次连接失败再回退。
        source = "eastmoney-direct"
        endpoint = "stock_profile_direct"
        rec = _company_profile_direct(raw)
            # 这里原本还有第三级兜底 ak.stock_profile_cninfo（巨潮资讯），已移除——它会把整个
            # 进程打死。
            #
            # 根因：stock_profile_cninfo 需要跑 JS 解混淆，走 py_mini_racer(V8)，而 V8 在
            # Python 3.14 + Windows 下会抛原生致命断言
            #   [FATAL:partition_address_space.cc] Check failed: !IsConfigurablePoolInitialized()
            # 这不是 Python 异常，下面的 except 接不住，进程直接死。实测主线程、worker 线程、
            # 加锁串行化、主线程预热全都救不回来。
            #
            # 平时不响是因为前两级（东财 em / 东财直连）通着，走不到这一级；一旦东财抽风，
            # 这条路就是必死。研究管线与对话链路都会命中。
            #
            # 移除的代价极小：巨潮只给 简称/行业/上市日期，总市值还是空的；而下面第 94 行的
            # 行情兜底给的是 简称/行业/**真实总市值**，比它还全，只少一个上市时间。
            # 用一个上市时间换"进程不会被打死"，非常划算。
        name = rec.get("股票简称", "")
        industry = rec.get("行业", "")
        market_cap = rec.get("总市值", "")
        return {
            "code": to_market_ticker(raw),
            "name": name,
            "industry": industry,
            "market_cap": market_cap,
            "list_date": rec.get("上市时间", ""),
            "extra": rec,
            "provenance": make_provenance(
                source=source, endpoint=endpoint, raw_value=raw
            ),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("公司资料主源失败，尝试行情名称兜底：%s", e)
        try:
            from .market import get_realtime_quote

            quote = get_realtime_quote(code)
            if quote.get("name"):
                return {
                    "code": to_market_ticker(raw),
                    "name": quote.get("name", ""),
                    "industry": quote.get("industry", ""),
                    "market_cap": quote.get("marketCap", ""),
                    "list_date": "",
                    "extra": {},
                    "provenance": make_provenance(
                        source=str((quote.get("provenance") or {}).get("source") or "quote-fallback"),
                        endpoint="realtime_quote_profile_fallback",
                        raw_value=raw,
                    ),
                }
        except Exception as fallback_error:  # noqa: BLE001
            logger.warning("公司资料行情兜底失败：%s", fallback_error)
        return {
            "code": to_market_ticker(raw),
            "name": "",
            "industry": "",
            "market_cap": "",
            "list_date": "",
            "extra": {},
            "provenance": make_provenance(
                source="akshare", endpoint="stock_individual_info_em", error=str(e)
            ),
        }


def _fetch_indicator_rows(raw: str, n: int) -> Optional[list]:
    """analysis_indicator 源：原生同比列（主营业务收入增长率等）、扣非净利。无自身缓存。"""
    import akshare as ak

    start_year = str(dt.date.today().year - max(1, int(n)))
    df = ak.stock_financial_analysis_indicator(symbol=raw, start_year=start_year)
    if df is None or df.empty:
        return None
    # 第一列通常是日期
    df = df.copy()
    date_col = df.columns[0]
    df["_date"] = df[date_col].astype(str)
    df["_year"] = df["_date"].str[:4]
    rows = df.to_dict(orient="records")
    return rows


def _fetch_abstract_rows(raw: str) -> Optional[list[dict]]:
    """新浪财务摘要源（stock_financial_abstract）：营收绝对值/归母净利润/毛利率/ROE。

    analysis_indicator 接口没有营收绝对值（只有增长率），茅台等的"销售毛利率(%)"
    恒 nan——摘要源补齐这两个关键口径。返回按报告期倒序的 [{_date,_year,指标:值}]。
    """
    import akshare as ak

    df = ak.stock_financial_abstract(symbol=raw)
    if df is None or df.empty or "指标" not in df.columns:
        return None
    period_cols = [
        c for c in df.columns
        if c not in ("选项", "指标") and len(str(c)) == 8 and str(c).isdigit()
    ]
    if not period_cols:
        return None
    table = {
        str(r["指标"]).strip(): {c: r[c] for c in period_cols}
        for _, r in df.iterrows()
        if str(r.get("指标") or "").strip()
    }
    rows = []
    for col in period_cols:  # 报告期列本身即倒序（最新在前）
        row = {
            "_date": f"{str(col)[:4]}-{str(col)[4:6]}-{str(col)[6:8]}",
            "_year": str(col)[:4],
        }
        for key, values in table.items():
            row[key] = values.get(col)
        rows.append(row)
    return rows


def _last_n_years_indicator(raw: str, n: int = 6) -> Optional[list]:
    """取最近 n 年财务指标（每年最新报告期，双源融合，带缓存）。

    主源：新浪财务摘要（营收绝对值/归母净利润/毛利率/ROE/资产负债率）；
    增强：analysis_indicator（原生同比列、扣非净利），按年对齐、摘要键优先。
    任一源失败退回另一源；同年保留最新报告期，不能拿一季报冒充全年口径。
    """
    cache_key = f"fin_indicator:{raw}:{n}"
    cached = slow_cache.get(cache_key)
    if cached is not None:
        return cached
    abstract = _fetch_abstract_rows(raw)
    indicator = _fetch_indicator_rows(raw, n)
    if abstract is None and indicator is None:
        return None
    if abstract is not None and indicator is not None:
        ind_by_year = {str(r.get("_year")): r for r in indicator}
        merged = []
        for a in abstract:
            row = {**ind_by_year.get(a["_year"], {}), **a}  # 摘要键优先（口径更全）
            row["_date"], row["_year"] = a["_date"], a["_year"]
            merged.append(row)
        rows = merged
    else:
        rows = abstract if abstract is not None else _dedupe_latest_per_year(indicator)
    rows = _dedupe_latest_per_year(rows)
    rows = sorted(rows, key=lambda r: str(r.get("_year")))
    slow_cache.set(cache_key, rows, ttl=6 * 3600)
    return rows


def _dedupe_latest_per_year(rows: list) -> list:
    """同年保留最新报告期（按 _date 显式比较，不依赖输入顺序）。"""
    per_year: dict = {}
    for r in rows or []:
        year = str(r.get("_year") or "")
        date = str(r.get("_date") or "")
        if not year:
            continue
        if year not in per_year or date > str(per_year[year].get("_date") or ""):
            per_year[year] = r
    return list(per_year.values())


def get_financial_indicators(code: str) -> dict:
    """财务指标（前端 financials 形状：years + rows[name, values[]]）。

    拉最近 5 年：营收/净利润/ROE/毛利率/资产负债率。
    拿不到的字段留 "—"，绝不让 LLM 编。
    """
    raw = normalize_ticker(code)
    try:
        rows = _last_n_years_indicator(raw, n=6) or []
        if not rows:
            raise ValueError("无财务指标数据")
        rows = rows[-5:]
        years = [str(r.get("_year", "")) for r in rows]

        def pick(r, *keys):
            for k in keys:
                if k in r and r[k] not in (None, "", "nan"):
                    try:
                        value = float(r[k])
                        return value if math.isfinite(value) else None
                    except Exception:  # noqa: BLE001
                        return r[k]
            return None

        def fmt_pct(v):
            return f"{float(v):.1f}%" if isinstance(v, (int, float)) and math.isfinite(float(v)) else "—"

        def fmt_num(v, unit="亿"):
            if isinstance(v, (int, float)) and math.isfinite(float(v)):
                # 序列内单位必须统一（facts.to_float/yoy_pct 会跨年相除）：
                # 曾经小额年份输出裸元、大额年份输出亿，混排后同比算出天文数字
                scaled = v / 1e8
                digits = 4 if 0 < abs(scaled) < 0.01 else 2
                return f"{scaled:.{digits}f}{unit}"
            return "—"

        # pick 键双源：摘要源（营业总收入/归母净利润/毛利率/净资产收益率(ROE)/资产负债率）
        # 在前，analysis_indicator 源（主营业务收入(元)/销售毛利率(%) 等）兜底
        revenue_vals = [fmt_num(pick(r, "营业总收入", "营业收入", "主营业务收入(元)")) for r in rows]
        net_vals = [fmt_num(pick(r, "归母净利润", "净利润", "扣非净利润", "扣除非经常性损益后的净利润(元)")) for r in rows]
        rev_yoy_vals = [fmt_pct(pick(r, "主营业务收入增长率(%)", "营业收入同比增长率(%)")) for r in rows]
        net_yoy_vals = [fmt_pct(pick(r, "净利润增长率(%)", "归母净利润同比增长率(%)")) for r in rows]
        roe_vals = [fmt_pct(pick(r, "净资产收益率(ROE)", "净资产收益率(%)", "加权净资产收益率(%)", "净资产收益率", "ROE")) for r in rows]
        gm_vals = [fmt_pct(pick(r, "毛利率", "销售毛利率(%)", "销售毛利率")) for r in rows]
        debt_vals = [fmt_pct(pick(r, "资产负债率", "资产负债率(%)")) for r in rows]

        return {
            "financials": {
                "years": years,
                # 报告期全日期（YYYY-MM-DD）：披露口径用。只看年份分不清 2026 是年报还是中报，
                # 消费方据此决定同比是否可算（中报 vs 上年年报直接相除是 -50% 式系统性错误）
                "periods": [str(r.get("_date", ""))[:10] for r in rows],
                "rows": [
                    {"name": "营业收入", "values": revenue_vals},
                    {"name": "净利润", "values": net_vals},
                    {"name": "营收同比", "values": rev_yoy_vals},
                    {"name": "净利润同比", "values": net_yoy_vals},
                    {"name": "ROE", "values": roe_vals},
                    {"name": "毛利率", "values": gm_vals},
                    {"name": "资产负债率", "values": debt_vals},
                ],
            },
            "provenance": make_provenance(
                source="akshare",
                endpoint="stock_financial_analysis_indicator",
                raw_value=raw,
            ),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("get_financial_indicators 失败：%s", e)
        return {
            "financials": {"years": [], "rows": []},
            "provenance": make_provenance(
                source="akshare",
                endpoint="stock_financial_analysis_indicator",
                error=str(e),
            ),
        }
