"""事实块渲染器：确定性、缺失显式、带 provenance。

铁律落地（数字零幻觉）：
- 所有渲染函数对 None / 0（price 这类不可能为 0 的字段）/ "" / "nan" 一律输出 MISSING，
  绝不把缺失值伪装成事实，绝不把 0.0 当"未获取"塞进事实块。
- 事实块尾部固定追加一行约束，要求模型不得推算或填补。
- 引文（新闻/网页标题）用「」包裹并标注来源，永不经过 sanitize。

本模块是 report.py 与 context.py 的公共事实底座：_to_float / _fmt / _row_vals /
_yoy_pct / _delta_pp 自 report.py 迁入此处，report.py 改为 import 复用，消除重复定义。
"""

from __future__ import annotations

from typing import Any, Optional

# 缺失统一标记（不是 0、不是空、不是"—"，而是显式的"未获取"）
MISSING = "—（未获取）"

# 事实块尾部约束。由 compose_facts() 在装配时**只追加一次**——
# 早期实现让每个 render_* 各追加一份，拼两个块就出现两次【约束】，且第二块内容跑到第一份约束之后。
FACT_FOOTER = (
    "【约束】以上为结构化事实，正文中所有数字必须严格引用此处，"
    "不得以任何形式推算、估算或填补；事实块未出现的数字一律不得写入正文。"
)

# 不可能为 0 的字段：0 视为缺失（行情快照里 pe=0/pb=0/price=0 是"未获取"而非"真的为 0"）。
# changePct / change 可合理为 0（平盘），故不在其中——它们由 quote_usable() 整体把关。
_ZERO_IS_MISSING = {"price", "pe", "pb", "high", "low", "open", "prevClose", "marketCap"}


# ────────────────────────────────────────────────────────────
# 公共数值工具（自 report.py 迁入，消除重复定义）
# ────────────────────────────────────────────────────────────
def to_float(v: Any) -> Optional[float]:
    """把字符串值解析为 float，失败/缺失返回 None。

    state 里数值常见形态：1234.56、"15.0%"、"50.00亿"、"—"。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
            return None
        return f
    s = str(v).strip()
    if not s or s in ("—", "nan", "NaN", "None"):
        return None
    try:
        if s.endswith("%"):
            return float(s[:-1])
        if s.endswith("亿"):
            return float(s[:-1])
        return float(s.replace(",", ""))
    except Exception:  # noqa: BLE001
        return None


def fmt(v: Any, d: int = 2) -> str:
    """裸数字格式化：None / 不可解析 → MISSING。

    注意：本函数**不做零值判定**（0 是合法数字，如涨跌幅平盘）。
    需要"0 即缺失"语义的字段（价格/PE/PB）请用 measure(..., zero_missing=True)，
    或先经 quote_usable() 判定整个快照是否可用。
    """
    f = to_float(v)
    if f is None:
        return MISSING
    return f"{f:.{d}f}"


def measure(v: Any, unit: str = "", d: int = 2, zero_missing: bool = False) -> str:
    """单位感知渲染：有值→"12.30 元"；缺失→MISSING（不带单位）。

    单位与 MISSING 粘连（"—（未获取） 元"/"—（未获取）%"）是本模块要消灭的一类瑕疵，
    所以单位只在有值时才拼接。
    """
    f = to_float(v)
    if f is None or (zero_missing and f == 0.0):
        return MISSING
    s = f"{f:.{d}f}"
    return f"{s}{unit}" if unit else s


def pct(v: Any, d: int = 2, signed: bool = True) -> str:
    """百分比渲染："+1.20%" / "-0.35%"；缺失 → MISSING（不带 %）。"""
    f = to_float(v)
    if f is None:
        return MISSING
    return f"{f:+.{d}f}%" if signed else f"{f:.{d}f}%"


def mult_str(v: Any, d: int = 1) -> str:
    """倍数渲染（PE/PB）：0 与缺失同义，均为 MISSING（不带"倍"）。"""
    return measure(v, " 倍", d, zero_missing=True)


def quote_usable(quote: Optional[dict]) -> bool:
    """行情快照整体是否可用。

    铁律执行点：price 缺失或取数报错 ⇒ 整个快照不可信 ⇒ 所有派生字段一律 MISSING。
    早期实现逐字段判零，于是失败快照的 changePct=0.0 会作为"涨跌幅 0.00%"混进事实块，
    而 system prompt 又命令模型"严格引用事实块"——等于指挥模型写编造值。
    """
    if not quote:
        return False
    if (quote.get("provenance") or {}).get("error"):
        return False
    p = to_float(quote.get("price"))
    return p is not None and p > 0


def compose_facts(*blocks: str) -> str:
    """把若干事实块拼成一个 human 消息，并在末尾**只追加一次**【约束】。"""
    body = "\n".join(b.strip() for b in blocks if b and b.strip())
    return f"{body}\n{FACT_FOOTER}" if body else FACT_FOOTER


def val_or_missing(v: Any, field: str = "") -> str:
    """值缺失判断：None/''/'nan' → MISSING；对 _ZERO_IS_MISSING 字段 0 也算缺失。"""
    if v is None:
        return MISSING
    s = str(v).strip()
    if not s or s in ("—", "nan", "NaN", "None"):
        return MISSING
    if field in _ZERO_IS_MISSING:
        f = to_float(v)
        if f is not None and f == 0.0:
            return MISSING
    return s


def row_vals(fins: dict, name: str) -> list:
    """取 financials.rows 里指定 name 的 values 列表。"""
    for r in fins.get("rows", []) or []:
        if r.get("name") == name:
            return list(r.get("values", []) or [])
    return []


def val_str(vals: list, idx: int = -1) -> str:
    """安全取某一年（默认最新年）的原始字符串；缺失 → MISSING。"""
    if not vals:
        return MISSING
    try:
        v = vals[idx]
        if v in (None, ""):
            return MISSING
        s = str(v).strip()
        return MISSING if s in ("—", "nan", "NaN", "None") else s
    except IndexError:
        return MISSING


def yoy_pct(vals: list) -> Optional[float]:
    """最新一年相对上一年的同比变化（百分比，非百分点）。

    对营收/净利润：返回相对增速（如 +12.3 表示 +12.3%）。
    对 ROE/毛利率/资产负债率：调用方应改用 delta_pp（百分点）。
    """
    if len(vals) < 2:
        return None
    cur, prev = to_float(vals[-1]), to_float(vals[-2])
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100.0


def delta_pp(vals: list) -> Optional[float]:
    """最新一年相对上一年的差值（百分点），用于比率类指标。"""
    if len(vals) < 2:
        return None
    cur, prev = to_float(vals[-1]), to_float(vals[-2])
    if cur is None or prev is None:
        return None
    return cur - prev


def yoy_str(vals: list) -> str:
    """同比增速（%）的渲染，缺失 → MISSING。"""
    y = yoy_pct(vals)
    return f"{y:+.1f}%" if y is not None else MISSING


_PERIOD_LABELS = {"03-31": "一季报", "06-30": "中报", "09-30": "三季报", "12-31": "年报"}


def _period_tail(period: str) -> str:
    """报告期日期 '2026-06-30' → 月日 '06-30'；异常输入返回 ''。"""
    p = str(period or "").strip()
    return p[5:10] if len(p) >= 10 and p[4] == "-" else ""


def periods_comparable(f: dict) -> bool:
    """最近两期报告期是否同形（可由 levels 重算同比）。

    中报（H1 累计）对上年年报、三季报对年报直接相除都是 -50% 式系统性错误。
    无 periods 信息（旧形状数据）时保守返回 True 维持旧行为。
    """
    periods = [str(p) for p in (f.get("periods") or [])]
    if len(periods) < 2:
        return True
    cur_tail, prev_tail = _period_tail(periods[-1]), _period_tail(periods[-2])
    if cur_tail and prev_tail and cur_tail != prev_tail:
        return False
    return True


def yoy_from_fins(f: dict, base_name: str, direct_name: str) -> str:
    """同比渲染：优先接口原生同比增长率列（对比口径天然正确）。

    原生列缺失才退回 levels 重算，且重算前校验最近两期报告期同形；
    口径不一致时 MISSING，宁可缺失也不编数。
    """
    direct = to_float(val_str(row_vals(f, direct_name)))
    if direct is not None:
        return f"{direct:+.1f}%"
    if not periods_comparable(f):
        return MISSING
    return yoy_str(row_vals(f, base_name))


def latest_period_label(f: dict) -> str:
    """最新报告期的披露文案：'2026-06-30（中报）'；无 periods 时退回年份。"""
    periods = [str(p) for p in (f.get("periods") or [])]
    tail = _period_tail(periods[-1]) if periods else ""
    if tail:
        label = _PERIOD_LABELS.get(tail)
        return f"{periods[-1]}（{label}）" if label else periods[-1]
    years = f.get("years") or []
    return str(years[-1]) if years else "—"


def pp_str(vals: list) -> str:
    """同比变动（百分点）的渲染，缺失 → MISSING。"""
    p = delta_pp(vals)
    return f"{p:+.1f}pct" if p is not None else MISSING


# ────────────────────────────────────────────────────────────
# 事实块渲染器
# ────────────────────────────────────────────────────────────
def render_quote_facts(quote: dict) -> str:
    """行情事实块：带数据时点与来源；快照不可用时如实披露失败原因，绝不渲染派生数字。"""
    if not quote:
        return "【当前行情】" + MISSING + "（行情接口未返回数据）"
    prov = quote.get("provenance") or {}
    source = prov.get("source") or "—"
    err = prov.get("error")
    name = quote.get("name") or "—"
    industry = quote.get("industry") or "—"

    if not quote_usable(quote):
        # 整个快照不可信：价位/涨跌幅/估值一律不渲染，并把失败原因交代清楚
        reason = err or "行情接口未返回有效价格"
        return "\n".join([
            f"【当前行情】{name} · {industry}",
            f"最新价 {MISSING}；涨跌幅 {MISSING}；动态 PE {MISSING}、PB {MISSING}；总市值 {MISSING}。",
            f"取数状态：失败（来源 {source}，原因：{reason}）。本轮不得使用任何行情数字。",
        ])

    as_of = quote.get("as_of") or quote.get("trading_date") or "—"
    lines = [
        f"【当前行情】{name} · {industry}",
        f"最新价 {measure(quote.get('price'), ' 元', 2, zero_missing=True)}"
        f"（涨跌幅 {pct(quote.get('changePct'), 2)}）；"
        f"动态 PE {mult_str(quote.get('pe'), 1)}、PB {mult_str(quote.get('pb'), 1)}；"
        f"总市值 {val_or_missing(quote.get('marketCap'), 'marketCap')}。",
        f"数据时点：{as_of}；来源：{source}。",
    ]
    return "\n".join(lines)


def render_financial_facts(fins: dict) -> str:
    """财务事实块：同比(%)与百分点(pct)语义严格区分。"""
    if not fins or not (fins.get("rows") or fins.get("financials")):
        return "【财务指标】" + MISSING + "（财务接口未返回数据）"
    # 兼容 research 节点产出 {financials:{...}} 与直接 {...} 两种形状
    f = fins.get("financials", fins) if isinstance(fins, dict) else {}
    years = f.get("years", []) or []
    rev = row_vals(f, "营业收入")
    net = row_vals(f, "净利润")
    roe = row_vals(f, "ROE")
    gm = row_vals(f, "毛利率")
    debt = row_vals(f, "资产负债率")
    latest_year = latest_period_label(f)
    lines = [
        f"【财务指标】最新一期（{latest_year}）",
        f"营业收入 {val_str(rev)}（同比 {yoy_from_fins(f, '营业收入', '营收同比')}）；"
        f"净利润 {val_str(net)}（同比 {yoy_from_fins(f, '净利润', '净利润同比')}）。",
        f"ROE {val_str(roe)}（同比变动 {pp_str(roe)}）；毛利率 {val_str(gm)}（同比变动 {pp_str(gm)}）；"
        f"资产负债率 {val_str(debt)}（同比变动 {pp_str(debt)}）。",
        "注：营收/净利润括注为同比增速（%）；ROE/毛利率/资产负债率括注为同比变动（pct=百分点）。",
    ]
    return "\n".join(lines)


def render_news_facts(news: list, web: list = None) -> str:
    """新闻/网页证据：引文用「」包裹并标注来源，不经 sanitize。"""
    lines = ["【资讯与公开信息证据】"]
    has_any = False
    for n in (news or [])[:3]:
        if not isinstance(n, dict):
            continue
        title = (n.get("title") or "").strip()
        if not title:
            continue
        src = n.get("source") or n.get("time") or "公开来源"
        lines.append(f"· 「{title}」（来源：{src}）")
        has_any = True
    for w in (web or [])[:3]:
        if not isinstance(w, dict) or not (w.get("title") or "").strip():
            continue
        lines.append(f"· 「{w['title'].strip()}」（来源：{w.get('source') or '网页'}）")
        has_any = True
    if not has_any:
        lines.append(MISSING + "（未检索到可引用的公开资讯）")
    lines.append("引文为原始标题，不得改写、删改或断章取义。")
    return "\n".join(lines)


def render_indicator_facts(indicators: list) -> str:
    """技术指标事实块：列出各指标值与信号，缺失显式标注。"""
    if not indicators:
        return "【技术指标】" + MISSING + "（技术指标未计算成功）"
    lines = ["【技术指标】"]
    for it in indicators:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        val = val_or_missing(it.get("value"))
        sig = it.get("signal") or "中性"
        lines.append(f"· {it['name']} {val}（{sig}）")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────
# findings 形状归一化（消除 report.py 里 3 次出现的形状兜底）
# ────────────────────────────────────────────────────────────
def normalize_findings(findings: list) -> dict:
    """把 entity/research/market 节点的 findings 归一为稳定形状 dict。

    消掉 report.py 里反复出现的 `_find(f,"indicators").get("indicators")
    or _find(f,"indicators").get("data", [])` 这类形状兜底。
    返回 {quote, fins, indicators(list), kline(list), profile, news, web, entity, web_configured}。
    """
    by_key = {}
    for f in findings or []:
        if isinstance(f, dict) and f.get("key"):
            by_key[f["key"]] = f

    ind_finding = by_key.get("indicators", {})
    indicators = ind_finding.get("indicators")
    if not isinstance(indicators, list):
        indicators = ind_finding.get("data") or []

    fins_finding = by_key.get("financials", {})
    fins = fins_finding.get("financials") or {"years": [], "rows": []}

    web_finding = by_key.get("web_search", {})

    return {
        "entity": by_key.get("entity", {}),
        "profile": by_key.get("profile", {}),
        "quote": (by_key.get("quote", {}).get("quote") or {}),
        "kline": (by_key.get("kline", {}).get("kline") or []),
        "indicators": indicators,
        "fins": fins,
        "news": (by_key.get("news", {}).get("items") or []),
        "web": (web_finding.get("items") or []),
        "web_configured": bool(web_finding.get("configured")),
    }
