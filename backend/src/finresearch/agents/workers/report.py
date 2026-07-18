"""report 节点：分析师级研报组装 + 模板驱动的多段 LLM 撰写 + Jinja2 渲染。

铁律（从注释落到执行点，每条都有对应测试）：
1. 数字只能引用事实块，本文件任何字符串拼接都不得凭空捏造价位/增速/比率。
   - MA10 缺失时不用相邻均线插值伪造。
   - 快照不可用（price 缺失或 provenance.error）⇒ 价位/涨跌幅/估值倍数一律 facts.MISSING。
     逐字段判零不够：失败快照的 changePct=0.0 会以"涨跌幅 0.00%"混进事实块，
     而 system prompt 又命令模型"严格引用事实块"——等于指挥模型写编造值。
2. 数据缺失时诚实降级：不展开就是不展开，不一边标"未展开"一边输出整段正文；
   零数据套话（"行业景气度受宏观经济、政策导向共同影响"）一律删除。
3. 合规禁用词：check_generated 只检测 → 命中则重写一次 → 仍命中回退规则模板并标注。
   盲替换已废弃（会污染引文、产生"不建议建议关注（偏多）"）。引文永不 sanitize。
4. 可溯源：证据分层栏按各段**实际**生成来源（llm/template/insufficient）如实声明。

模板驱动：SECTION_SPECS 按 equity_deep/industry/earnings/risk 给出不同栏目集合与顺序；
每段只构造本段所需字段的事实块（估值段看不到新闻，行业段看不到 KDJ），
经 _invoke 的并发闸 + 超时并发撰写；规则模板作为 LLM 不可用/超时/429/校验失败的回退。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from ...config import settings
from ...llm import build_llm
from ...tools import facts, report as report_tool
from ...tools.context import build_report_section_context, finalize_diag
from ...tools.entity import to_market_ticker
from ..prompts import (
    FORBIDDEN_VERBS,
    SUMMARY_USER_TEMPLATE,
    section_system_prompt,
    summary_system_prompt,
)
from ..state import ResearchState

logger = logging.getLogger(__name__)

# 行情来源中文名（证据分层栏动态生成来源描述，替代硬编码"东方财富主源"）
_SRC_LABELS = {
    "sina": "新浪（Sina 直连）",
    "tencent": "腾讯（Tencent 直连）",
    "eastmoney-direct": "东方财富（直连）",
    "akshare": "AkShare（东方财富）",
}

# 摘要 + 各段并发撰写；家用 key 的 RPS 有限，不加闸会成片 429 → 静默回退模板。
_LLM_GATE = asyncio.Semaphore(max(1, int(getattr(settings, "report_llm_concurrency", 3))))
_SECTION_TIMEOUT = float(getattr(settings, "report_section_timeout_s", 60.0))


async def _invoke(llm, messages) -> str:
    """带并发闸与超时的单次 LLM 调用；统一抽取 content（含 content-block 列表形态）。"""
    async with _LLM_GATE:
        resp = await asyncio.wait_for(llm.ainvoke(messages), timeout=_SECTION_TIMEOUT)
    text = resp.content if hasattr(resp, "content") else str(resp)
    if isinstance(text, list):  # 部分模型返回 content block 列表
        text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in text)
    return str(text)


def _extract_token(chunk) -> str:
    """从 astream 的增量 chunk 抽出文本 token（兼容 str / content-block 列表两种形态）。"""
    content = getattr(chunk, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content)


# ────────────────────────────────────────────────────────────
# 合规自检：只检测，不盲替换（替代旧 _sanitize）
# ────────────────────────────────────────────────────────────
def _stream_push(msg) -> None:
    """推送进度到 SSE 流（节点内 langgraph get_stream_writer 写 custom 事件；非流式上下文 noop）。

    routes.py 的 astream 用 stream_mode=["updates","custom"] 消费，转为 SSE tool 事件发给前端。
    解决根因：report 节点 4 段 LLM 并行撰写（30-120s）期间前端零事件只能转圈。
    """
    try:
        from langgraph.config import get_stream_writer
        payload = msg if isinstance(msg, dict) else {"type": "report_progress", "text": str(msg)}
        get_stream_writer()(payload)
    except Exception:
        pass


def check_generated(text: str) -> List[str]:
    """检测 LLM 产出是否命中禁用词，返回命中列表（不改写文本）。

    旧 _sanitize 盲替换会污染引文、产生"不建议建议关注（偏多）"等语义崩坏；
    此处只检测，命中后由调用方发起重写或回退模板。
    """
    if not text:
        return []
    return [w for w in FORBIDDEN_VERBS if w in text]


# ────────────────────────────────────────────────────────────
# state 取数工具
# ────────────────────────────────────────────────────────────
def _find(findings: list, key: str) -> dict:
    """从 findings 按 key 取一项，缺失返回 {}。"""
    for f in findings or []:
        if isinstance(f, dict) and f.get("key") == key:
            return f
    return {}


def _indicator_map(indicators: list) -> dict:
    """[{name,value,signal}] → {name: {value,signal}}。"""
    m: dict[str, dict] = {}
    for it in indicators or []:
        if isinstance(it, dict) and it.get("name"):
            m[it["name"]] = {
                "value": it.get("value", "—"),
                "signal": it.get("signal", "中性"),
            }
    return m


def _trend(vals: list) -> str:
    """对 ROE/营收/负债率等给出趋势方向文字（基于首末值）。"""
    if len(vals) < 2:
        return "（样本不足，趋势未明）"
    first, last = facts.to_float(vals[0]), facts.to_float(vals[-1])
    if first is None or last is None:
        return "（部分年度数据缺失）"
    if last > first * 1.02:
        return "上行"
    if last < first * 0.98:
        return "下行"
    return "基本平稳"


def _sign_pp(delta: Optional[float]) -> str:
    """把百分点变动转为「同比提升/收窄 X.X 个百分点」。"""
    if delta is None:
        return ""
    sign = "提升" if delta > 0 else ("收窄" if delta < 0 else "持平")
    return f"同比{sign} {abs(delta):.1f}个百分点"


# ────────────────────────────────────────────────────────────
# 技术面分析（规则模板——LLM 不可用/失败时回退）
# ────────────────────────────────────────────────────────────
def _ma(v) -> Optional[float]:
    """均线取值：0 视为"未算出"。

    行情工具在样本不足时用 0.0 填 ma5/ma10/ma20（n=120 的前 9 根 ma10 就是 0.0），
    而股价均线不可能为 0——只判 None 会让报告写出"10日均线 0.00 元"。
    """
    f = facts.to_float(v)
    return None if f is None or f == 0.0 else f


def _ma_arrangement(kline: list, im: dict) -> str:
    """均线排列状态。MA10 缺失时不插值伪造，也不提及"10日均线"。"""
    last = kline[-1] if kline else {}
    ma5 = _ma(last.get("ma5"))
    ma10 = _ma(last.get("ma10"))
    ma20 = _ma(last.get("ma20"))
    close = _ma(last.get("close"))
    if ma5 is None:
        ma5 = _ma(im.get("MA5", {}).get("value"))
    if ma20 is None:
        ma20 = _ma(im.get("MA20", {}).get("value"))
    # MA10 缺失即缺失，不再插值伪造（铁律修复 #1）
    if None in (ma5, ma20, close):
        return "（均线数据不足，无法研判排列状态）"

    # 只在 ma10 存在时才提及 10日均线（验收：MA10 缺失时正文不含"10日均线"）
    if ma10 is not None:
        parts = [f"5日均线 {facts.fmt(ma5)} 元、10日均线 {facts.fmt(ma10)} 元、20日均线 {facts.fmt(ma20)} 元"]
        if ma5 > ma10 > ma20:
            arrange = "标准多头排列且向上发散，短期多头格局确立"
        elif ma5 < ma10 < ma20:
            arrange = "空头排列并向下发散，中期调整趋势未改"
        else:
            arrange = "短期均线缠绕粘合、趋于走平，多空分歧加大，方向选择在即"
    else:
        parts = [f"5日均线 {facts.fmt(ma5)} 元、20日均线 {facts.fmt(ma20)} 元"]
        arrange = "短期均线仅依 5/20 日两线研判（中期均线数据缺失，三线排列暂不完整）"

    pos_ma5 = "站上" if close > ma5 else "跌破"
    pos_ma20 = "运行于20日均线之上" if close > ma20 else "受制于20日均线"
    return (
        f"{'，'.join(parts)}，呈{arrange}；"
        f"当前股价 {facts.fmt(close)} 元{pos_ma5}5日均线、{pos_ma20}。"
    )


def _support_resistance(kline: list, im: dict) -> str:
    """支撑压力位：近 K 线高低点 + BOLL 中轨。"""
    parts: list[str] = []
    if len(kline) >= 5:
        recent = kline[-min(20, len(kline)):]
        highs = [k["high"] for k in recent if k.get("high") is not None]
        lows = [k["low"] for k in recent if k.get("low") is not None]
        if highs and lows:
            rh, rl = max(highs), min(lows)
            parts.append(f"近20日区间高点 {facts.fmt(rh)} 元构成第一压力位、低点 {facts.fmt(rl)} 元为第一支撑位")
    boll = im.get("BOLL", {})
    boll_mid = facts.to_float(boll.get("value"))
    boll_sig = boll.get("signal", "中性")
    if boll_mid:
        pos_desc = {
            "多头": "运行于布林带中轨之上，相对位置偏强",
            "空头": "运行于布林带下轨之下，相对位置偏弱",
            "中性": "围绕布林带中轨震荡",
        }.get(boll_sig, "围绕布林带中轨震荡")
        parts.append(
            f"布林带中轨（20日均线）位于 {facts.fmt(boll_mid)} 元，股价{pos_desc}；"
            "上下轨具体价位需结合波动率进一步测算（受限于当前指标数据，仅披露中轨）"
        )
    if not parts:
        return "（K线与布林带数据不足，无法定位支撑压力位）"
    return "；".join(parts) + "。"


def _momentum(im: dict) -> str:
    """动量：MACD 红绿柱方向 + RSI 超买超卖 + KDJ。"""
    parts: list[str] = []
    macd = im.get("MACD", {})
    hist = facts.to_float(macd.get("value"))
    macd_sig = macd.get("signal", "中性")
    if hist is not None:
        bar = "红柱" if hist > 0 else "绿柱"
        amp = "温和放大" if abs(hist) > 0.05 else "极短"
        cross = {"金叉": "MACD 完成金叉", "死叉": "MACD 出现死叉"}.get(macd_sig, "MACD 双线缠绕")
        parts.append(f"{cross}，{bar}{amp}，{'多头动能增强' if hist > 0 else '空头动能释放'}")
    rsi = im.get("RSI(14)", {})
    rsi_v = facts.to_float(rsi.get("value"))
    rsi_sig = rsi.get("signal", "中性")
    if rsi_v is not None:
        zone = {"超买": "进入超买区域，短线存在技术性回调需求",
                "超卖": "深陷超卖区域，技术性反弹一触即发",
                "中性": "处于中性区域，多空力量相对均衡"}.get(rsi_sig, "运行于中性区域")
        parts.append(f"RSI(14) 为 {facts.fmt(rsi_v, 1)}，{zone}")
    kdj = im.get("KDJ-J", {})
    j_v = facts.to_float(kdj.get("value"))
    j_sig = kdj.get("signal", "中性")
    if j_v is not None:
        kzone = {"超买": "进入超买区，短线择时信号偏谨慎",
                 "超卖": "处于超卖区，低位反弹信号较强",
                 "中性": "在中性区运行"}.get(j_sig, "在中性区运行")
        parts.append(f"KDJ-J 值 {facts.fmt(j_v, 1)}{kzone}")
    return "；".join(parts) + "。" if parts else "（动量指标数据不足）"


def _tech_summary(im: dict) -> tuple[str, str]:
    """多指标共振定性：返回 (方向定性, 风险/机会提示)。"""
    bull = 0
    bear = 0
    for name in ("MA5", "MA20", "BOLL"):
        sg = im.get(name, {}).get("signal", "中性")
        if sg == "多头":
            bull += 1
        elif sg == "空头":
            bear += 1
    macd = im.get("MACD", {}).get("signal")
    if macd == "金叉":
        bull += 1
    elif macd == "死叉":
        bear += 1
    overbought = sum(1 for n in ("RSI(14)", "KDJ-J") if im.get(n, {}).get("signal") == "超买")
    oversold = sum(1 for n in ("RSI(14)", "KDJ-J") if im.get(n, {}).get("signal") == "超卖")
    if bull >= 2 and bull > bear:
        direction = "短期技术面偏多"
    elif bear >= 2 and bear > bull:
        direction = "短期技术面偏空"
    else:
        direction = "短期技术面以震荡为主"
    note = ""
    if overbought >= 2:
        note = "多重超买信号共振，存在技术性回调压力"
    elif oversold >= 2:
        note = "多重超卖信号共振，存在技术性反弹机会"
    return direction, note


def _section_technical(fv: dict) -> str:
    """技术面规则模板（回退用）。fv = normalize_findings 产出。"""
    kline = fv.get("kline", [])
    im = _indicator_map(fv.get("indicators", []))
    quote = fv.get("quote", {})
    close = facts.to_float(quote.get("price"))
    blocks: list[str] = []
    blocks.append("【趋势与均线】" + _ma_arrangement(kline, im))
    blocks.append("【支撑与压力】" + _support_resistance(kline, im))
    blocks.append("【动量指标】" + _momentum(im))
    direction, note = _tech_summary(im)
    conc = f"【综合研判】多指标交叉验证，{direction}"
    if note:
        conc += f"；{note}"
    conc += "。技术分析仅为辅助参考，存在指标滞后与信号失效可能，不构成投资建议。"
    blocks.append(conc)
    return "\n".join(blocks)


# ────────────────────────────────────────────────────────────
# 基本面分析（规则模板——回退用）
# ────────────────────────────────────────────────────────────
def _section_fundamentals(fv: dict) -> str:
    fins = fv.get("fins", {})
    rev = facts.row_vals(fins, "营业收入")
    net = facts.row_vals(fins, "净利润")
    roe = facts.row_vals(fins, "ROE")
    gm = facts.row_vals(fins, "毛利率")
    debt = facts.row_vals(fins, "资产负债率")
    years = fins.get("years", []) or []

    blocks: list[str] = []
    rev_yoy = facts.yoy_pct(rev)
    net_yoy = facts.yoy_pct(net)
    rev_l, net_l = facts.val_str(rev), facts.val_str(net)
    if rev_yoy is not None or net_yoy is not None:
        line = (
            f"【成长性】最新一期（{years[-1] if years else '—'}）实现营业收入 {rev_l}"
            f"（同比 {facts.pct(rev_yoy, 1)}）、"
            f"净利润 {net_l}（同比 {facts.pct(net_yoy, 1)}）。"
        )
        if rev_yoy is not None and net_yoy is not None:
            if net_yoy > rev_yoy + 5:
                line += "利润增速高于营收增速，关注毛利率扩张或费用率优化的贡献。"
            elif net_yoy < rev_yoy - 5:
                line += "呈现增收不增利特征，关注成本端或费用端对盈利的拖累。"
            else:
                line += "营收与利润增速基本匹配，盈利转化平稳。"
        blocks.append(line)
    else:
        blocks.append("【成长性】营收/净利润同比数据不足，无法量化增速趋势。")

    roe_l = facts.val_str(roe)
    gm_l = facts.val_str(gm)
    roe_pp = facts.delta_pp(roe)
    gm_pp = facts.delta_pp(gm)
    quality = (
        f"【盈利质量】ROE 最新 {roe_l}"
        f"（{_sign_pp(roe_pp) or '同比变动数据不足'}），"
        f"毛利率 {gm_l}（{_sign_pp(gm_pp) or '同比变动数据不足'}）。"
    )
    roe_f = facts.to_float(roe_l)
    if roe_f is not None:
        if roe_f >= 15:
            quality += "ROE 处于较高水平，盈利能力较强；"
        elif roe_f >= 8:
            quality += "ROE 处于行业中枢附近，盈利能力稳健；"
        else:
            quality += "ROE 偏低，盈利能力有待改善；"
    quality += "毛利率近五年" + _trend(gm) + "，反映产品定价权与成本端的边际变化。"
    blocks.append(quality)

    debt_l = facts.val_str(debt)
    debt_pp = facts.delta_pp(debt)
    debt_f = facts.to_float(debt_l)
    lev = f"【资本结构】资产负债率 {debt_l}（{_sign_pp(debt_pp) or '同比变动数据不足'}），"
    if debt_f is not None:
        if debt_f >= 60:
            lev += "处于偏高水平，需关注有息负债结构与利息保障倍数。"
        elif debt_f >= 40:
            lev += "处于适中水平，财务结构总体稳健。"
        else:
            lev += "处于较低水平，财务杠杆运用保守。"
    else:
        lev += "具体水平需结合行业可比口径评估。"
    blocks.append(lev)

    blocks.append(
        "【说明】以上财务数据均来自 akshare 财务分析指标接口（详见财务概览表与数据来源），"
        "未经人工调整；杜邦分解、现金流质量等更细颗粒度分析受限于当前数据维度，"
        "建议结合定期报告附注进一步验证。"
    )
    return "\n".join(blocks)


# ────────────────────────────────────────────────────────────
# 估值（规则模板——回退用；修复 0.0 冒充未获取）
# ────────────────────────────────────────────────────────────
def _section_valuation(fv: dict) -> str:
    quote = fv.get("quote", {})
    fins = fv.get("fins", {})
    # 快照不可用 → 估值倍数一律 MISSING，不再打印"PE 0.0 倍"（铁律修复 #2）
    m = _quote_measures(quote)
    as_of = quote.get("as_of") if facts.quote_usable(quote) else None
    body = (
        f"【相对估值】当前股价对应动态 PE {m['pe']}、PB {m['pb']}"
        f"（数据时点：{as_of or '行情快照不可用'}）。"
    )
    roe_l = facts.val_str(facts.row_vals(fins, "ROE"))
    body += f"公司 ROE 最新 {roe_l}，可作为 PB 估值的盈利支撑参考。"
    body += (
        "【数据局限】受限于当前可得的行业可比公司样本与历史分位数数据，"
        "本报告暂无法给出近 3/5 年历史分位数定位与可比公司均值对照，"
        "亦未对目标价进行绝对估值（DCF）测算。"
        "建议结合 Wind/同花顺等终端的行业 PE/PB 中位数、可比公司均值及历史分位数进一步交叉验证。"
    )
    return body


# ────────────────────────────────────────────────────────────
# 行业与公司（规则模板——回退用；引文不经 sanitize）
# ────────────────────────────────────────────────────────────
def _section_industry_company(fv: dict) -> str:
    quote = fv.get("quote", {})
    profile = fv.get("profile", {})
    news = fv.get("news", [])
    web = fv.get("web", [])
    name = quote.get("name") or profile.get("name", "")
    industry = quote.get("industry") or profile.get("industry", "")
    market_cap = quote.get("marketCap") or profile.get("market_cap", "—")
    parts = [
        f"【行业定位】{name}所属{industry or '（行业未识别）'}行业，"
        f"总市值约 {market_cap}。"
    ]
    parts.append(
        "【商业模式】公司具体业务模式、主营构成与竞争壁垒需结合公司公告与招股说明书进一步确认；"
        "基于公开行情与财务指标，本报告对其行业地位仅做框架性描述。"
    )
    if news:
        items = news[:3]
        headlines = "；".join(
            f"「{n.get('title', '').strip() or '相关动态'}」（{n.get('source', '') or n.get('time', '公开来源')}）"
            for n in items if isinstance(n, dict)
        )
        parts.append(f"【新闻证据·个股资讯】东方财富公开资讯显示：{headlines}。")
    if web:
        items = web[:3]
        headlines = "；".join(
            f"「{w.get('title', '').strip() or '相关网页'}」（{w.get('source', '') or '网页'}）"
            for w in items if isinstance(w, dict) and w.get("title")
        )
        if headlines:
            parts.append(f"【公开信息·联网搜索】网络检索显示：{headlines}。")
    parts.append(
        "【竞争格局】行业集中度、产业链价值量分布与公司市占率需补充第三方研究数据（如行业协会、"
        "Frost & Sullivan）方可定量评估，本报告暂不展开。"
    )
    return "\n".join(parts)


# ────────────────────────────────────────────────────────────
# 证据分层与数据来源（规则生成——描述数据来源事实，不让模型写）
# ────────────────────────────────────────────────────────────
def _section_evidence_traceback(findings: list, fv: dict, gen: dict) -> str:
    """证据分层。gen = {段标题: 'llm'|'template'|'insufficient'}，据此如实声明生成来源。

    此前这一栏写死"投资摘要与各分析段由大语言模型生成"——LLM 未配置或 429 降级时，
    正文全是规则模板，这一栏却仍这么宣称。这一栏的全部意义就是告诉读者哪句话是谁写的。
    """
    quote_prov = (fv.get("quote", {}).get("provenance") or {}) if isinstance(fv.get("quote"), dict) else {}
    kline_prov = _find(findings, "kline").get("provenance") or {}
    news_items = fv.get("news", [])
    web_items = fv.get("web", [])
    web_configured = fv.get("web_configured", False)
    mkt_src = quote_prov.get("source") or kline_prov.get("source") or "akshare"
    src_label = _SRC_LABELS.get(mkt_src, mkt_src or "akshare")
    quote_err = quote_prov.get("error")

    if facts.quote_usable(fv.get("quote")):
        market_line = (
            f"【行情事实】实时快照与日K数据来自 {src_label}，"
            "所有价位、成交量、技术指标均由本地工具库计算，未经人工调整。"
        )
    else:
        market_line = (
            f"【行情事实】行情快照取数失败（来源 {mkt_src}"
            + (f"，原因：{quote_err}" if quote_err else "")
            + "）：本报告未使用任何实时价位或估值倍数。"
        )
    parts = [
        market_line,
        f"【新闻证据】个股资讯来自 akshare·stock_news_em（东方财富），共引用 {len(news_items)} 条，"
        "仅作背景参考，不代表本报告立场。",
    ]
    if web_configured:
        parts.append(
            f"【公开信息】联网搜索增强已启用（Serper），共检索 {len(web_items)} 条公开网页结果，"
            "标题/摘要/链接见下方来源清单，需读者自行核验时效与权威性。"
        )
    else:
        parts.append(
            "【公开信息】联网搜索增强未配置（SERPER_API_KEY 缺失）：本报告未引入真实网页搜索证据。"
            "如需补充公开信息交叉验证，请在「设置」中填入 Serper API Key。"
        )

    by_llm = [t for t, g in gen.items() if g == "llm"]
    by_tpl = [t for t, g in gen.items() if g == "template"]
    thin = [t for t, g in gen.items() if g == "insufficient"]
    lines = []
    if by_llm:
        lines.append(f"模型撰写：{'、'.join(by_llm)}（基于结构化事实块，凡未出现在事实块中的判断均为定性推断）")
    if by_tpl:
        lines.append(f"规则模板撰写：{'、'.join(by_tpl)}（模型不可用、调用失败或输出含禁用表述，已降级）")
    if thin:
        lines.append(f"数据不足未展开：{'、'.join(thin)}")
    parts.append("【生成来源】" + ("；".join(lines) if lines else "全部由规则模板生成。") + "。本报告不构成投资建议。")
    return "\n".join(parts)


# ────────────────────────────────────────────────────────────
# SECTION_SPECS：模板驱动组装（四段分析；模板差异仅在摘要侧重）
# ────────────────────────────────────────────────────────────
@dataclass
class SectionSpec:
    key: str
    title: str
    has_data: Callable[[dict], bool]
    build_facts: Callable[[dict], str]
    fallback: Callable[[dict], str]
    max_chars: int = 320


def _has_indicators(fv: dict) -> bool:
    return bool(fv.get("kline")) or bool(fv.get("indicators"))


def _has_fins(fv: dict) -> bool:
    return bool((fv.get("fins") or {}).get("rows"))


def _has_quote(fv: dict) -> bool:
    return facts.quote_usable(fv.get("quote"))


def _has_evidence(fv: dict) -> bool:
    # 行业估值本身就是证据：有它就该展开，即便这次没抓到新闻。
    iv = fv.get("industry_valuation")
    if iv and iv.get("items"):
        return True
    return bool(fv.get("news")) or bool(fv.get("web"))


def _facts_technical(fv: dict) -> str:
    return facts.compose_facts(
        facts.render_indicator_facts(fv.get("indicators", [])),
        facts.render_quote_facts(fv.get("quote", {})),
    )


def _facts_fundamentals(fv: dict) -> str:
    return facts.compose_facts(facts.render_financial_facts(fv.get("fins", {})))


def _facts_valuation(fv: dict) -> str:
    roe = facts.val_str(facts.row_vals(fv.get("fins", {}), "ROE"))
    return facts.compose_facts(
        facts.render_quote_facts(fv.get("quote", {})),
        f"【盈利支撑】公司 ROE 最新 {roe}。",
    )


def _facts_industry(fv: dict) -> str:
    return facts.compose_facts(
        facts.render_quote_facts(fv.get("quote", {})),
        facts.render_news_facts(fv.get("news", []), fv.get("web", [])),
    )


def _facts_landscape(fv: dict) -> str:
    # 行业研究：fv 带 industry_valuation（search_industry 的申万估值）时，用真实行业估值数据。
    # 个股走 industry 模板时没有这个键，退回原来的"个股行业归属 + 个股资讯"，行为不变。
    iv = fv.get("industry_valuation")
    if iv and iv.get("items"):
        return facts.compose_facts(
            _render_industry_valuation_facts(iv),
            facts.render_news_facts(fv.get("news", []), fv.get("web", [])),
        )
    quote = fv.get("quote", {})
    industry = quote.get("industry") or (fv.get("profile") or {}).get("industry") or facts.MISSING
    return facts.compose_facts(
        f"【行业归属】{industry}",
        facts.render_news_facts(fv.get("news", []), fv.get("web", [])),
    )


def _render_industry_valuation_facts(iv: dict) -> str:
    """把 search_industry 的申万行业估值渲染成事实块（数字全部来自工具，模型只引用）。"""
    lines = []
    for it in (iv.get("items") or [])[:6]:
        parts = [f"{it.get('name', '')}（{it.get('level', '')}，{it.get('constituents', 0)}只成份）"]
        pe = it.get("pe_ttm")
        pb = it.get("pb")
        dy = it.get("dividend_yield")
        if pe is not None:
            parts.append(f"TTM市盈率{pe}")
        if pb is not None:
            parts.append(f"市净率{pb}")
        if dy is not None:
            parts.append(f"股息率{dy}%")
        lines.append("· " + "，".join(parts))
    warn = ""
    if (iv.get("levels_ok") or 0) < (iv.get("levels_total") or 0):
        warn = "\n（注：部分层级取数失败，行业列表可能不完整。）"
    body = "\n".join(lines) if lines else facts.MISSING
    return f"【申万行业估值·数据源新浪】\n{body}{warn}"


def _facts_earnings(fv: dict) -> str:
    return facts.compose_facts(facts.render_financial_facts(fv.get("fins", {})))


def _facts_scenario(fv: dict) -> str:
    return facts.compose_facts(
        facts.render_quote_facts(fv.get("quote", {})),
        facts.render_financial_facts(fv.get("fins", {})),
        facts.render_indicator_facts(fv.get("indicators", [])),
    )


def _section_landscape(fv: dict) -> str:
    """行业景气的规则回退：只原样罗列已获取的资讯证据，不作任何解读。

    旧实现在这里放"行业景气度受宏观经济、政策导向与下游需求周期共同影响"——
    零数据、纯模板，读起来却像分析。宁可只摆证据，不要假分析。
    """
    quote = fv.get("quote", {})
    industry = quote.get("industry") or (fv.get("profile") or {}).get("industry", "")
    parts = [f"【行业归属】{industry or '（行业未识别）'}。"]

    # 行业估值：数字来自 search_industry（申万/新浪），规则回退照抄不做解读，不违反零幻觉。
    iv = fv.get("industry_valuation")
    if iv and iv.get("items"):
        vals = []
        for it in iv["items"][:5]:
            seg = f"{it.get('name','')}"
            if it.get("pe_ttm") is not None:
                seg += f" TTM市盈率{it['pe_ttm']}"
            if it.get("pb") is not None:
                seg += f" 市净率{it['pb']}"
            if it.get("dividend_yield") is not None:
                seg += f" 股息率{it['dividend_yield']}%"
            vals.append(seg)
        parts.append("【申万行业估值·新浪】" + "；".join(vals) + "。")
        if (iv.get("levels_ok") or 0) < (iv.get("levels_total") or 0):
            parts.append("（注：部分层级取数失败，行业列表可能不完整。）")

    cites = []
    for item in list(fv.get("news", []))[:3] + list(fv.get("web", []))[:3]:
        if isinstance(item, dict) and (item.get("title") or "").strip():
            cites.append(f"「{item['title'].strip()}」（{item.get('source') or '公开来源'}）")
    if cites:
        parts.append("【已获取的公开资讯】" + "；".join(cites) + "。")
    parts.append("【说明】模型不可用，本栏仅罗列已取得的估值与资讯证据，未作景气度解读；行业规模、集中度与市占率需第三方数据支撑，本报告不展开。")
    return "\n".join(parts)


def _section_scenario(fv: dict) -> str:
    """情景分析的规则回退：只给出可监测的真实信号清单，不编造情景概率或目标价。"""
    im = _indicator_map(fv.get("indicators", []))
    fins = fv.get("fins", {})
    monitors = [f"{n}（当前信号：{im[n].get('signal', '中性')}）" for n in im]
    net_yoy = _latest_pct(fins, "净利润同比", "净利润")
    if net_yoy is not None:
        monitors.append(f"净利润同比 {facts.pct(net_yoy, 1)}")
    debt = facts.val_str(facts.row_vals(fins, "资产负债率"))
    if debt != facts.MISSING:
        monitors.append(f"资产负债率 {debt}")
    body = "【说明】模型不可用，情景研判需基于事实块由模型撰写，本栏不作定性推断。"
    if monitors:
        body += "\n【可监测指标】" + "；".join(monitors) + "。"
    else:
        body += "\n【可监测指标】当前无可用指标数据。"
    return body


# 四段通用分析
_SPEC_TECHNICAL = SectionSpec("technical", "技术面分析", _has_indicators, _facts_technical, _section_technical)
_SPEC_FUNDAMENTALS = SectionSpec("fundamentals", "基本面分析", _has_fins, _facts_fundamentals, _section_fundamentals)
_SPEC_VALUATION = SectionSpec("valuation", "估值分析", _has_quote, _facts_valuation, _section_valuation)
_SPEC_INDUSTRY = SectionSpec("industry", "行业与公司分析", _has_quote, _facts_industry, _section_industry_company)

# 模板专属段：只在真实证据/数据存在时组装，绝不放零数据套话
_SPEC_LANDSCAPE = SectionSpec(
    "industry_landscape", "行业景气与竞争格局", _has_evidence, _facts_landscape, _section_landscape, 420,
)
_SPEC_EARNINGS = SectionSpec("earnings_review", "业绩快评", _has_fins, _facts_earnings, _section_fundamentals, 420)
_SPEC_SCENARIO = SectionSpec(
    "risk_scenario", "风险情景与监测指标", lambda fv: _has_quote(fv) or _has_fins(fv),
    _facts_scenario, _section_scenario, 420,
)

# 模板驱动组装：不同模板给出不同的栏目集合与顺序（此前四模板产出结构完全相同）
SECTION_SPECS: dict[str, List[SectionSpec]] = {
    "equity_deep": [_SPEC_TECHNICAL, _SPEC_FUNDAMENTALS, _SPEC_VALUATION, _SPEC_INDUSTRY],
    # 专项模板只生成自身栏目；财务表、风险清单和来源由报告骨架统一展示。
    "industry": [_SPEC_LANDSCAPE],
    "earnings": [_SPEC_EARNINGS],
    "risk": [_SPEC_SCENARIO],
}


def _section_specs(template: str) -> List[SectionSpec]:
    return SECTION_SPECS.get((template or "equity_deep").strip(), SECTION_SPECS["equity_deep"])


# ────────────────────────────────────────────────────────────
# 单段 LLM 撰写（命中禁用词→重写→仍命中回退模板并标注）
# ────────────────────────────────────────────────────────────
async def _llm_section(state: ResearchState, spec: SectionSpec, fv: dict) -> tuple[str, str]:
    """单段撰写，返回 (正文, 生成来源)。来源取值 llm / template / insufficient。

    来源要如实上报：证据分层栏据此声明哪几段是模型写的、哪几段是规则模板兜底的。
    LLM 不可用/超时/429/命中禁用词且重写无效 → 回退规则模板。单段独立降级，互不影响。
    """
    template = state.get("template", "equity_deep")
    task_id = state.get("task_id") or state.get("normalized_ticker", "")

    if not spec.has_data(fv):
        # 诚实降级：不展开就是不展开，别一边标"未展开"一边输出整段正文
        return f"（{spec.title}所需数据不足，本报告不展开该部分。）", "insufficient"

    facts_block = spec.build_facts(fv)
    sys_prompt = section_system_prompt(spec.key, template)

    # temperature 统一 1.0：o3 / QwQ 等推理模型拒绝非默认 temperature，
    # 设 0.7 会让这些模型整段静默回退模板。
    # streaming=True：报告分段改真流式，逐 token 吐给前端（见下方 astream 循环）。
    llm = build_llm(
        streaming=True,
        max_tokens=max(64, int(getattr(settings, "report_section_max_tokens", 480))),
        override=state.get("llm_config"),
    )
    if llm is None:
        return spec.fallback(fv), "template"

    lc, diag = build_report_section_context(
        system_prompt=sys_prompt, facts_block=facts_block,
        profile=state.get("profile"), template=template,
        section=spec.key, obj_id=task_id,
    )
    _t0 = time.perf_counter()

    def _elapsed() -> int:
        return int((time.perf_counter() - _t0) * 1000)

    # 真流式（方案A）：逐 token 从模型流出，每个安全 token 立即经 section_token 推给前端，
    # 同时累积成整段正文。相较旧的一次性 _invoke，前端能一字字看到板块生成过程。
    cap = min(spec.max_chars, max(120, int(getattr(settings, "report_section_max_chars", 420))))
    parts: list[str] = []
    acc = ""
    forbidden = False
    try:
        async with asyncio.timeout(_SECTION_TIMEOUT):
            async with _LLM_GATE:
                async for chunk in llm.astream(lc):
                    tok = _extract_token(chunk)
                    if not tok:
                        continue
                    if not acc and not tok.strip():
                        continue  # 跳过前导纯空白 token，避免空段先闪一段空白再被模板覆盖
                    # 实时禁用词闸：并入这段 token 会命中禁用词，就不外发它、停止流式，
                    # 转下方回退模板（最终 report_section 事件会以模板 body 覆盖已流出的片段）。
                    if check_generated(acc + tok):
                        forbidden = True
                        break
                    acc += tok
                    parts.append(tok)
                    _stream_push({"type": "section_token", "title": spec.title, "text": tok})
                    if len(acc) >= cap:
                        break  # 到确定性字符上限即停，省 token
        if forbidden:
            finalize_diag(diag, _elapsed(), error="forbidden_verbs")
            return spec.fallback(fv) + "（模型输出含禁用表述，已使用事实模板。）", "template"
        text = "".join(parts)
        # 空/纯空白兜底：模型在并发限流、内容审查、max_tokens 被 reasoning 吃满等情况下偶发返回空。
        # 空返回当撰写失败、回退规则模板（body 必非空），如实标注来源 template；否则渲染成空白章节。
        if not text.strip():
            logger.warning("%s LLM 流式返回空内容，回退模板", spec.key)
            finalize_diag(diag, _elapsed(), error="empty_llm_output")
            return spec.fallback(fv), "template"
        text, truncated = _bound_generated_text(text, cap)
        finalize_diag(diag, _elapsed(), note="output_truncated" if truncated else "")
        return text, "llm"
    except Exception as e:  # noqa: BLE001
        logger.warning("%s LLM 流式撰写失败，回退模板：%s", spec.key, e)
        finalize_diag(diag, _elapsed(), error=str(e))
        return spec.fallback(fv), "template"


def _bound_generated_text(text: str, max_chars: int) -> tuple[str, bool]:
    """对模型正文做确定性字符上限，优先在中文句号/分号边界截断。"""
    value = (text or "").strip()
    if len(value) <= max_chars:
        return value, False
    window = value[:max_chars]
    cut = max(window.rfind(mark) for mark in ("。", "；", "！", "？", "\n"))
    if cut < max_chars // 2:
        cut = max_chars - 1
    bounded = window[: cut + 1].rstrip()
    if len(bounded) >= max_chars:
        bounded = bounded[: max_chars - 1].rstrip()
    return bounded + "…", True


# ────────────────────────────────────────────────────────────
# 投资摘要（接上高质量 SUMMARY 提示词；0.0 不冒充未获取）
# ────────────────────────────────────────────────────────────
def _quote_measures(quote: dict) -> dict:
    """行情派生量的统一渲染。快照整体不可用时，价位/涨跌幅/估值一律 MISSING。

    铁律执行点：早期实现用 facts.fmt 渲染 price/changePct，而 fmt 只把 None 映射为 MISSING，
    于是失败快照的 0.0 被渲染成 "0.00 元" / "0.00%"，编造值一路流进摘要与事实块。
    """
    if not facts.quote_usable(quote):
        return {"price": facts.MISSING, "change_pct": facts.MISSING,
                "pe": facts.MISSING, "pb": facts.MISSING, "market_cap": facts.MISSING}
    return {
        "price": facts.measure(quote.get("price"), " 元", 2, zero_missing=True),
        "change_pct": facts.pct(quote.get("changePct"), 2),
        "pe": facts.mult_str(quote.get("pe"), 1),
        "pb": facts.mult_str(quote.get("pb"), 1),
        "market_cap": facts.val_or_missing(quote.get("marketCap"), "marketCap"),
    }


def _latest_pct(fins: dict, direct_name: str, base_name: str) -> Optional[float]:
    """优先使用数据源直接提供的同比，旧数据再由绝对值序列计算。"""
    direct = facts.to_float(facts.val_str(facts.row_vals(fins, direct_name)))
    return direct if direct is not None else facts.yoy_pct(facts.row_vals(fins, base_name))


def _compose_summary_block(state: ResearchState, fv: dict) -> str:
    """按报告模板生成互不串用的事实摘要；数字缺失统一走 MISSING。"""
    quote = fv.get("quote", {})
    profile = fv.get("profile", {})
    fins = fv.get("fins", {})
    name = quote.get("name") or profile.get("name", "该公司")
    industry = quote.get("industry") or profile.get("industry", "")
    template = (state.get("template") or "equity_deep").strip()
    m = _quote_measures(quote)
    roe_l = facts.val_str(facts.row_vals(fins, "ROE"))
    gross_l = facts.val_str(facts.row_vals(fins, "毛利率"))
    debt_l = facts.val_str(facts.row_vals(fins, "资产负债率"))
    im = _indicator_map(fv.get("indicators", []))
    direction, _ = _tech_summary(im)
    rev_yoy = _latest_pct(fins, "营收同比", "营业收入")
    net_yoy = _latest_pct(fins, "净利润同比", "净利润")
    risk_signals: list[str] = []
    cpct = facts.to_float(quote.get("changePct")) if facts.quote_usable(quote) else None
    if cpct is not None and cpct < -3:
        risk_signals.append("短线跌幅较大，需观察量价修复")
    if net_yoy is not None and net_yoy < 0:
        risk_signals.append(f"净利润同比 {net_yoy:.1f}%，盈利端承压")
    if not risk_signals:
        risk_signals.append("当前事实指标未触发明确单项预警")

    disclaimer = "本内容仅供研究参考，不构成投资建议。"
    if template == "industry":
        evidence_count = len(fv.get("news") or []) + len(fv.get("web") or [])
        return (
            f"行业研究摘要：{name}的行业归属为{industry or '未获取'}；本次取得可引用公开资讯 {evidence_count} 条。"
            f"报告仅围绕已取得的行业景气、竞争格局和产业链证据展开，不以个股技术走势或估值替代行业结论。{disclaimer}"
        )
    if template == "earnings":
        return (
            f"财报点评摘要：{name}最新可得口径显示，营收同比 {facts.pct(rev_yoy, 1)}、"
            f"净利润同比 {facts.pct(net_yoy, 1)}、ROE {roe_l}、毛利率 {gross_l}、资产负债率 {debt_l}。"
            f"缺失指标保持未获取状态，不使用股价走势补写业绩结论。{disclaimer}"
        )
    if template == "risk":
        return (
            f"风险评估摘要：{name}最新价 {m['price']}、涨跌幅 {m['change_pct']}，技术状态为{direction}；"
            f"净利润同比 {facts.pct(net_yoy, 1)}、资产负债率 {debt_l}。"
            f"当前可验证风险信号：{'；'.join(risk_signals)}。后文仅列风险情景和可监测指标，不给出目标价或情景概率。{disclaimer}"
        )
    return (
        f"个股深度摘要：{name}（{industry or '行业未标注'}）最新价 {m['price']}，技术状态为{direction}；"
        f"动态 PE {m['pe']}、PB {m['pb']}、ROE {roe_l}，营收同比 {facts.pct(rev_yoy, 1)}、"
        f"净利润同比 {facts.pct(net_yoy, 1)}。报告将分别核验技术、基本面、估值和公司行业证据。{disclaimer}"
    )


async def _llm_summary(state: ResearchState, fv: dict) -> str:
    """分析师级摘要。使用 summary_system_prompt（此前死代码，现已接上）。

    LLM 未配置或失败时回退模板（数字仍来自事实块，缺失走 MISSING）。
    """
    quote = fv.get("quote", {})
    profile = fv.get("profile", {})
    fins = fv.get("fins", {})
    name = quote.get("name", "")
    code = state.get("normalized_ticker", "")
    template = state.get("template", "equity_deep")
    task_id = state.get("task_id") or code
    im = _indicator_map(fv.get("indicators", []))
    direction, _ = _tech_summary(im)
    tech_signals = "；".join(
        f"{n} {im[n].get('value', facts.MISSING)}（{im[n].get('signal', '')}）"
        for n in ("MA5", "MA20", "MACD", "RSI(14)", "KDJ-J", "BOLL")
        if n in im
    ) or "指标数据不足"

    rev = facts.row_vals(fins, "营业收入")
    net = facts.row_vals(fins, "净利润")
    risk_signals = "；".join(state.get("risk_flags") or []) or "未触发明确风险信号"

    m = _quote_measures(quote)
    s_facts = {
        "name": name,
        "code": code,
        "industry": quote.get("industry") or profile.get("industry", ""),
        **m,
        "as_of": quote.get("as_of") or state.get("data_as_of", "") or facts.MISSING,
        "roe": facts.val_str(facts.row_vals(fins, "ROE")),
        "revenue": facts.val_str(rev),
        "rev_yoy": facts.pct(facts.yoy_pct(rev), 1),
        "net_profit": facts.val_str(net),
        "net_yoy": facts.pct(facts.yoy_pct(net), 1),
        "gross_margin": facts.val_str(facts.row_vals(fins, "毛利率")),
        "debt_ratio": facts.val_str(facts.row_vals(fins, "资产负债率")),
        "tech_direction": direction,
        "tech_signals": tech_signals,
        "risk_signals": risk_signals,
        "missing": facts.MISSING,
    }

    llm = build_llm(streaming=False, override=state.get("llm_config"))
    if llm is None:
        return _compose_summary_block(state, fv) + "（LLM 未配置，以上为模板化摘要，数字均来自本地工具库。）"

    user = SUMMARY_USER_TEMPLATE.format(**s_facts)
    sys_prompt = summary_system_prompt(template)
    lc, diag = build_report_section_context(
        system_prompt=sys_prompt, facts_block=user,
        profile=state.get("profile"), template=template,
        section="summary", obj_id=task_id,
    )
    _t0 = time.perf_counter()

    def _elapsed() -> int:
        return int((time.perf_counter() - _t0) * 1000)

    try:
        text = await _invoke(llm, lc)
        # 空返回兜底（同 _llm_section 的理由）：摘要空返回会让报告顶部的投资摘要一片空白。
        if not (text or "").strip():
            logger.warning("LLM 摘要返回空内容，回退模板")
            finalize_diag(diag, _elapsed(), error="empty_llm_output")
            return _compose_summary_block(state, fv)
        hits = check_generated(text)
        if not hits:
            finalize_diag(diag, _elapsed())
            return text.strip()
        finalize_diag(diag, _elapsed(), error=f"forbidden_verbs:{','.join(hits)}")
        return _compose_summary_block(state, fv) + "（模型输出含禁用表述，已使用事实摘要。）"
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 摘要失败，回退模板：%s", e)
        finalize_diag(diag, _elapsed(), error=str(e))
        return _compose_summary_block(state, fv) + "（LLM 调用失败，回退模板摘要，数字仍来自本地计算。）"


# ────────────────────────────────────────────────────────────
# 催化剂 + 风险（规则生成；不再 _sanitize 盲替换）
# ────────────────────────────────────────────────────────────
def _compose_catalyst(fv: dict, template: str = "equity_deep") -> str:
    profile = fv.get("profile", {})
    news = fv.get("news", [])
    ind = profile.get("industry", "") or "相关"
    if template == "risk":
        return "持续监测盈利增速、资产负债率、量价结构和流动性变化；任一指标恶化都应重新评估风险暴露。"
    if template == "earnings":
        return "后续重点核验下一报告期营收、利润、毛利率和现金流是否延续当前趋势。"
    if template == "industry":
        return f"{ind}行业需求、政策与竞争格局的新增公开证据，是后续更新行业判断的主要触发条件。"
    parts = [f"{ind}行业景气边际变化与公司经营节奏是核心催化，需结合财务指标持续跟踪。"]
    if news:
        n0 = news[0]
        if isinstance(n0, dict) and n0.get("title"):
            parts.append(f"近期公开信息（「{n0['title'].strip()}」）可作为短期催化剂观察点。")
    parts.append(
        "潜在催化剂（需后续验证）：①行业政策或下游需求边际改善；"
        "②公司新产品/新产能投放或客户结构优化；③技术面关键压力位有效突破。"
    )
    return "".join(parts)


def _compose_risks(risk_flags: list, fv: dict) -> list:
    fins = fv.get("fins", {})
    risks: list[str] = []
    for rf in risk_flags or []:
        if rf:
            risks.append(rf)
    debt_f = facts.to_float(facts.val_str(facts.row_vals(fins, "资产负债率")))
    if debt_f is not None and debt_f >= 60:
        risks.append(f"资产负债率 {debt_f:.1f}% 偏高，关注有息负债结构与偿债压力")
    net_yoy = _latest_pct(fins, "净利润同比", "净利润")
    if net_yoy is not None and net_yoy < 0:
        risks.append(f"最新一期净利润同比 {net_yoy:.1f}%，盈利端承压")
    if not risks:
        risks = [
            "技术指标未触发明确风险信号",
            "需关注宏观环境与行业景气度边际变化",
            "估值受市场情绪与流动性影响可能显著波动",
        ]
    risks.append("市场有风险，投资需谨慎；本报告不构成任何投资建议。")
    return risks


# ────────────────────────────────────────────────────────────
# summary_node：先生成可立即展示的事实摘要
# ────────────────────────────────────────────────────────────
async def summary_node(state: ResearchState) -> dict:
    """在详细段落开始前产出摘要，降低研究任务的首段等待时间。

    1.2.3：默认使用 LLM 撰写摘要（config.report_use_llm_summary=True），
    与四个详细段落口吻统一；LLM 不可用时自动回退模板。
    """
    t0 = time.perf_counter()
    findings = state.get("findings", [])
    # 已有 facts（prefetch 存好的）就在其上补齐，不整个重建——否则会丢掉 normalize_findings
    # 不认识的键（行业研究的 industry_valuation、以及 profile.name）。
    # 个股链路无影响：state.facts 本就是 prefetch_basics 里 normalize 出来的同一份。
    fv = dict(state.get("facts") or {})
    if not fv:
        fv = facts.normalize_findings(findings)
    fv["profile"] = fv.get("profile") or _find(findings, "profile")

    _stream_push("正在撰写投资摘要…")
    if getattr(settings, "report_use_llm_summary", False):
        summary = await _llm_summary(state, fv)
    else:
        summary = _compose_summary_block(state, fv)
    _stream_push("✓ 投资摘要完成")

    return {
        "draft_summary": summary,
        "facts": fv,
        "timings": {"summary_ms": int((time.perf_counter() - t0) * 1000)},
    }


# ────────────────────────────────────────────────────────────
# report_node：模板驱动组装 + 多段并发 LLM 撰写
# ────────────────────────────────────────────────────────────
async def report_node(state: ResearchState) -> dict:
    findings = state.get("findings", [])
    # facts 由 summary_node 归一化；兼容单独调用 report_node 的旧路径。
    fv = dict(state.get("facts") or {})
    if not fv:
        fv = facts.normalize_findings(findings)
    fv["profile"] = fv.get("profile") or _find(findings, "profile")
    template = state.get("template", "equity_deep")
    quote = fv.get("quote", {})
    profile = fv.get("profile", {})
    name = quote.get("name", "") or profile.get("name", "")

    sections_t0 = time.perf_counter()
    summary = state.get("draft_summary") or _compose_summary_block(state, fv)
    # 模板对应各段并发撰写（并发闸在 _invoke 里，避免家用 key 成片 429）。
    specs = _section_specs(template)
    # 逐段真流式（方案A）：串行撰写，每段先发 section_start 让前端建段并置为 streaming，
    # 段内由 _llm_section 逐 token 发 section_token，最后发权威 report_section 收口。
    # 比旧的并行 gather 慢，但前端能一段段、一字字看到生成，而非等整段算完才闪出。
    _stream_push(f"开始撰写 {len(specs)} 段分析（逐段真流式）…")

    results: list = []
    for spec in specs:
        _stream_push({"type": "section_start", "title": spec.title})
        _stream_push(f"正在撰写：{spec.title}…")
        try:
            result = await _llm_section(state, spec, fv)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 段撰写异常：%s", spec.key, exc)
            _stream_push(f"⚠ {spec.title} 撰写异常，回退模板")
            results.append(exc)
            continue
        body, source = result
        # 权威 body 收口：覆盖流式期间已展示的片段（截断/禁用词回退后以此为准）
        _stream_push({"type": "report_section", "title": spec.title, "body": body, "source": source})
        _stream_push(f"✓ {spec.title} 完成")
        results.append(result)
    _stream_push("各段撰写完成，正在组装报告…")

    sections: list[dict] = []
    gen: dict[str, str] = {}
    for spec, res in zip(specs, results):
        if isinstance(res, Exception):
            logger.warning("%s 段 gather 异常，回退模板：%s", spec.key, res)
            # fallback 本身也可能抛异常（如数据源返回畸形 kline，_ma_arrangement 崩）。
            # 这里是"单段独立降级"的最后一道兜底：fallback 再崩，也只让这一段显示占位，
            # 绝不让整个报告因一段挂掉（此前无保护，fallback 一崩 report_node 直接抛异常，
            # 整份报告失败）。
            try:
                body, source = spec.fallback(fv) + "（撰写异常，已回退模板。）", "template"
            except Exception as fb_err:  # noqa: BLE001
                logger.warning("%s 段 fallback 也失败：%s", spec.key, fb_err)
                body, source = f"（{spec.title}生成失败，数据可能异常，本段暂不展开。）", "error"
        else:
            body, source = res
        gen[spec.title] = source
        sections.append({"title": spec.title, "body": body})

    # 证据分层与数据来源（规则生成，不让模型写；如实上报每段的生成来源）
    sections.append({"title": "证据分层与数据来源", "body": _section_evidence_traceback(findings, fv, gen)})

    catalyst = _compose_catalyst(fv, template)
    risks = _compose_risks(state.get("risk_flags") or [], fv)
    sections_ms = int((time.perf_counter() - sections_t0) * 1000)

    citations = state.get("citations", []) or []
    sources = [
        {"title": c.get("title", ""), "url": c.get("url", ""), "date": c.get("date", "")}
        for c in citations
    ] or [
        {"title": "akshare 行情与财务", "url": "akshare", "date": state.get("data_as_of", "")}
    ]

    # 行业研究：normalized_ticker 已是申万行业代码（801735.SI），不能过 to_market_ticker——
    # 那会把 .SI 误当个股后缀改成 .SZ，污染成不存在的股票代码。
    _raw_code = state.get("normalized_ticker") or state.get("code", "")
    _ticker = _raw_code if state.get("is_industry") else to_market_ticker(_raw_code)
    doc = report_tool.build_doc(
        ticker=_ticker,
        name=name,
        template=template,
        summary=summary,
        catalyst=catalyst,
        financials=fv.get("fins", {"years": [], "rows": []}),
        sections=sections,
        risks=risks,
        sources=sources,
        as_of=state.get("data_as_of"),
        quote=fv.get("quote"),
    )

    try:
        html = report_tool.render_html(doc)
    except Exception as e:  # noqa: BLE001
        logger.error("报告渲染失败：%s", e)
        doc["summary"] = (doc.get("summary", "") + f"\n（渲染告警：{e}）").strip()
        html = f"<pre>{doc.get('summary', '')}</pre>"

    doc["html"] = html
    return {
        "draft_report": doc,
        "facts": fv,
        "timings": {"report_sections_ms": sections_ms, "section_count": len(sections)},
    }
