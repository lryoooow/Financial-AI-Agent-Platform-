"""Check generated decimal literals against the facts actually sent to the model."""

import re
import unicodedata
from bisect import bisect_right
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import NamedTuple

_NUMBER = re.compile(r"(?<![A-Za-z0-9])(?:[+-]?\d{1,3}(?:,\d{3})+|[+-]?\d+)(?:\.\d+)?")
_UNIT = re.compile(r"^\s*(万亿元|万亿|亿元|万元|亿|万|元|个百分点|百分点|pct\b|pp\b|%|倍|手|股|点(?!位))", re.I)
_UNIT_ALIASES = {
    "pct": "百分点", "pp": "百分点", "个百分点": "百分点",
    "亿元": "亿", "万元": "万", "万亿元": "万亿",
}
_LABELS = {
    "revenue_yoy": r"(?:营收|营业总?收入|主营(?:业务)?收入|销售收入)[^。！？；;,，、\n\d]{0,4}?(?:同比|增速|增幅|降幅|增长率|增长|下滑|下降)",
    "profit_yoy": r"(?:归母净利(?:润)?|扣非净利(?:润)?|净利(?:润)?|归母利润)[^。！？；;,，、\n\d]{0,4}?(?:同比|增速|增幅|降幅|增长率|增长|下滑|下降)",
    "return_pct": r"涨跌幅|涨幅|跌幅|区间涨跌|(?:相对|较)昨收",
    "price_change": r"涨跌额",
    "prev_close": r"昨收(?:价)?|前收盘",
    "price": r"现价|股价|最新价",
    "avg": r"均价",
    "close": r"收盘(?:价)?",
    "open": r"开盘(?:价)?|今开",
    "high": r"最高(?:价)?",
    "low": r"最低(?:价)?",
    "volume": r"成交量",
    "turnover": r"成交额",
    "market_cap": r"总市值|流通市值|市值",
    "pe": r"市盈率|(?<![A-Za-z])PE(?:\s*\(?(?:TTM|静|动)\)?|\s*TTM)?(?![A-Za-z])",
    "pb": r"市净率|(?<![A-Za-z])PB(?![A-Za-z])",
    "dividend_yield": r"股息率|分红率",
    "roe": r"净资产收益率|(?<![A-Za-z])ROE(?![A-Za-z])",
    "revenue": r"营业总?收入|主营(?:业务)?收入|销售收入|营收",
    "profit": r"(?:归母|扣非)?净利(?:润)?|归母利润",
    "cashflow": r"(?:经营(?:活动)?)?现金流(?:净额)?",
    "gross_margin": r"(?:销售)?毛利率",
    "rd_expense": r"研发(?:费用|投入)",
    "debt": r"(?:资产)?负债率",
    "drawdown": r"最大回撤|回撤",
    "volatility": r"(?:年化)?波动率",
    "sharpe": r"夏普(?:比率)?|(?<![A-Za-z])Sharpe(?![A-Za-z])",
    "var": r"(?<![A-Za-z])VaR(?:\s*\(?\s*9[59]%?\s*\)?)?(?![A-Za-z])",
    "cvar": r"(?<![A-Za-z])CVaR(?:\s*\(?\s*9[59]%?\s*\)?)?(?![A-Za-z])",
    "rsi": r"(?<![A-Za-z])RSI(?:\s*\(\s*\d+\s*\))?(?![A-Za-z])",
    "beta": r"(?<![A-Za-z])Beta(?![A-Za-z])",
    **{f"ma{n}": rf"(?<![A-Za-z])MA\s*{n}(?!\d)" for n in (5, 10, 20, 60)},
}
_METRIC = re.compile("|".join(f"(?P<{key}>{pattern})" for key, pattern in _LABELS.items()), re.I)
_AFTER_METRIC = re.compile(r"[^。！？；;,，、/\n|\d]{0,16}$")
_FINANCIAL_UNIT = {"元", "万", "亿", "万亿", "百分点", "%", "倍", "手", "股", "点"}
_CN_DIGIT = r"[零〇一二两三四五六七八九十壹贰貳叁參肆伍陆陸柒捌玖拾]"
_CN_SCALE = r"[百千万亿佰仟點点]"
_CHINESE_NUMBER = rf"(?<![0-9.]){_CN_DIGIT}(?:{_CN_DIGIT}|{_CN_SCALE})*"
_CHINESE_IDIOM_EXEMPT = re.compile(
    rf"(?<!{_CN_DIGIT})(?![0-9])(?:"
    r"万亿元|千亿元|百亿元|十亿元|千万元|百万元|亿元级|万元级"
    r"|翻了一倍|翻倍|一倍以上|两倍以上|不到一倍|近一倍|逾一倍|超一倍|几个百分点|数个百分点"
    r"|一元化|(?:二元|三元)(?=[\u4e00-\u9fff])(?!(?:左右|上下|以上|以下|以内|多|余|整))"
    r"|(?:二手|两手|两股)(?=[\u4e00-\u9fff])(?!(?:股票|股份|左右|上下|以上|以下|以内|多|余))"
    r"|一手|一股|一点|一季度|二季度|三季度|四季度|一季报|半年报|三季报)"
)
_CHINESE_FINANCIAL = re.compile(
    rf"(?:百分之\s*{_CHINESE_NUMBER}|{_CHINESE_NUMBER}\s*(?:万亿元|亿元|万元|元|倍|个百分点|百分点|手|股))"
)
_TRADITIONAL = str.maketrans("現價開盤漲跌幅淨資產營業萬億點為負債虧損", "现价开盘涨跌幅净资产营业万亿点为负债亏损")
_NEGATIVE_CONTEXT = re.compile(
    r"亏损|净亏|(?<!扭)亏|负值|为负|负增长"
    r"|下降|下滑|(?<!涨)跌幅|下跌|(?<!涨)跌|(?<!升)降|(?<!升)降幅|下挫|回落|回调|下探|走低|低开"
    r"|减少|收窄|缩窄|缩减|萎缩|缩水|减亏|净减|预减"
    r"|回撤|流出|净流出|净卖出|减持|折价|贴水|倒挂"
    r"|较[^，,。！？；\n]{0,8}低"
)
_POSITIVE_FLIP = re.compile(
    r"扭亏|转正|盈利|净赚|实现(?:归母|扣非)?(?:净利|营收|营业)"
    r"|(?<!不)上涨|(?<!跌)涨幅|上升|(?<!负)增长|增幅|增加|回升|反弹|流入|净流入|净买入|增持|溢价"
)
_CLAUSE_SPLIT = re.compile(r"[，,。！？；;!\?\n]")
_SENTENCE_SPLIT = re.compile(r"[。！？；;!\?\n]")
_BOUND_BEFORE_GE = re.compile(r"(?:超(?:过)?|逾|突破|高于|大于|不低于|不少于|站上)\s*$")
_BOUND_BEFORE_LE = re.compile(r"(?:低于|不足|小于|不到|不高于|不超过|跌破|失守)\s*$")
_BOUND_BEFORE_APPROX = re.compile(r"(?:接近|逼近|近|约|大概|大致)\s*$")
_BOUND_AFTER_GE = re.compile(r"^\s*(?:以上|开外|出头|有余)")
_BOUND_AFTER_LE = re.compile(r"^\s*(?:以下|以内)")
_BOUND_AFTER_APPROX = re.compile(r"^\s*(?:左右|上下)")
_BOUND_SUFFIX = re.compile(r"\s*(?:以上|以下|以内|开外|出头|有余|左右|上下)")
# A number whose unit is followed by a closing bracket/sentence terminator is complete even mid-stream.
_CLOSED_AFTER = re.compile(r"^[ \t]*[)）\]】」』\"”']*[ \t]*[。！？；;!?\n]")
_MASK_NON_METRIC = re.compile(
    r"(?:"
    r"(?<![0-9A-Za-z.])(?:19|20)\d{2}\s*[-/.年]\s*(?:0?[1-9]|1[0-2])(?!\d)(?!\.\d+\s*(?:%|倍|元|亿|万|手|股|个?百分点))"
    r"(?:\s*[-/.月]\s*(?:0?[1-9]|[12]\d|3[01])(?!\d)\s*日?)?"
    r"|(?<![0-9A-Za-z.])\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|(?<![0-9A-Za-z.])\d{1,2}\s*[-~—–至到]\s*\d{1,2}\s*月"
    r"|(?<![0-9A-Za-z.])(?:(?:19|20)\d{2}\s*)?Q[1-4](?:\s*[-~—–至到]\s*Q?[1-4])?"
    r"|(?<![0-9A-Za-z.])(?:(?:19|20)\d{2}\s*)?H[12]"
    r"|(?<![0-9A-Za-z.])(?:19|20)\d{2}\s*(?:年(?:度|报|初|末|底|上半年|下半年)?|一季报|中报|半年报|三季报)"
    r"|(?<![0-9A-Za-z.])(?:19|20)\d{2}\s*[-~—–至到]\s*(?:19|20)\d{2}\s*年?"
    r"|(?<![0-9A-Za-z.])(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?![0-9.])"
    r"|(?<![0-9A-Za-z.])[123]\s*/\s*[23]\s*期"
    r"|(?:任何|哪怕|动辄|区区)\s*\d{1,2}\s*[-~—–至到]\s*\d{1,2}\s*(?:元|%|个?百分点)"
    r"|\[\d{1,3}\]|【\d{1,3}】"
    r"|(?<![0-9A-Za-z])(?:(?:SH|SZ|BJ)[:.]?\d{6}|\d{6}\.(?:SH|SZ|BJ|HK|SI))(?![0-9A-Za-z])"
    r"|(?<![A-Za-z])(?:MA|EMA|SMA|WMA|RSI|KDJ|MACD|WR|CCI|BIAS|BOLL|ATR|OBV|ROC|DMI|VR)\s*\(\s*\d{1,3}(?:\s*[,/]\s*\d{1,3})*\s*\)"
    r"|(?<![A-Za-z])(?:MA|EMA|SMA|RSI)\s*(?:5|10|14|20|30|60|120|250)(?!\d)"
    r"|(?<![A-Za-z])C?VaR\s*(?:\(\s*9[59]%?\s*\)|\s*9[59]%?)"
    r"|(?<![0-9A-Za-z.])9[59]%\s*(?:置信(?:度|水平)(?:下)?\s*)?(?:的\s*)?C?VaR"
    r"|(?<![A-Za-z0-9])[A-Za-z]{1,8}-\d{1,6}(?![0-9.])"
    r"|(?:^|[\n\r；;])\s*(?:\d{1,2}[.)、]|[（(]\d{1,2}[)）])"
    r")",
    re.I,
)
_WINDOW_DAY = re.compile(
    r"^\s*(?:个)?(?:交易日|自然日|工作日|日|天|周|个月|月|年|季度|季|期|阶段|线|根|档|只|支|家|款|种|类|条|项|次|轮|步|章|节|名|位|人|例|起|份|篇|页|号|版|代|批|组|套|座|台|辆|片|粒|瓶|盒|针|剂)"
)
_FIELD_KEYS = {
    "prevClose": "prev_close",
    "change": "price_change",
    "changePct": "return_pct",
    "rel_pct": "return_pct",
    "pe_ttm": "pe",
    "pe_static": "pe",
    "max_drawdown_pct": "drawdown",
    "max_drawdown": "drawdown",
    "annualized_volatility": "volatility",
    "volatility_annual": "volatility",
    "volatility_pct": "volatility",
    "turnover_yuan": "turnover",
    "amount": "turnover",
    "marketCap": "market_cap",
    "floatMarketCap": "market_cap",
    "total_mv": "market_cap",
    "circ_mv": "market_cap",
    "sharpe_ratio": "sharpe",
    "var_95": "var",
    "var_95_pct": "var",
    "cvar_95": "cvar",
    "cvar_95_pct": "cvar",
}
_UNSCALED_RATIO_KEYS = {"max_drawdown", "annualized_volatility", "volatility_annual", "var_95", "cvar_95"}
_FIELD_DEFAULT_UNITS: dict[str, tuple[str, ...]] = {
    "price": ("元",),
    "price_change": ("元",),
    "open": ("元",),
    "high": ("元",),
    "low": ("元",),
    "close": ("元",),
    "prev_close": ("元",),
    "avg": ("元",),
    "ma5": ("元",),
    "ma10": ("元",),
    "ma20": ("元",),
    "ma60": ("元",),
    "pe": ("倍",),
    "pb": ("倍",),
    "dividend_yield": ("%",),
    "return_pct": ("%",),
    "roe": ("%",),
    "revenue_yoy": ("%",),
    "profit_yoy": ("%",),
    "gross_margin": ("%",),
    "debt": ("%",),
    "volatility": ("%",),
    "drawdown": ("%",),
    "var": ("%",),
    "cvar": ("%",),
    "volume": ("手", "股"),
}
_SCALABLE_FIELDS = {"volume", "turnover", "revenue", "profit", "cashflow", "rd_expense", "market_cap"}


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_TRADITIONAL)


def _mask_preserving_length(text: str) -> str:
    return _MASK_NON_METRIC.sub(lambda m: " " * len(m.group(0)), text)


def _rounded_variants(value: Decimal) -> set[Decimal]:
    out = {value}
    for places in ("0.1", "0.01", "0.001", "0.0001"):
        try:
            out.add(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))
        except InvalidOperation:
            pass
    if abs(value) >= Decimal("10"):
        try:
            out.add(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except InvalidOperation:
            pass
    if value == value.to_integral_value():
        out.add(value.to_integral_value())
    return out


def _has_neg_context(nearby_wide: str) -> bool:
    sent_tail = _SENTENCE_SPLIT.split(nearby_wide)[-1]
    clause_tail = _CLAUSE_SPLIT.split(sent_tail)[-1]
    clause_matches = list(_NEGATIVE_CONTEXT.finditer(clause_tail))
    if clause_matches:
        return not _POSITIVE_FLIP.search(clause_tail[clause_matches[-1].end():])
    sent_matches = list(_NEGATIVE_CONTEXT.finditer(sent_tail))
    if sent_matches:
        return not _POSITIVE_FLIP.search(sent_tail[sent_matches[-1].end():])
    return False


def _detect_bound_kind(nearby: str, after_unit: str) -> str | None:
    if _BOUND_AFTER_GE.match(after_unit):
        return "ge"
    if _BOUND_AFTER_LE.match(after_unit):
        return "le"
    if _BOUND_BEFORE_GE.search(nearby):
        return "ge"
    if _BOUND_BEFORE_LE.search(nearby):
        return "le"
    if _BOUND_AFTER_APPROX.match(after_unit) or _BOUND_BEFORE_APPROX.search(nearby):
        return "approx"
    return None


def _observations(
    text: str,
    *,
    streaming: bool = False,
    financial_only: bool = False,
    mask_dates: bool = False,
    detailed: bool = False,
):
    scan_text = _mask_preserving_length(text) if mask_dates else text
    for match in _NUMBER.finditer(scan_text):
        try:
            after = text[match.end():]
            unit_match = _UNIT.match(after)
            after_unit = after[unit_match.end():] if unit_match else after
            if (
                streaming
                and (len(text) - match.end() <= 6 or len(after_unit) <= 4 or after == ".")
                and len(match[0]) < 16
                and not _CLOSED_AFTER.match(after_unit)
            ):
                continue
            unit = unit_match[1].lower() if unit_match else ""
            unit = _UNIT_ALIASES.get(unit, unit)
            if mask_dates and not unit and "." not in match[0]:
                if _WINDOW_DAY.match(after):
                    continue
                int_val = int(match[0].replace(",", ""))
                if 1990 <= int_val <= 2099:
                    continue
            nearby = text[max(0, match.start() - 32):match.start()]
            nearby_wide = text[max(0, match.start() - 56):match.start()]
            metrics = list(_METRIC.finditer(nearby))
            last = metrics[-1] if metrics else None
            after_metric = nearby[last.end():] if last else ""
            field = (
                last.lastgroup
                if last
                and _AFTER_METRIC.fullmatch(after_metric)
                and (after_metric.count(")") + after_metric.count("）"))
                <= (after_metric.count("(") + after_metric.count("（"))
                else None
            )
            if financial_only and unit not in _FINANCIAL_UNIT and not field and "." not in match[0]:
                continue
            neg_context = _has_neg_context(nearby_wide)
            val = Decimal(match[0].replace(",", ""))
            if detailed:
                bound_kind = _detect_bound_kind(nearby, after_unit) if "." not in match[0] else None
                end = match.end() + (unit_match.end() if unit_match else 0)
                yield _Obs(val, unit, field, neg_context, bound_kind, match.start(), end, match[0])
            else:
                yield val, unit, field, neg_context
        except (InvalidOperation, ValueError):
            continue


class _Obs(NamedTuple):
    value: Decimal
    unit: str
    field: str | None
    neg: bool
    bound: str | None
    start: int
    end: int
    raw: str


class NumericIssue(NamedTuple):
    """An unverifiable number span, in the coordinates of the ORIGINAL text passed to ``locate``."""

    start: int
    end: int
    reason: str


_UNIT_SCALE = {"": Decimal(1), "元": Decimal(1), "万": Decimal(10) ** 4, "亿": Decimal(10) ** 8, "万亿": Decimal(10) ** 12}
_RATIO_MULTIPLIER_CUE = re.compile(r"(?:高出|多出|增长了?|增加了?|提升了?|上涨了?)\s*$")
_SHARE_CUE = re.compile(r"(?:占|占比|比重|贡献)")


def _unit_class(unit: str) -> tuple[str, Decimal] | None:
    """Map a unit to (dimension, scale) so operands of different magnitudes stay comparable."""
    if unit in _UNIT_SCALE:
        return "amount", _UNIT_SCALE[unit]
    if unit in {"倍", "%", "手", "股", "点"}:
        return unit, Decimal(1)
    return None


def _decimals(raw: str) -> int:
    return len(raw.split(".", 1)[1]) if "." in raw else 0


def _close_enough(computed: Decimal, shown: Decimal, raw: str, bound: str | None) -> bool:
    """Accept the displayed value only if it is the honest rounding of ``computed`` (or a faithful bound on it)."""
    mag_c, mag_s = abs(computed), abs(shown)
    if bound == "ge":
        return mag_s <= mag_c < mag_s + max(Decimal(1), mag_s * Decimal("0.1"))
    if bound == "le":
        return mag_s - max(Decimal(1), mag_s * Decimal("0.1")) < mag_c <= mag_s
    step = Decimal(1).scaleb(-_decimals(raw))
    tol = max(step / 2, mag_c * Decimal("0.005"))
    if bound == "approx":
        tol = max(tol, step)
    return abs(mag_c - mag_s) <= tol


def _norm_with_map(text: str) -> tuple[str, list[int]]:
    """Normalize while keeping an index map back to the original string (NFKC can change lengths)."""
    whole = _normalized(text)
    if len(whole) == len(text):
        return whole, list(range(len(text) + 1))
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        piece = _normalized(ch)
        out.append(piece)
        idx.extend([i] * len(piece))
    idx.append(len(text))
    return "".join(out), idx


def metric_values(artifact: dict) -> dict[str, list]:
    """Collect structured fact values under their display metric."""
    fields: dict[str, list] = {}

    def add_field(name: str, val, raw_key: str = "") -> None:
        if val is None or isinstance(val, bool):
            return
        fields.setdefault(name, []).append(val)
        if raw_key in _UNSCALED_RATIO_KEYS and isinstance(val, (int, float)) and abs(val) <= 5:
            fields[name].append(f"{Decimal(str(val)) * Decimal(100)}%")

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        points = value.get("data")
        if isinstance(points, list) and len(points) > 1 and isinstance(points[0], dict) and isinstance(points[-1], dict):
            f_close, l_close = points[0].get("close"), points[-1].get("close")
            if isinstance(f_close, (int, float)) and isinstance(l_close, (int, float)) and f_close > 0:
                add_field("return_pct", f"{(l_close - f_close) / f_close * 100.0:.2f}%")
        row_label = value.get("name")
        row = _METRIC.search(_normalized(row_label)) if isinstance(row_label, str) else None
        if row:
            if isinstance(value.get("values"), list):
                for item in value["values"]:
                    add_field(row.lastgroup, item)
            elif "value" in value and isinstance(value.get("value"), (int, float, str)):
                add_field(row.lastgroup, value["value"])
        for key, item in value.items():
            name = _FIELD_KEYS.get(key, key)
            if name not in _LABELS and isinstance(key, str):
                key_match = _METRIC.fullmatch(_normalized(key))
                if key_match:
                    name = key_match.lastgroup
            if name in _LABELS and isinstance(item, (int, float, str)) and not isinstance(item, bool):
                add_field(name, item, raw_key=key)
            elif key not in {"provenance", "news", "events"}:
                visit(item)

    visit(artifact)
    return fields


class NumberEvidence:
    def __init__(self, facts: str, fields: dict[str, list] | None = None, *, strict_fields: bool = False) -> None:
        observations = list(_observations(_normalized(facts), mask_dates=True))
        self._allowed: set[tuple[Decimal, str]] = set()
        self._allowed_values: set[Decimal] = set()
        self._unit_values: set[Decimal] = set()
        self._fields: dict[str, set[Decimal]] = {}
        self._strict_fields = strict_fields

        def register_pair(val: Decimal, unit: str, field_name: str | None = None) -> None:
            extra_units = _FIELD_DEFAULT_UNITS.get(field_name or "", ()) if not unit else ()
            for variant in _rounded_variants(val):
                self._allowed.add((variant, unit))
                self._allowed_values.add(variant)
                if unit:
                    self._unit_values.add(variant)
                for u in extra_units:
                    self._allowed.add((variant, u))
                    self._unit_values.add(variant)
                if field_name:
                    self._fields.setdefault(field_name, set()).add(variant)

        def register_scaled(val: Decimal, unit: str, field_name: str | None = None) -> None:
            register_pair(val, unit, field_name)
            wan_threshold = (
                Decimal("1000")
                if (unit in {"元", "手", "股"} or field_name in _SCALABLE_FIELDS)
                else Decimal("10000")
            )
            if unit in {"", "元", "手", "股"} and abs(val) >= wan_threshold:
                if not (unit == "" and field_name is None and Decimal("1900") <= abs(val) <= Decimal("2100")):
                    register_pair(val / Decimal("10000"), "万", field_name)
                if unit in {"", "元", "股"} and abs(val) >= Decimal("100000"):
                    register_pair(val / Decimal("100000000"), "亿", field_name)
                    if abs(val) >= Decimal("100000000000"):
                        register_pair(val / Decimal("1000000000000"), "万亿", field_name)
            elif unit == "万" and abs(val) >= Decimal("100"):
                register_pair(val / Decimal("10000"), "亿", field_name)
            elif unit == "亿":
                register_pair(val * Decimal("10000"), "万", field_name)
                if abs(val) >= Decimal("100"):
                    register_pair(val / Decimal("10000"), "万亿", field_name)
            elif unit == "万亿":
                register_pair(val * Decimal("10000"), "亿", field_name)

        for value, unit, obs_field, obs_neg in observations:
            register_scaled(value, unit, obs_field)
            if obs_neg and value > 0:
                register_scaled(-value, unit, obs_field)

        if fields is not None:
            for field, values in fields.items():
                self._fields.setdefault(field, set())
                for raw in values:
                    if raw is None:
                        continue
                    for number, unit, _, obs_neg in _observations(_normalized(str(raw))):
                        register_scaled(number, unit, field)
                        if obs_neg and number > 0:
                            register_scaled(-number, unit, field)

    def _matches_value(self, value: Decimal, neg_context: bool) -> Decimal | None:
        if value in self._allowed_values:
            return value
        if neg_context and (-value) in self._allowed_values:
            return -value
        return None

    def _matches_approx_bound(
        self,
        value: Decimal,
        unit: str,
        field: str | None,
        neg_context: bool,
        bound_kind: str | None,
    ) -> bool:
        if not bound_kind or (unit not in _FINANCIAL_UNIT and not (unit == "" and field == "rsi")):
            return False
        if abs(value) < Decimal("10") or value != value.to_integral_value() or int(abs(value)) % 10 != 0:
            return False
        targets = [value] + ([-value] if neg_context and value > 0 else [])
        allowed_field = (self._fields.get(field) or None) if field else None
        for cand_val, cand_unit in self._allowed:
            if cand_unit != unit:
                continue
            if allowed_field is not None and cand_val not in allowed_field:
                continue
            for t in targets:
                if not ((cand_val > 0 and t > 0) or (cand_val < 0 and t < 0)):
                    continue
                mag_c, mag_t = abs(cand_val), abs(t)
                diff = abs(mag_c - mag_t)
                if bound_kind == "ge" and mag_c >= mag_t and diff <= Decimal("10"):
                    return True
                if bound_kind == "le" and mag_c <= mag_t and diff <= Decimal("10"):
                    return True
                if bound_kind == "approx" and diff <= Decimal("10"):
                    return True
        return False

    def _verify(self, o: _Obs) -> str | None:
        """Return the failure reason for one observation, or None when it is grounded in the evidence."""
        value, unit, field, neg_context, bound_kind = o.value, o.unit, o.field, o.neg, o.bound
        matched_val = self._matches_value(value, neg_context)
        if matched_val is None:
            if self._matches_approx_bound(value, unit, field, neg_context, bound_kind):
                return None
            return "unsupported_number"
        if field and self._strict_fields:
            allowed_field = self._fields.get(field, set())
            if value not in allowed_field and not (neg_context and (-value) in allowed_field):
                if not self._matches_approx_bound(value, unit, field, neg_context, bound_kind):
                    return "unsupported_metric"
        if unit and matched_val != Decimal("0") and matched_val in self._unit_values:
            if (value, unit) not in self._allowed and not (neg_context and (-value, unit) in self._allowed):
                if not self._matches_approx_bound(value, unit, field, neg_context, bound_kind):
                    return "unsupported_unit"
        return None

    @staticmethod
    def _effective_unit(o: _Obs) -> str:
        if o.unit:
            return o.unit
        defaults = _FIELD_DEFAULT_UNITS.get(o.field or "", ())
        return defaults[0] if defaults else ""

    def _derivable(self, target: _Obs, operands: list[_Obs], text: str) -> bool:
        """Honest arithmetic on two grounded numbers written next to it (ratio, premium, share, pp, difference)."""
        t_unit = target.unit
        if t_unit not in {"倍", "%", "百分点"} and _unit_class(t_unit) is None:
            return False
        if len(operands) < 2:
            return False
        lead = text[max(0, target.start - 12):target.start]
        multiplier_cue = bool(_RATIO_MULTIPLIER_CUE.search(lead))
        t_cls = _unit_class(t_unit)
        for i, a in enumerate(operands):
            for b in operands[i + 1:]:
                ua, ub = self._effective_unit(a), self._effective_unit(b)
                ca, cb = _unit_class(ua), _unit_class(ub)
                if ca is None or cb is None or ca[0] != cb[0]:
                    continue
                va, vb = abs(a.value) * ca[1], abs(b.value) * cb[1]
                candidates: list[Decimal] = []
                for x, y in ((va, vb), (vb, va)):
                    if t_unit == "倍":
                        if y:
                            candidates.append(x / y)
                            if multiplier_cue:
                                candidates.append(x / y - 1)
                    elif t_unit == "%":
                        if ca[0] == "%" or not y:
                            continue
                        candidates.append((x - y) / y * 100)
                        candidates.append(x / y * 100)
                    elif t_unit == "百分点":
                        if ca[0] == "%":
                            candidates.append(x - y)
                    elif t_cls is not None and t_cls[0] == ca[0]:
                        candidates.append((x - y) / t_cls[1])
                        candidates.append((x + y) / t_cls[1])
                if any(_close_enough(c, target.value, target.raw, target.bound) for c in candidates):
                    return True
        return False

    def _scan(
        self, text: str, *, streaming: bool, financial_only: bool,
    ) -> tuple[list[tuple[int, int, str]], list[tuple[int, int, str]], list[int]]:
        norm, idx = _norm_with_map(text)
        chinese: list[tuple[int, int, str]] = []
        masked = _CHINESE_IDIOM_EXEMPT.sub(lambda m: "□" * len(m.group(0)), norm)
        for m in _CHINESE_FINANCIAL.finditer(masked):
            if streaming and m.end() > len(norm) - 6:
                continue
            chinese.append((idx[m.start()], idx[m.end()], "unsupported_number"))

        observations = list(_observations(
            norm, streaming=streaming, financial_only=financial_only, mask_dates=True, detailed=True,
        ))
        sentence_ends = [m.end() for m in _SENTENCE_SPLIT.finditer(norm)]
        grounded: list[_Obs] = []
        failing: list[tuple[_Obs, str]] = []
        for o in observations:
            reason = self._verify(o)
            if reason:
                failing.append((o, reason))
            else:
                grounded.append(o)
        numeric: list[tuple[int, int, str]] = []
        for o, reason in failing:
            sid = bisect_right(sentence_ends, o.start)
            operands = [g for g in grounded if sid - 1 <= bisect_right(sentence_ends, g.start) <= sid]
            if self._derivable(o, operands, norm):
                continue
            end = o.end
            suffix = _BOUND_SUFFIX.match(norm, end)
            if suffix:
                end = suffix.end()
            numeric.append((idx[o.start], idx[end], reason))
        return chinese, numeric, sentence_ends

    def locate(self, text: str, *, streaming: bool = False, financial_only: bool = False) -> list[NumericIssue]:
        """Every unverifiable number span in ``text`` (original coordinates), sorted by position."""
        chinese, numeric, _ = self._scan(text, streaming=streaming, financial_only=financial_only)
        return sorted(NumericIssue(*item) for item in chinese + numeric)

    def check(self, text: str, *, streaming: bool = False, financial_only: bool = False) -> list[str]:
        chinese, numeric, _ = self._scan(text, streaming=streaming, financial_only=financial_only)
        if chinese:
            return [chinese[0][2]]
        if numeric:
            return [numeric[0][2]]
        return []
