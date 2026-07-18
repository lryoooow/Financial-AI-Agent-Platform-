"""deepagents 工具层：把既有纯函数包成 @tool，供 agent 自主调用。

数字零幻觉在 agent 下的实现方式变了，但约束本身没变：
旧管线里 LLM 拿不到取数决定权（节点直接调 akshare，见 workers/research.py），
现在 agent 自己决定调什么，所以约束改由三件事共同保证——

1. 工具是唯一数值来源：这里只暴露取数工具，不挂任何计算/执行工具，
   agent 没有能力自己算出一个数（execute 工具在 chat_agent.py 里被显式摘除）。
2. 每个返回值都带 provenance（tools.make_provenance），agent 据此注明数据时点与来源。
3. 取数失败返回显式 error 字段，而不是空值/零值——让 agent 明确知道"这个数拿不到"，
   照 prompts.AGENT_CHAT_COMPLIANCE 的要求如实说明，而不是把缺失当成 0。

所有底层函数都是阻塞的 akshare 调用，一律经 _timed_call（asyncio.to_thread + 超时 +
绝不抛异常）。超时值沿用旧管线各节点的实测值，不另拍脑袋。

docstring 就是模型看到的工具说明书，改动前先想清楚模型会怎么读。
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from .._utils import _timed_call
from ...tools import make_provenance
from ...tools.entity import resolve_entity as _resolve_entity
from ...tools.industry import search_industry as _search_industry
from ...tools.fundamentals import get_company_profile as _get_company_profile
from ...tools.fundamentals import get_financial_indicators as _get_financial_indicators
from ...tools.market import compute_indicators_from_kline as _compute_indicators_from_kline
from ...tools.market import get_kline as _get_kline
from ...tools.market import get_realtime_quote as _get_realtime_quote
from ...tools.news import search_news as _search_news
from ...tools.search import web_search as _web_search

logger = logging.getLogger(__name__)

# 超时对齐 research.py / market.py 各节点的实测值。resolve 比旧对话链路的 1.5s 放宽——
# 那 1.5s 是为了"第一个 token 不能卡住"，而 agent 调 resolve 时用户已经在等工具了，
# 宁可等到也别查不着。
#
# financials 15s：实测冷启动稳定 10.6s（akshare 串行抓 7 页），旧值 8s 卡在数据到达之前，
# 每次冷缓存必丢财务。详见 workers/research.py 里同一处的注释。
# 其余各源实测 profile 3.3s / news 2.1s / web 0.3s，余量充足。
#
# industry 20s：实测冷启动 11.3s（一次拉申万一级/二级/三级三张表，各约 4s 串行）。
# 同样是"超时必须设在实测冷启动之上"——照财务指标那个教训来，别再自己掐自己。
# 拉回来按 30 分钟缓存（行业估值日频更新），热路径 0ms。
_T_RESOLVE = 3.0
_T_QUOTE = 8.0
_T_KLINE = 8.0
_T_PROFILE = 6.0
_T_FINANCIALS = 15.0
_T_NEWS = 6.0
_T_WEB = 5.0
_T_INDUSTRY = 20.0


def _fail(source: str, endpoint: str, error: str) -> dict:
    """取数失败的统一返回：显式 error + provenance。

    绝不返回空 dict——空值会被模型读成"没有数据"进而自行脑补，
    显式 error 才能让它照约束如实说"取不到"。
    """
    return {
        "error": error,
        "provenance": make_provenance(source=source, endpoint=endpoint, error=error),
    }


def _normalize(res):
    """统一失败语义：把既有工具的 provenance.error 提升为顶层 error。

    既有纯函数失败时大多不抛异常，而是优雅降级——例如 tools/search.py:web_search 未配置
    Serper 时返回 {"items": [], "provenance": {..., "error": "serper_unconfigured"}}：
    数据为空，但从返回值形状上看是成功的，_timed_call 也抓不到。

    这对模型是有害的：它看到空 items 会当成"确实没有相关新闻"，而不是"这次没查成"，
    进而说出与事实不符的话——正是 AGENT_CHAT_COMPLIANCE 第 3 条要防的。
    所以在这里把 provenance.error 提上来，让各工具 docstring 里"返回含 error 字段表示取数
    失败"这个约定对所有失败路径都成立，而不只对抛异常的那条。

    provenance.error 正常时是 None（make_provenance 的默认值），不会误伤成功结果。
    """
    if not isinstance(res, dict):
        return res
    prov = res.get("provenance")
    if isinstance(prov, dict) and prov.get("error") and not res.get("error"):
        return {**res, "error": prov["error"]}
    return res


@tool
async def resolve_entity(query: str, limit: int = 8) -> dict:
    """按名称/代码/拼音查 A 股标的，返回候选列表。

    用户提到「茅台」「600519」这类指代时，先用本工具确定标的代码，再去取数。
    命中多个候选时必须向用户澄清是哪一个，不要自己挑一个继续。

    Args:
        query: 标的名称、6 位代码或拼音，如 "茅台" / "600519" / "贵州茅台"
        limit: 返回候选数量上限
    """
    res, _ms, err = await _timed_call(
        _resolve_entity, query, limit, timeout=_T_RESOLVE, label="resolve_entity"
    )
    if err:
        return _fail("akshare", "stock_info_a_code_name", err)
    return _normalize(res)


@tool
async def get_realtime_quote(code: str) -> dict:
    """取单只标的的实时行情快照（现价、涨跌、涨跌幅、开高低收、成交量额等）。

    返回的数值来自交易所行情源，直接引用，不要自己换算或估算。
    provenance.fetched_at 是数据时点，回答涉及价格时必须注明。
    返回含 error 字段表示本次取数失败——如实说明取不到，不得凭记忆给数字。

    Args:
        code: 标的代码，需带市场后缀，如 "600519.SH" / "000001.SZ"
    """
    res, _ms, err = await _timed_call(
        _get_realtime_quote, code, timeout=_T_QUOTE, label="get_realtime_quote"
    )
    if err:
        return _fail("akshare", "stock_zh_a_spot_em", err)
    return _normalize(res)


@tool
async def get_technical_indicators(code: str) -> dict:
    """取日线并计算技术指标（均线/MACD/RSI/KDJ/BOLL），返回指标值与多空信号。

    指标由 pandas-ta-classic 在本地按标准公式算出，不是估算值——直接引用即可。
    你没有能力自己计算这些指标，必须调本工具取得。

    Args:
        code: 标的代码，需带市场后缀，如 "600519.SH"
    """
    # kline + compute 作为一个确定性单元，与 workers/market.py 的做法一致：
    # 拆成两个工具会让 agent 有机会拿着半截 kline 自己"算"指标。
    kline, _ms, kerr = await _timed_call(
        _get_kline, code, 120, "qfq", timeout=_T_KLINE, label="kline"
    )
    if kerr:
        return _fail("akshare", "stock_zh_a_hist", kerr)
    ind, _ms2, ierr = await _timed_call(
        _compute_indicators_from_kline,
        code,
        (kline or {}).get("data", []),
        kline or {},
        timeout=_T_KLINE,
        label="compute_indicators",
    )
    if ierr:
        return _fail("pandas-ta-classic", "compute_indicators_from_kline", ierr)
    return _normalize(ind)


@tool
async def get_kline(code: str, n: int = 60, adjust: str = "qfq") -> dict:
    """取日线 K 线序列（日期/开高低收/成交量）。

    只在需要看具体价格走势时调用；要技术指标请直接用 get_technical_indicators，
    不要取回 K 线自己算——你算的指标不可信。

    Args:
        code: 标的代码，如 "600519.SH"
        n: 取最近多少根日线，默认 60；超过 250 会占用大量上下文，非必要不要放大
        adjust: 复权方式，qfq 前复权 / hfq 后复权 / "" 不复权
    """
    res, _ms, err = await _timed_call(
        _get_kline, code, n, adjust, timeout=_T_KLINE, label="get_kline"
    )
    if err:
        return _fail("akshare", "stock_zh_a_hist", err)
    return _normalize(res)


@tool
async def get_company_profile(code: str) -> dict:
    """取公司概况（名称、所属行业、总市值、流通市值等）。

    Args:
        code: 标的代码，如 "600519.SH"
    """
    res, _ms, err = await _timed_call(
        _get_company_profile, code, timeout=_T_PROFILE, label="get_company_profile"
    )
    if err:
        return _fail("akshare", "stock_individual_info_em", err)
    return _normalize(res)


@tool
async def get_financial_indicators(code: str) -> dict:
    """取财务分析指标的多年序列（营收/净利同比、ROE、毛利率、资产负债率等）。

    数值来自交易所披露口径，直接引用并沿用其口径，不要自行换算同比/环比。

    Args:
        code: 标的代码，如 "600519.SH"
    """
    res, _ms, err = await _timed_call(
        _get_financial_indicators, code, timeout=_T_FINANCIALS, label="get_financial_indicators"
    )
    if err:
        return _fail("akshare", "stock_financial_analysis_indicator", err)
    return _normalize(res)


@tool
async def search_news(code: str, limit: int = 12) -> dict:
    """取某标的的相关新闻（标题、时间、链接）。

    引用新闻观点时注明标题与时间；新闻里的数字同样不能直接当作行情事实，
    要价格请用 get_realtime_quote。

    Args:
        code: 标的代码，如 "600519.SH"
        limit: 返回条数上限
    """
    res, _ms, err = await _timed_call(
        _search_news, code, timeout=_T_NEWS, label="search_news", limit=limit
    )
    if err:
        return _fail("akshare", "stock_news_em", err)
    return _normalize(res)


@tool
async def web_search(query: str, limit: int = 8) -> dict:
    """联网搜索公开信息（政策、行业动态、公司事件等）。

    需在设置中配置 Serper API Key 才可用；未配置时返回 error，
    此时如实告知用户联网搜索不可用，不要用记忆里的内容冒充搜索结果。

    Args:
        query: 搜索词
        limit: 返回条数上限
    """
    res, _ms, err = await _timed_call(
        _web_search, query, timeout=_T_WEB, label="web_search", limit=limit
    )
    if err:
        return _fail("serper", "search", err)
    return _normalize(res)


@tool
async def search_industry(name: str, limit: int = 8) -> dict:
    """按名称搜行业，返回匹配的申万行业及其估值（PE/PB/股息率/成份股数）。

    这是本项目唯一的行业维度数据源，也是目前唯一拿得到的估值数据——个股级的 PE/PB 依赖
    东方财富接口，那个源在本机网络下不可达，所以个股 PE/PB 恒为「未获取」。

    子串匹配、三个层级全搜、粒度细的排前面：搜「光伏」会同时返回三级的「光伏发电」
    「光伏电池组件」和二级的「光伏设备」，按需挑合适粒度引用。

    返回带 levels_ok / levels_total：若 levels_ok 小于 levels_total，说明有层级取数失败、
    结果不完整（provenance.error 里有说明）。此时要如实告知用户"行业列表可能不全"，
    不要把残缺结果当成该行业的全貌。

    Args:
        name: 行业名称或关键词，如 "光伏" / "白酒" / "半导体"
        limit: 返回条数上限
    """
    res, _ms, err = await _timed_call(
        _search_industry, name, limit, timeout=_T_INDUSTRY, label="search_industry"
    )
    if err:
        return _fail("sina", "sw_index_info", err)
    return _normalize(res)


# 对话链路工具集。研究链路在此基础上增补 prefetch_basics / compose_report。
CHAT_TOOLS = [
    resolve_entity,
    get_realtime_quote,
    get_technical_indicators,
    get_kline,
    get_company_profile,
    get_financial_indicators,
    search_news,
    web_search,
    search_industry,
]
