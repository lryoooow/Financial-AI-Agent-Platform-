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
        if s.endswith("万"):
            return float(s[:-1]) / 10000.0
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
    """倍数渲染（PB 等非负倍数）：0 与缺失同义，均为 MISSING（不带"倍"）。"""
    return measure(v, " 倍", d, zero_missing=True)


def pe_str(v: Any, d: int = 1) -> str:
    """市盈率渲染：0/缺失 → MISSING；负值（亏损）→ 显式标注「亏损（PE 为负）」，避免写「-18.5 倍」误导模型。"""
    f = to_float(v)
    if f is None or f == 0.0:
        return MISSING
    if f < 0:
        return f"亏损（PE 为负，{f:.{d}f}）"
    return f"{f:.{d}f} 倍"


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


def row_vals(fins: dict, name) -> list:
    """取 financials.rows 里指定 name 的 values 列表。

    v1.6.0：name 支持别名列表（str 或 [str,...]）—— 生产侧行名与消费方查询名
    曾错配（「扣非净利润」vs「净利润」），别名机制让新旧数据形状都能命中。
    """
    names = [name] if isinstance(name, str) else list(name or [])
    for r in fins.get("rows", []) or []:
        if r.get("name") in names:
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


def profit_turnaround_tag(cur_val: Any, prev_val: Any) -> str:
    """识别跨零轴/亏损期的盈利状态定性，避免负基数百分比增速单独出现引起误读。"""
    cur, prev = to_float(cur_val), to_float(prev_val)
    if cur is None or prev is None:
        return ""
    if prev < 0 < cur:
        return "扭亏为盈"
    if prev > 0 > cur:
        return "由盈转亏"
    if prev < 0 and cur < 0:
        if cur > prev:
            return "同比减亏"
        if cur < prev:
            return "亏损扩大"
    return ""


def latest_profit_turnaround(fins: dict) -> str:
    """结合同口径基准（年报相邻列或中期 yoy_base）返回最新一期净利润扭亏/减亏定性。"""
    vals = row_vals(fins, ["净利润", "扣非净利润"])
    if not vals:
        return ""
    if latest_pair_comparable(fins) and len(vals) >= 2:
        return profit_turnaround_tag(vals[-1], vals[-2])
    base = (fins or {}).get("yoy_base") or {}
    return profit_turnaround_tag(vals[-1], base.get("净利润"))


def delta_pp(vals: list) -> Optional[float]:
    """最新一年相对上一年的差值（百分点），用于比率类指标。"""
    if len(vals) < 2:
        return None
    cur, prev = to_float(vals[-1]), to_float(vals[-2])
    if cur is None or prev is None:
        return None
    return cur - prev


def _period_tail(fins: dict) -> tuple[str, str]:
    """(最新列报告期, 前一列报告期)；无报告期信息返回空串。"""
    periods = [str(p or "") for p in ((fins or {}).get("periods") or [])]
    cur = periods[-1] if periods else ""
    prev = periods[-2] if len(periods) >= 2 else ""
    return cur, prev


def latest_pair_comparable(fins: dict) -> bool:
    """最新两列是否同一报告期口径（审计 A01）。

    年报 vs 年报、中报 vs 上年中报才可比；「2026 中报 vs 2025 年报」不可比 ——
    相邻两列直接相减会把半年数与全年数的差值冒充同比。无报告期信息的旧数据
    （历史缓存/旧报告）按原语义视为可比，不改变既有行为。
    """
    cur, prev = _period_tail(fins)
    if not cur:
        return True
    if not prev:
        return cur.endswith("-12-31")
    return cur[5:] == prev[5:]


def latest_is_interim(fins: dict) -> bool:
    cur, _ = _period_tail(fins)
    return bool(cur) and not cur.endswith("-12-31")


def delta_pp_latest(fins: dict, name) -> Optional[float]:
    """比率类指标（ROE/毛利率/资产负债率）最新一期的同比变动（百分点），口径感知。

    可比 → 相邻两列相减；最新列是中期口径 → 只与 yoy_base（上年同期）相减；
    拿不到同期基准 → None（不展示同比，绝不跨口径相减）。
    """
    vals = row_vals(fins, name)
    if latest_pair_comparable(fins):
        return delta_pp(vals)
    names = [name] if isinstance(name, str) else list(name or [])
    base = (fins or {}).get("yoy_base") or {}
    base_v = next((to_float(base.get(n)) for n in names if to_float(base.get(n)) is not None), None)
    cur = to_float(vals[-1]) if vals else None
    if cur is None or base_v is None:
        return None
    return cur - base_v


def yoy_pct_latest(fins: dict, name) -> Optional[float]:
    """绝对值序列（营收/净利润）推算的同比，口径感知：不可比时返回 None。"""
    if not latest_pair_comparable(fins):
        return None
    return yoy_pct(row_vals(fins, name))


def financial_yoy(fins: dict, direct: str, base: str) -> Optional[float]:
    reported = row_vals(fins, direct)
    value = to_float(reported[-1]) if reported else None
    return value if value is not None else yoy_pct_latest(fins, base)


def pp_str_latest(fins: dict, name) -> str:
    p = delta_pp_latest(fins, name)
    return f"{p:+.1f}pct" if p is not None else MISSING


def annual_series(fins: dict, vals: list) -> list:
    """趋势判断用的同口径序列：最新列是中期口径时去掉它（只看年报列的首末）。"""
    if vals and latest_is_interim(fins) and not latest_pair_comparable(fins):
        return list(vals[:-1])
    return list(vals)


def latest_labeled(fins: dict, name) -> str:
    """最新一期取值；最新列是中期口径时附报告期标注（如「8.6%（2026一季报，非全年口径）」）。

    审计 A01 补全：ROE 等流量型比率在一季报/中报口径下只有全年的几分之一，不标注就交给
    估值段/摘要，会被读成全年水平（PB 与季度 ROE 错配）。
    """
    v = val_str(row_vals(fins, name))
    if v != MISSING and latest_is_interim(fins):
        years = (fins or {}).get("years") or []
        label = years[-1] if years else "中期"
        return f"{v}（{label}，非全年口径）"
    return v


def yoy_str(vals: list) -> str:
    """同比增速（%）的渲染，缺失 → MISSING。"""
    y = yoy_pct(vals)
    return f"{y:+.1f}%" if y is not None else MISSING


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
        f"动态 PE {pe_str(quote.get('pe'), 1)}、PB {mult_str(quote.get('pb'), 1)}；"
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
    # 别名兼容：v1.5 及更早数据行名为「扣非净利润」
    net = row_vals(f, ["净利润", "扣非净利润"])
    roe = row_vals(f, "ROE")
    gm = row_vals(f, "毛利率")
    debt = row_vals(f, "资产负债率")
    # v1.6.0：同比改读专用增速行（上游直接给的真值），不再从绝对值序列二次计算 ——
    # 旧做法在绝对值行缺失时连带同比一起丢，而增速行明明有数。
    rev_yoy = row_vals(f, ["营收同比", "营业收入同比"])
    net_yoy = row_vals(f, ["净利润同比", "归母净利润同比"])
    latest_year = years[-1] if years else "—"
    turn_tag = latest_profit_turnaround(f)
    net_yoy_display = f"{val_str(net_yoy)}，{turn_tag}" if turn_tag and val_str(net_yoy) != MISSING else (turn_tag or val_str(net_yoy))
    lines = [
        f"【财务指标】最新一期（{latest_year}）",
        f"营业收入 {val_str(rev)}（同比 {val_str(rev_yoy)}）；净利润 {val_str(net)}（同比 {net_yoy_display}）。",
        f"ROE {val_str(roe)}（同比变动 {pp_str_latest(f, 'ROE')}）；毛利率 {val_str(gm)}（同比变动 {pp_str_latest(f, '毛利率')}）；"
        f"资产负债率 {val_str(debt)}（同比变动 {pp_str_latest(f, '资产负债率')}）。",
        "注：营收/净利润括注为上游增速行的同比（%）；ROE/毛利率/资产负债率括注为同比变动（pct=百分点）。",
    ]
    if latest_is_interim(f):
        base = (f.get("yoy_base") or {}).get("period") or ""
        lines.append(
            f"口径：最新一期为中期报告（非全年），与其余年报列不可直接比较；"
            + (f"同比变动的基准为上年同期（{base}）。" if base else "上年同期数据缺失，比率类同比变动不展示。")
        )
    return "\n".join(lines)


def render_news_facts(news: list, web: list = None) -> str:
    """新闻/网页证据：引文用「」包裹并标注来源，不经 sanitize。"""
    from ..core.guardrails.untrusted import fence_external, looks_like_instruction

    lines = ["【资讯与公开信息证据】"]
    has_any = False
    for n in (news or [])[:3]:
        if not isinstance(n, dict):
            continue
        title = (n.get("title") or "").strip()
        if not title or looks_like_instruction(title):
            continue
        src = str(n.get("source") or "").strip()
        tm = str(n.get("time") or "").strip()
        meta = f"{src} · {tm}" if (src and tm) else (src or tm or "公开来源")
        lines.append(f"· {fence_external(f'「{title}」', src or 'news', limit=240)}（来源：{meta}）")
        has_any = True
    for w in (web or [])[:3]:
        if not isinstance(w, dict) or not (w.get("title") or "").strip() or looks_like_instruction(w["title"]):
            continue
        w_src = str(w.get("source") or "").strip()
        w_tm = str(w.get("time") or "").strip()
        w_meta = f"{w_src} · {w_tm}" if (w_src and w_tm) else (w_src or w_tm or "网页")
        headline = f"「{w['title'].strip()}」"
        lines.append(f"· {fence_external(headline, w_src or 'web', limit=240)}（来源：{w_meta}）")
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


def render_chat_facts(quote: dict, news: list = None) -> str:
    """对话链路事实块：行情 + 资讯精简版（供对话 system 注入），末尾单份【约束】。"""
    blocks = [render_quote_facts(quote)]
    if news:
        blocks.append(render_news_facts(news, None))
    return compose_facts(*blocks)


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
