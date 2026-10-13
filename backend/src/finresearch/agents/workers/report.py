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
import re
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from ...config import settings
from ...llm import build_llm, is_reasoning_model, reasoning_profile
from ...tools import facts
from ...tools import report as report_tool
from ...tools.context import build_report_context, build_report_section_context, finalize_diag
from ...tools.entity import to_market_ticker
from ...tools.text import strip_md
from ..prompts import (
    FORBIDDEN_COMPOUNDS,
    FORBIDDEN_VERBS,
    QUOTE_SPAN,
    SUMMARY_USER_TEMPLATE,
    report_system_prompt,
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
_REPORT_TIMEOUT = float(getattr(settings, "report_timeout_s", 180.0))


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


def _scrub_protected(text: str) -> str:
    """剔除检测豁免域：「」引文（提示词强制原样引用）与数据口径名词搭配。

    豁免域里的禁用词是描述第三方事实（"北向资金净买入 3.2 亿"），不是对读者
    发出的指令——曾经不分层，A 股新闻标题高频含这类词，整篇报告被子串检测误杀。
    """
    text = QUOTE_SPAN.sub("", text)
    for compound in FORBIDDEN_COMPOUNDS:
        text = text.replace(compound, "")
    return text


def check_generated(text: str) -> List[str]:
    """检测 LLM 产出是否命中"裸"禁用词（引文与数据口径豁免后），返回命中列表。

    旧 _sanitize 盲替换会污染引文、产生"不建议建议关注（偏多）"等语义崩坏；
    此处只检测。命中的处置见 sanitize_generated：收口时对裸词做映射替换。
    """
    if not text:
        return []
    scrubbed = _scrub_protected(text)
    return [w for w in FORBIDDEN_VERBS if w in scrubbed]


# 裸指令动词的方向性改写（2 字换 2-4 字，映射表不含任何数字字符；
# 引文/口径搭配已在 _scrub_protected 阶段豁免，replace 不会碰到它们——
# 数字零幻觉纪律不受影响：数字、来源、「」内容物理上不在替换域内）
_VERB_REWRITES = {
    "买入": "看多", "卖出": "看空", "加仓": "增配", "减仓": "减配",
    "止损": "控制回撤", "清仓": "了结持仓", "抄底": "低位布局",
    "建仓": "逐步布局", "满仓": "重仓参与",
}


def sanitize_generated(text: str) -> tuple[str, List[str]]:
    """收口时把裸指令动词替换为方向性表述；返回 (新文本, 残余命中)。

    只动 _scrub_protected 后仍裸露的词；替换后二次校验，理论上必空
    （残余即模型自造「」包裹指令词的逃逸，由调用方落模板兜底）。
    """
    hits = check_generated(text)
    if not hits:
        return text, []
    for word in set(hits):
        text = text.replace(word, _VERB_REWRITES[word])
    return text, check_generated(text)


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

    blocks: list[str] = []
    rev_yoy = _latest_pct(fins, "营收同比", "营业收入")
    net_yoy = _latest_pct(fins, "净利润同比", "净利润")
    rev_l, net_l = facts.val_str(rev), facts.val_str(net)
    if rev_yoy is not None or net_yoy is not None:
        line = (
            f"【成长性】最新一期（{facts.latest_period_label(fins)}）实现营业收入 {rev_l}"
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
# 整篇一次撰写：流式段切分
# ────────────────────────────────────────────────────────────
class _SectionSplitter:
    """把整篇报告的 token 流实时切成分段事件。

    为什么要切：前端按 section_start / section_token(title) / report_section(title) 渲染，
    这套契约是分段串行时代定的。整篇一次生成后仍沿用它，前端就一行都不用改——
    而且因为中间没有停顿，视觉上正是"一篇文章连续写下来"。

    切法：按行缓冲，把行内容剥掉装饰后与已知段标题比对，命中就切段。

    **必须容忍多种标题写法**。提示词要求写【标题】，但模型不一定照办——写成
    "## 技术面分析"、"**技术面分析**"、"1. 技术面分析" 或裸标题都很常见。
    如果只认【】一种，模型换个写法就一段都切不出来，整篇被判为空 → 静默降级回分段撰写，
    用户看到的还是老样子的"一段段等"。这里宽容匹配，就是为了让主路径真的走得通。

    - 只认已知标题（剥装饰后精确相等），模型自己发挥的小标题按正文处理，不会切出幽灵段
    - 标题行本身不进正文（前端另有 title 字段渲染，正文里再来一遍是重复）
    - 跨 chunk 的半截标题（"【技术" + "面分析】"）由行缓冲天然处理：
      没遇到 \\n 就一直攒着，不会误判

    每个 token 都要过 on_token 回调（推 SSE），所以刻意不做整段缓冲。
    """

    # 标题行的常见装饰：md 标题符/强调符、书名号方括号、序号前缀、行尾冒号。
    #
    # 行尾**不剥句号**：标题不会以句号结尾，正文会。剥了的话正文里一句
    # "……从技术面分析。" 会被归一成"技术面分析"，整句被当标题吞掉。
    _DECOR = re.compile(r"^[\s#*_>\-–—]*[【\[（(]?\s*|\s*[】\]）)]?[\s*_:：]*$")
    _ORDER = re.compile(r"^(?:第?\s*[0-9一二三四五六七八九十]+\s*[.、)）:：]?\s*(?:部分|章|节)?\s*)")

    SUMMARY_TITLE = "投资摘要"  # 特殊段：token 以 summary_token 事件发出（前端打字机）

    def __init__(self, titles: List[str], on_event: Callable[[dict], None]) -> None:
        self._titles = list(titles)
        self._by_norm = {self._normalize(t): t for t in titles}
        self._on_event = on_event
        self._line = ""            # 当前未闭合的行
        self._current: Optional[str] = None
        self._bodies: dict[str, list[str]] = {}
        self._started: set[str] = set()
        self._completed: set[str] = set()  # 经标题边界切过段的：确定完整

    @property
    def heading_closed(self) -> frozenset[str]:
        """经标题边界收尾的段（后续又出现了新标题）——流中断时这些段仍完整可信；
        close() 冲刷的尾段在流中断场景不可信（可能写了一半）。"""
        return frozenset(self._completed)

    def _emit_text(self, text: str) -> None:
        if not text or self._current is None:
            return
        self._bodies.setdefault(self._current, []).append(text)
        if self._current == self.SUMMARY_TITLE:
            # 摘要段走独立事件通道：前端逐字追加到摘要卡（打字机效果）
            self._on_event({"type": "summary_token", "text": text})
        else:
            self._on_event({"type": "section_token", "title": self._current, "text": text})

    def _open(self, title: str) -> None:
        if self._current is not None and self._current != title:
            self._completed.add(self._current)  # 标题边界：上一段确定写完
        self._current = title
        if title not in self._started:
            self._started.add(title)
            self._bodies.setdefault(title, [])
            # 摘要段不发 section_start：前端不会在正文区建空段（done 时 doc.sections
            # 不含摘要 → 空段瞬间消失 = "内容闪现后消失"的根因）
            if title != self.SUMMARY_TITLE:
                self._on_event({"type": "section_start", "title": title})

    @classmethod
    def _normalize(cls, line: str) -> str:
        """剥掉标题行的装饰，只留标题本身。用于宽容匹配各种写法。"""
        s = cls._DECOR.sub("", line.strip())
        return cls._ORDER.sub("", s).strip()

    def _match_heading(self, line: str) -> Optional[str]:
        """行是否是某个已知段标题。长行直接排除，避免正文句子误判成标题。"""
        stripped = line.strip()
        if not stripped or len(stripped) > 40:
            return None
        return self._by_norm.get(self._normalize(stripped))

    def _adopt_single(self) -> bool:
        """单段模板下，把无归属的正文认领给唯一的那一段。

        industry / earnings / risk 三个模板都只有一段，正文归属没有歧义。模型漏写标题时
        若不认领，整篇会被判为空 → 白白降级重跑一次（多花一轮调用与等待）。
        多段模板不能这么做：分不清哪段是哪段，宁可降级重来。
        """
        if self._current is None and len(self._titles) == 1:
            self._open(self._titles[0])
            return True
        return self._current is not None

    def _flush_line(self, *, with_newline: bool) -> None:
        """处理一个已闭合的行：是标题就切段，否则作为正文发出去。"""
        line, self._line = self._line, ""
        title = self._match_heading(line)
        if title:
            self._open(title)
            return
        # 段首的空行不发（标题与正文之间的空行会让正文平白多一个换行）
        if self._current is None and not (line.strip() and self._adopt_single()):
            return
        if not self._bodies.get(self._current) and not line.strip():
            return
        self._emit_text(line + ("\n" if with_newline else ""))

    def feed(self, token: str) -> None:
        """按 chunk 处理，**不要拆成字符**。

        只有「行」才可能是标题，所以按 \\n 切开即可；行内内容整块外发。
        早先这里写成 `for ch in token`，正文里绝大多数字符都不是标题前缀，于是每个字符
        立刻单独发一帧——实测帧数放大到字符级（平均每帧 1.00 字符，是模型 chunk 数的 7.8 倍）。
        这些小帧要逐个穿过 langgraph custom writer → JSON → SSE → 前端解析 → React re-render，
        五层叠加后就表现为"流式卡住，然后一次性把整段刷出来"。
        """
        if not token:
            return
        parts = token.split("\n")
        for index, part in enumerate(parts):
            self._line += part
            if index < len(parts) - 1:
                self._flush_line(with_newline=True)
            elif part and not self._maybe_heading() and self._adopt_single():
                # 行未闭合但已不可能是标题：整块外发，别攒着等换行（长段落会卡住不动）
                self._emit_text(self._line)
                self._line = ""

    def _maybe_heading(self) -> bool:
        """当前未闭合行是否还可能长成标题（决定要不要继续攒着不外发）。

        宽容匹配下不能只比前缀：'## 技术' 剥装饰后是 '技术'，得看它是不是某个标题的前缀。
        """
        stripped = self._line.strip()
        if not stripped:
            return True   # 纯空白：可能是标题行的缩进
        if len(stripped) > 40:
            return False
        norm = self._normalize(stripped)
        if not norm:
            return True   # 只有装饰符（"## "、"【"），还看不出来
        return any(t.startswith(norm) for t in self._by_norm)

    def close(self) -> dict[str, str]:
        """收尾：冲掉残留行，返回 {标题: 正文}（未出现的段不在里面）。"""
        if self._line:
            self._flush_line(with_newline=False)
        return {
            title: "".join(parts).strip()
            for title, parts in self._bodies.items()
            if "".join(parts).strip()
        }


def _whole_facts_block(specs: List[SectionSpec], fv: dict) -> str:
    """把各段事实块按段标题合并成整篇的事实块。

    保留段边界（每段以【标题】起头）有两个作用：模型知道哪些数字归哪一段用；
    以及 _truncate_facts_by_segment 超预算时能按段整块丢弃，不会把某段截半截。
    """
    blocks = []
    for spec in specs:
        if not spec.has_data(fv):
            continue
        blocks.append(f"【{spec.title}·事实】\n{spec.build_facts(fv)}")
    return "\n\n".join(blocks)


async def _llm_report(
    state: ResearchState, specs: List[SectionSpec], fv: dict
) -> dict[str, tuple[str, str]]:
    """一次调用写完整篇，边流边切段。返回 {标题: (正文, 来源)}。

    没有字符上限、没有断流——篇幅由用户选的思考档位经 max_tokens 控制。
    整篇失败（模型不可用/超时/禁用词）返回空 dict，由调用方降级到并行分段。
    """
    template = state.get("template", "equity_deep")
    task_id = state.get("task_id") or state.get("normalized_ticker", "")
    usable = [s for s in specs if s.has_data(fv)]
    if not usable:
        return {}
    # 摘要并入整篇流：作为第一个分段，token 以 summary_token 逐字流出，
    # 摘要写完标题一切正文无缝续上——一条流从摘要首字到报告结尾不断
    splitter_titles = [_SectionSplitter.SUMMARY_TITLE] + [s.title for s in usable]

    llm_cfg = state.get("llm_config") or {}
    _level, max_tokens, depth_spec = reasoning_profile(llm_cfg.get("reasoning_effort"))
    llm = build_llm(streaming=True, max_tokens=max_tokens, override=state.get("llm_config"))
    if llm is None:
        return {}

    sys_prompt = report_system_prompt(
        [(s.key, s.title) for s in usable], template, depth_spec
    )
    # 摘要事实块拼在最前（模型按提纲先写摘要再写正文）
    summary_facts = _compose_summary_block(state, fv)
    whole_facts = ("【投资摘要·事实】\n" + summary_facts + "\n\n" + _whole_facts_block(usable, fv)) if summary_facts else _whole_facts_block(usable, fv)
    lc, diag = build_report_context(
        system_prompt=sys_prompt,
        facts_block=whole_facts,
        profile=state.get("profile"),
        template=template,
        obj_id=task_id,
    )
    _t0 = time.perf_counter()
    splitter = _SectionSplitter(splitter_titles, _stream_push)
    model_name = str((state.get("llm_config") or {}).get("model") or "")
    gen_budget = 120.0 if is_reasoning_model(model_name) else _REPORT_TIMEOUT
    try:
        async with asyncio.timeout(gen_budget + 120.0):  # 外层：排队不计入生成预算
            async with _LLM_GATE:
                async with asyncio.timeout(gen_budget):
                    async for chunk in llm.astream(lc):
                        tok = _extract_token(chunk)
                        if not tok:
                            continue
                        # 禁用词不再中止流：引文/数据口径已在 check_generated 分层豁免，
                        # 裸指令词留到收口用 sanitize_generated 局部替换（曾有"北向资金
                        # 净买入"引文命中 → 整篇 7 秒作废 → 降级再命中 → 模板零 token
                        # 的事故链）。流式体验优先，合规在收口硬保证。
                        splitter.feed(tok)
    except Exception as e:  # noqa: BLE001
        finalize_diag(diag, int((time.perf_counter() - _t0) * 1000), error=str(e))
        # 异常也不丢弃已完成的干净段落：只保留经标题边界切过段的（heading_closed），
        # close() 冲刷的尾段可能写了一半，不可信——交由调用方按"缺失段"补写。
        partial = splitter.close()
        reliable = splitter.heading_closed
        kept = {}
        for title, body in partial.items():
            if title not in reliable:
                continue
            body, residual = sanitize_generated(strip_md(body))
            if not residual:
                kept[title] = (body, "llm")
        logger.warning("整篇撰写失败，保留 %d 个已完成段落，仅缺失段补写：%s", len(kept), e)
        return kept

    bodies = splitter.close()
    if not bodies:
        finalize_diag(diag, int((time.perf_counter() - _t0) * 1000), error="empty_llm_output")
        logger.warning("整篇撰写返回空内容，转分段补写")
        return {}
    finalize_diag(diag, int((time.perf_counter() - _t0) * 1000))
    # 提示词是软约束，清洗才是硬保证：模型仍可能吐 **加粗**，而前端是纯文本渲染。
    # 裸指令词在此统一替换（引文/口径豁免域不受影响）；替换后仍脏的极少数段落
    # 不返回，由调用方按"缺失段"单独重写。
    result = {}
    for title, body in bodies.items():
        body, residual = sanitize_generated(strip_md(body))
        if residual:
            logger.warning("整篇段落「%s」替换后仍含禁用表述，转单段重写", title)
            continue
        result[title] = (body, "llm")
    return result


# ────────────────────────────────────────────────────────────
# 单段 LLM 撰写（命中禁用词→重写→仍命中回退模板并标注）
#
# 2.0.3 起这是**降级路径**：主路径是 _llm_report 一次写完整篇。整篇失败时各段并行
# 重试一次，仍失败才落规则模板。
#
# 篇幅同样跟随思考档位（与主路径一致）——降级不该让用户看到一份莫名其妙变短的报告。
# 保留的 report_section_max_chars 只当安全阀：单段真跑飞时截断，正常篇幅碰不到。
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
    llm_cfg = state.get("llm_config") or {}
    _level, level_tokens, depth_spec = reasoning_profile(llm_cfg.get("reasoning_effort"))
    sys_prompt = section_system_prompt(spec.key, template, depth_spec)

    # temperature 统一 1.0：o3 / QwQ 等推理模型拒绝非默认 temperature，
    # 设 0.7 会让这些模型整段静默回退模板。
    # streaming=True：逐 token 吐给前端（见下方 astream 循环）。
    # max_tokens 按档位分摊到单段：整篇预算除以段数，下限 480（旧的固定值，保证不比以前差）。
    section_tokens = max(
        int(getattr(settings, "report_section_max_tokens", 480)),
        level_tokens // max(1, len(_section_specs(template))),
    )
    llm = build_llm(
        streaming=True,
        max_tokens=section_tokens,
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

    # 逐 token 从模型流出，每个安全 token 立即经 section_token 推给前端，同时累积成整段正文。
    #
    # cap 是安全阀不是篇幅限制：从本段的 token 预算换算（1 token 约 1.5 中文字，×4 留足余量），
    # 正常输出碰不到它，只在模型陷入循环时兜底。此前它是 min(spec.max_chars=320, 420)=320，
    # 那是个实打实的篇幅限制——每段刚写到 200 字就被 break 掉，末尾还补个"…"。
    cap = max(int(getattr(settings, "report_section_max_chars", 420)), section_tokens * 4)
    parts: list[str] = []
    acc = ""
    # 推理模型（DeepSeek-R1/QwQ/o 系/gpt-5）首字前的思考阶段可远超 30s：
    # 生成预算按首字宽限放宽；闸排队时间不计入（外层 +120s 只防无限排队）。
    model_name = str(llm_cfg.get("model") or "")
    gen_budget = 90.0 if is_reasoning_model(model_name) else _SECTION_TIMEOUT
    try:
        async with asyncio.timeout(gen_budget + 120.0):
            async with _LLM_GATE:
                async with asyncio.timeout(gen_budget):
                    async for chunk in llm.astream(lc):
                        tok = _extract_token(chunk)
                        if not tok:
                            continue
                        if not acc and not tok.strip():
                            continue  # 跳过前导纯空白 token，避免空段先闪一段空白再被模板覆盖
                        acc += tok
                        parts.append(tok)
                        _stream_push({"type": "section_token", "title": spec.title, "text": tok})
                        if len(acc) >= cap:
                            break  # 到确定性字符上限即停，省 token
        text = "".join(parts)
        # 空/纯空白兜底：模型在并发限流、内容审查、max_tokens 被 reasoning 吃满等情况下偶发返回空。
        # 空返回当撰写失败、回退规则模板（body 必非空），如实标注来源 template；否则渲染成空白章节。
        if not text.strip():
            logger.warning("%s LLM 流式返回空内容，回退模板", spec.key)
            finalize_diag(diag, _elapsed(), error="empty_llm_output")
            return spec.fallback(fv), "template"
        text, truncated = _bound_generated_text(text, cap)
        # 收口统一替换裸指令词（引文/口径豁免）；替换后仍脏 = 模型自造「」逃逸，落模板兜底
        text, residual = sanitize_generated(text)
        if residual:
            finalize_diag(diag, _elapsed(), error="forbidden_verbs")
            logger.warning("%s 段替换后仍含禁用表述，回退模板", spec.key)
            return spec.fallback(fv) + "（模型输出含禁用表述，已使用事实模板。）", "template"
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
    """优先使用数据源直接提供的同比，旧数据再由绝对值序列计算。

    levels 重算前校验最近两期报告期同形：中报对上年年报直接相除
    是 -50% 式系统性错误，口径不一致时返回 None 走 MISSING。
    """
    direct = facts.to_float(facts.val_str(facts.row_vals(fins, direct_name)))
    if direct is not None:
        return direct
    if not facts.periods_comparable(fins):
        return None
    return facts.yoy_pct(facts.row_vals(fins, base_name))


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
        "rev_yoy": facts.pct(_latest_pct(fins, "营收同比", "营业收入"), 1),
        "net_profit": facts.val_str(net),
        "net_yoy": facts.pct(_latest_pct(fins, "净利润同比", "净利润"), 1),
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

    if getattr(settings, "report_use_llm_summary", False):
        summary = await _llm_summary(state, fv)
    else:
        summary = _compose_summary_block(state, fv)

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
    specs = _section_specs(template)

    # 整篇一次撰写（摘要并入）：一次 LLM 调用从摘要首字到报告结尾连续流式。
    _stream_push("开始撰写报告…")
    whole = await _llm_report(state, specs, fv)

    # 摘要从整篇流中提取（模型按提纲先写摘要段）
    SUMMARY_KEY = _SectionSplitter.SUMMARY_TITLE
    draft_summary = ""
    if SUMMARY_KEY in whole:
        draft_summary = whole[SUMMARY_KEY][0].strip()
        del whole[SUMMARY_KEY]  # 不作为普通 section 渲染
        # 权威收口：替换语义兜住 sanitize/strip_md 差异 + 断线重连回放
        _stream_push({"type": "research_summary", "text": draft_summary, "name": state.get("code", ""), "ticker": state.get("normalized_ticker", "")})
    else:
        draft_summary = _compose_summary_block(state, fv)
        _stream_push({"type": "research_summary", "text": draft_summary, "name": state.get("code", ""), "ticker": state.get("normalized_ticker", "")})

    results: dict[str, tuple[str, str]] = {}
    for spec in specs:
        if spec.title in whole:
            results[spec.title] = whole[spec.title]
        elif not spec.has_data(fv):
            results[spec.title] = (f"（{spec.title}所需数据不足，本报告不展开该部分。）", "insufficient")

    # 整篇成功段的权威收口：整篇流结束即逐段发出（不等补写段）
    for spec in specs:
        if spec.title in whole:
            body, source = whole[spec.title]
            _stream_push({"type": "report_section", "title": spec.title, "body": body, "source": source})

    missing = [s for s in specs if s.title not in results]
    if missing:
        if whole:
            _stream_push("部分内容重新撰写中…")

        async def _run_section(spec: SectionSpec) -> tuple[str, str]:
            # 只对真正重写的段发 section_start（前端清空该段旧正文），已完成的段永不闪空
            _stream_push({"type": "section_start", "title": spec.title})
            try:
                body, source = await _llm_section(state, spec, fv)
            except Exception as e:  # noqa: BLE001
                logger.warning("%s 段撰写异常，回退模板：%s", spec.key, e)
                try:
                    body, source = spec.fallback(fv) + "（撰写异常，已回退模板。）", "template"
                except Exception as fb_err:  # noqa: BLE001
                    logger.warning("%s 段 fallback 也失败：%s", spec.key, fb_err)
                    body, source = f"（{spec.title}生成失败，数据可能异常，本段暂不展开。）", "error"
            # 完成即收口：不等兄弟段（曾有 gather 拦栅——一个慢段把所有已完成段的
            # 权威收口全部拖住，前端表现"正文迟迟整块出现"）
            _stream_push({"type": "report_section", "title": spec.title, "body": body, "source": source})
            return body, source

        gathered = await asyncio.gather(*(_run_section(s) for s in missing))
        for spec, res in zip(missing, gathered):
            results[spec.title] = res

    sections: list[dict] = []
    gen: dict[str, str] = {}
    for spec in specs:
        # results 全量覆盖：整篇段 / insufficient / _run_section 兜底后的补写段，
        # 异常兜底已在 _run_section 内完成（fallback 再崩也有占位段），这里不会再缺。
        body, source = results[spec.title]
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
        summary=draft_summary or summary,
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
