"""基本面：公司概况 + 财务指标。

akshare：
- stock_individual_info_em：公司概况（市值/行业/上市日期等）。
- stock_financial_analysis_indicator：财务分析指标（ROE/毛利率/资产负债率等）。
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import time
from typing import Optional

from ..cache import slow_cache
from . import make_provenance, now_iso
from .entity import normalize_ticker, to_market_ticker

logger = logging.getLogger(__name__)

# 直连会话（连接复用 + 统一 UA/Referer）。旧实现每次裸 requests.get 新建 TCP+TLS，
# 且无 UA —— push2 对无 UA 请求更易直接断开（审计实测本网络 RemoteDisconnected）。
_DIRECT_SESSION = None
# 直连熔断：push2/qt 接口在部分网络持续不可达，不熔断则每次研究连烧 2 个必败请求。
_DIRECT_FAIL_AT = 0.0
_DIRECT_BREAKER_S = 300.0


def _direct_session():
    global _DIRECT_SESSION
    if _DIRECT_SESSION is None:
        import requests

        _DIRECT_SESSION = requests.Session()
        _DIRECT_SESSION.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        })
    return _DIRECT_SESSION


def _company_profile_direct(raw: str) -> dict:
    """直接解析东方财富 data 字典，绕过 AkShare 对返回列数的固定假设。

    v1.6.0：带 5 分钟失败熔断 —— 该端点在本机网络实测持续不可达时，
    直接抛给上层走行情名称兜底，不再每次研究连烧必败请求。
    """
    from .market_provider import configure_market_network

    global _DIRECT_FAIL_AT
    configure_market_network()
    if time.time() - _DIRECT_FAIL_AT < _DIRECT_BREAKER_S:
        raise ConnectionError("公司资料直连接口近 5 分钟内失败，熔断中（直接走行情名称兜底）")
    # 核验修复（B7 补齐第五处）：北交所 920xxx 的东财 secid 市场号是 0，旧判定会把
    # "9" 开头一律归沪市（1），920 走该兜底拿到空档案。
    market = 0 if raw.startswith(("43", "83", "87", "88", "92")) else (1 if raw.startswith(("6", "9")) else 0)
    fields = "f57,f58,f84,f85,f127,f116,f117,f189"
    try:
        resp = _direct_session().get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={"fltt": "2", "invt": "2", "fields": fields, "secid": f"{market}.{raw}"},
            timeout=6,
        )
        resp.raise_for_status()
    except Exception:
        _DIRECT_FAIL_AT = time.time()
        raise
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
        import akshare as ak
        source = "akshare"
        endpoint = "stock_individual_info_em"
        try:
            df = ak.stock_individual_info_em(symbol=raw)
            rec = {}
            if df is not None and not df.empty:
                for _, r in df.iterrows():
                    rec[str(r.iloc[0])] = r.iloc[1] if len(r) > 1 else ""
        except Exception as ak_error:  # AkShare 可能因上游新增字段触发 Length mismatch
            logger.info("公司资料 AkShare 解析失败，改用直连接口：%s", ak_error)
            rec = _company_profile_direct(raw)
            source = "eastmoney-direct"
            endpoint = "stock_profile_direct"
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


_PERIOD_LABELS = {"03": "一季报", "06": "中报", "09": "三季报"}


def _norm_period(value) -> str:
    """把各数据源的报告期统一成 YYYY-MM-DD（"20251231" / "2025-12-31" / Timestamp 均可）。

    无法识别时返回空串 —— 调用方必须把空串当「报告期不明」，绝不拿公告日期顶替。
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 8:
        return ""
    y, m, d = digits[:4], digits[4:6], digits[6:8]
    if not ("01" <= m <= "12" and "01" <= d <= "31"):
        return ""
    return f"{y}-{m}-{d}"


def period_label(period: str, year: str = "") -> str:
    """表头标签：年报 → "2025"；中期 → "2026中报" / "2026一季报" / "2026三季报"。"""
    p = _norm_period(period)
    if not p:
        return str(year or "")
    y, m = p[:4], p[5:7]
    return y if m == "12" else f"{y}{_PERIOD_LABELS.get(m, p[5:])}"


def _last_n_years_indicator(raw: str, n: int = 6) -> Optional[list]:
    """取最近 n 年财务指标（带缓存）。返回 list[dict]。

    v1.6.3（P1 修复）：同年**优先取年报**（报告期 12-31）。当年尚无年报时回落最新
    报告期，并在行上标记 `_interim=True`。
    v1.7.1（审计 A01）：中期行额外挂 `_prior_same_period` —— 上一年**同一报告期**
    的原始行（如 2026-06-30 ↔ 2025-06-30）。上一年那一行在表里已被年报替换，
    不挂出来的话，消费方只能拿「中报 vs 上年年报」相减，冒充同比。
    """
    cache_key = f"fin_indicator:{raw}:{n}"
    cached = slow_cache.get(cache_key)
    if cached is not None:
        return cached
    import akshare as ak

    start_year = str(dt.date.today().year - max(1, int(n)))
    df = ak.stock_financial_analysis_indicator(symbol=raw, start_year=start_year)
    if df is None or df.empty:
        return None
    # 第一列通常是日期（"日期"）；按名字优先，避免上游列序变化
    df = df.copy()
    date_col = next((c for c in df.columns if str(c).strip() in ("日期", "报告期", "报告日")), df.columns[0])
    df["_date"] = df[date_col].map(_norm_period)
    df = df[df["_date"] != ""]
    if df.empty:
        return None
    df["_year"] = df["_date"].str[:4]
    df = df.sort_values("_date")
    by_date = {str(r["_date"]): r for r in df.to_dict("records")}
    picked_rows = []
    for _year, group in df.groupby("_year", sort=True):
        annual = group[group["_date"].str.slice(5, 7) == "12"]
        if not annual.empty:
            rec = annual.iloc[-1].to_dict()
            rec["_interim"] = False
        else:
            rec = group.iloc[-1].to_dict()
            rec["_interim"] = True  # 当年尚无年报：中期报告口径，调用方须标注
            d = str(rec["_date"])
            prior = by_date.get(f"{int(d[:4]) - 1}{d[4:]}")
            if prior is not None:
                rec["_prior_same_period"] = dict(prior)
        picked_rows.append(rec)
    rows = picked_rows
    slow_cache.set(cache_key, rows, ttl=6 * 3600)
    return rows


def _income_statement_annual(raw: str, years: list) -> dict:
    """新浪利润表取营收绝对值与毛利率（v1.6.3 B2）。

    返回 {报告期 "YYYY-MM-DD": {"revenue": 元, "gross_margin_pct": float}}，
    每个报告期一条；任何失败返回空 dict（调用方保持现状如实「—」，绝不编造）。

    v1.7.1（审计 A02）：报告期只认「报告日/报告期/截止日期」列。旧实现找第一个名字含
    「日期」的列，命中的是「公告日期」—— 2024 年报在 2025-03 公告，于是被当成 2025
    年的数据补进 2025 列，营收/毛利率与同列 ROE、利润错一年。报告期列找不到时宁可
    不补（返回空），也不拿公告日期顶替。
    """
    cache_key = f"income_sina:{raw}"
    cached = slow_cache.get(cache_key)
    if cached is not None:
        return cached
    import akshare as ak

    from .market_provider import _sina_symbol

    sym = _sina_symbol(raw)  # sh600519 / sz000001 / bj920000
    try:
        df = ak.stock_financial_report_sina(stock=sym, symbol="利润表")
    except Exception as e:  # noqa: BLE001
        logger.info("利润表换源失败（保持现状如实「—」）：%s", e)
        slow_cache.set(cache_key, {}, ttl=600)
        return {}
    if df is None or df.empty:
        slow_cache.set(cache_key, {}, ttl=600)
        return {}

    def _num(v):
        try:
            x = float(str(v).replace(",", ""))
            return x if math.isfinite(x) else None
        except Exception:  # noqa: BLE001
            return None

    cols = {str(c).strip(): c for c in df.columns}

    def _pref_col(candidates):
        """按候选优先级选列（显式偏好序；旧实现按 DataFrame 列序撞大运，
        抓到「营业总成本」（含税金及三费）冒充营业成本，毛利率语义全错——
        茅台实测 68.7% vs 真实 ~91%）。"""
        for name in candidates:
            if name in cols:
                return cols[name]
        return None

    period_col = _pref_col(("报告日", "报告期", "截止日期", "报表日期"))
    if period_col is None:
        logger.info("利润表缺少报告期列（仅有公告日期等），不做补全：%s", list(cols)[:8])
        slow_cache.set(cache_key, {}, ttl=600)
        return {}
    # 营业收入口径：优先「营业收入」（与利润表毛利率口径一致）；「营业总收入」次之
    rev_col = _pref_col(("营业收入", "营业总收入"))
    # 营业成本口径：仅「营业成本」——绝不用「营业总成本」（前者是毛利率分子口径）
    cost_col = _pref_col(("营业成本",))
    out: dict = {}
    if rev_col is not None:
        for rec in df.to_dict("records"):
            period = _norm_period(rec.get(period_col))
            if not period or period in out:
                continue  # 报告期不明不补；同期重复行取首条（上游按期唯一）
            rev = _num(rec.get(rev_col))
            if rev is None or rev <= 0:
                continue
            entry: dict = {"revenue": rev}
            if cost_col is not None:
                cost = _num(rec.get(cost_col))
                if cost is not None:
                    entry["gross_margin_pct"] = (rev - cost) / rev * 100.0
            out[period] = entry
    slow_cache.set(cache_key, out, ttl=6 * 3600)
    return out


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
        periods = [_norm_period(r.get("_date")) for r in rows]
        # 表头按报告期如实标注：年报列 "2025"，中期列 "2026中报"（审计 A01：页面表头、
        # 事实块与正文对报告期的表达一致，不让中期列伪装成整年）
        years = [period_label(p, str(r.get("_year", ""))) for p, r in zip(periods, rows)]

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
                return f"{v/1e8:.2f}{unit}" if abs(v) > 1e6 else f"{v:.2f}"
            return "—"

        # v1.6.0（审计 P0 修复 + 核验勘误）：净利润/增速侧按实测真实列名命中；
        # 营收绝对值经实测（akshare 1.18.64 实拉 600519，86 列全枚举）在该财务指标
        # 接口上**不存在**（仅「主营业务收入增长率(%)」，无任何营收绝对值列）——
        # 下列营收候选是防御性保留（未来上游加列即自动命中），现状如实渲染「—」
        # 并在 provenance.note 留痕，绝不编造。换利润表类接口可补真值（后续增强）。
        revenue_keys = ("营业总收入", "营业收入", "主营业务收入(元)", "营业总收入(元)", "营业收入(元)")
        net_keys = ("净利润", "归母净利润", "扣除非经常性损益后的净利润(元)")
        revenue_vals = [fmt_num(pick(r, *revenue_keys)) for r in rows]
        net_vals = [fmt_num(pick(r, *net_keys)) for r in rows]
        rev_yoy_vals = [fmt_pct(pick(r, "主营业务收入增长率(%)", "营业收入同比增长率(%)")) for r in rows]
        net_yoy_vals = [fmt_pct(pick(r, "净利润增长率(%)", "归母净利润同比增长率(%)")) for r in rows]
        roe_vals = [fmt_pct(pick(r, "净资产收益率(%)", "加权净资产收益率(%)", "净资产收益率", "ROE")) for r in rows]
        gm_vals = [fmt_pct(pick(r, "销售毛利率(%)", "销售毛利率", "毛利率")) for r in rows]
        debt_vals = [fmt_pct(pick(r, "资产负债率(%)", "资产负债率")) for r in rows]

        # v1.6.3（B2）：营收绝对值/毛利率换源补全 —— 财务指标接口无营收绝对值列、
        # 毛利率列全量 NaN（均经实测），从新浪利润表按年（年报口径）补真值；
        # 换源失败保持「—」。
        income = {}
        if any(v == "—" for v in revenue_vals) or any(v == "—" for v in gm_vals):
            try:
                income = _income_statement_annual(raw, years)
            except Exception as e:  # noqa: BLE001
                logger.info("营收/毛利率换源补全失败：%s", e)
        income_filled = []
        for i, y in enumerate(years):
            # 按完整报告期精确匹配（A02）；行无报告期信息（旧缓存/测试夹具）时才按年份
            # 匹配，且仅限年报列 —— 中期列绝不拿整年数补
            period = periods[i]
            if period:
                entry = income.get(period) or {}
            else:
                entry = income.get(str(rows[i].get("_year", y))) or {}
            if not period and rows[i].get("_interim"):
                entry = {}
            if revenue_vals[i] == "—" and entry.get("revenue"):
                v = entry["revenue"]
                revenue_vals[i] = f"{v/1e8:.2f}亿" if abs(v) > 1e6 else f"{v:.2f}"
                income_filled.append(f"营收{y}")
            if gm_vals[i] == "—" and entry.get("gross_margin_pct") is not None:
                gm_vals[i] = fmt_pct(entry["gross_margin_pct"])
                if f"毛利率{y}" not in income_filled:
                    income_filled.append(f"毛利率{y}")

        # 口径溯源：行名统一叫「净利润」，但值实际来自哪个上游列必须留痕（零幻觉原则：
        # 标签不撒谎，扣非就是扣非）。旧行名「扣非净利润」与全部消费方查询的「净利润」
        # 精确匹配不上，导致事实块/基本面段恒「未获取」而风险段却引用真值（同报告自相矛盾）。
        def _hit_col(keys):
            for k in keys:
                if any(k in r and r[k] not in (None, "", "nan") for r in rows):
                    return k
            return ""

        prov = make_provenance(
            source="akshare",
            endpoint="stock_financial_analysis_indicator",
            raw_value=raw,
        )
        # v1.6.3：中期报告口径如实披露（该年份行是中报/三季报而非年报）
        interim_years = [str(r.get("_year", "")) for r in rows if r.get("_interim")]
        interim_note = (
            f"；{','.join(interim_years)} 年暂无年报、行为中期报告口径（同比勿直接与年报行比较）"
            if interim_years else ""
        )
        income_note = (
            f"；营收/毛利率部分报告期由新浪利润表（按报告期对齐）补全：{','.join(income_filled)}"
            if income_filled else ""
        )
        prov["note"] = (
            f"净利润列={_hit_col(net_keys) or '未获取'}；营收列={_hit_col(revenue_keys) or '未获取'}"
            f"（净利润行含扣非口径候选，实际命中列以本注为准）{interim_note}{income_note}"
        )

        # 审计 A01：最新列是中期口径时，给出上一年同一报告期的比率类指标作为同比基准
        #（ROE/毛利率/资产负债率的「同比变动」只能同期相减；营收/净利润的同比本就读
        # 上游增速行，上游按同期口径计算）。拿不到同期行就不给基准 —— 消费方据此
        # 不展示同比，绝不退回「中报 − 上年年报」。
        yoy_base: dict = {}
        latest = rows[-1] if rows else {}
        prior = latest.get("_prior_same_period") if latest.get("_interim") else None
        if isinstance(prior, dict):
            yoy_base = {
                "period": _norm_period(prior.get("_date")),
                "ROE": fmt_pct(pick(prior, "净资产收益率(%)", "加权净资产收益率(%)", "净资产收益率", "ROE")),
                "毛利率": fmt_pct(pick(prior, "销售毛利率(%)", "销售毛利率", "毛利率")),
                "资产负债率": fmt_pct(pick(prior, "资产负债率(%)", "资产负债率")),
            }
            p_entry = income.get(yoy_base["period"]) or {}
            if yoy_base["毛利率"] == "—" and p_entry.get("gross_margin_pct") is not None:
                yoy_base["毛利率"] = fmt_pct(p_entry["gross_margin_pct"])

        return {
            "financials": {
                "years": years,
                "periods": periods,
                "yoy_base": yoy_base,
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
            "provenance": prov,
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
