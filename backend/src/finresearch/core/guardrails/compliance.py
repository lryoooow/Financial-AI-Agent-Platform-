"""Output checks shared by reports, chat and chart explanations."""

import re
import unicodedata

FORBIDDEN_VERBS = ("买入", "卖出", "加仓", "减仓", "止损", "清仓", "抄底", "建仓", "满仓")

_NEUTRAL = re.compile(
    r"净买入|净卖出|融资买入|融券卖出|买入额|卖出额|大宗交易买入|大宗交易卖出"
    r"|买入评级|卖出评级|(?:维持|给予|上调至|下调至|获)买入"
    r"|不(?:构成|作为)(?:任何)?(?:投资|交易|操作|买卖|买入|卖出|加仓|减仓|[、/或与及\s]){1,14}(?:的)?(?:建议|依据|参考|指导)"
    r"|(?:不(?:能|得|予|可|会|做)|禁止|严禁|拒绝|避免)(?:给出|提供|输出|出现|包含|下达|做)?(?:任何)?(?:投资|交易|操作|买卖|买入|卖出|加仓|减仓|建仓|清仓|抄底|止损|满仓|仓位|目标价|[、/或与及\s]){1,18}(?:建议|指令|指导|判断)?"
    r"|(?:询问|问及|问到)(?:是否|能不能|要不要|可否)?(?:买入|卖出|加仓|减仓|抄底|建仓|清仓|止损)"
    r"|(?:切勿|请勿|不宜|不要|避免|谨防|警惕)(?:盲目|轻易|冲动)?(?:追高|杀跌)?(?:买入|卖出|抄底|加仓|重仓|满仓|止损)"
    r"|(?:机构|基金|北向资金|陆股通|外资|社保|险资|游资|主力|股东|高管|实控人|公司)(?:新进|大幅|小幅|持续|合计|近期)?(?:建仓|加仓|减仓|清仓|买入|卖出)"
)
_DIRECTIVE = re.compile(
    r"买[汝如茹进]|[卖賣][粗岀掉]|[加减建清满全][苍艙舱]|止[笋損]|梭哈|闭眼入|抄个底|全仓"
    r"|(?:现在|立即|直接|建议|应当|应该|赶紧|适合|可分批|可以)(?:就|马上|立即|考虑)?(?:购入|买进|增持|做空|加码|卖掉)"
    r"|(?:购入|增持|卖掉)(?:这只|该股|该标的|这档)"
    r"|(?:逢低|趁低|现在|建议|尽快|立刻|直接|分批|赶紧)(?:就|可|应|再|去|先)?(?:上车|下车|入场|出场|入手|撤离)"
    r"|(?:把|将)(?:手里的)?(?:仓位|筹码).{0,12}(?:加满|翻倍|清空|交出去|提到|降到)"
    r"|(?:目标|止盈|止损)(?:价格|价位|价)\s*[：:=为看]*\s*[+\d]"
    r"|(?:用|上|给到|调到|控制在)\s*[一二两三四五六七八九十\d]+\s*(?:成|%)(?:仓位|仓)"
    r"|\b(?:buy|sell|short|dump|purchase)\s+(?:(?:this|that|the|these|those)\s+)?(?:stock|shares|position)\b",
    re.I,
)
_PROMPT_REQUEST = re.compile(
    r"(?:输出|列出|打印|复述|重现|展示|翻译|泄露|给我|告诉我).{0,32}(?:系统提示词|系统提示|开发者消息|内部指令|system prompt)"
    r"|(?:reveal|print|repeat|show|dump|output|translate).{0,45}(?:system prompt|developer message|hidden instructions)"
    r"|(?:逐字|原样).{0,14}(?:上文|起始指令|启动提示|隐藏规则)", re.I
)
_PROMPT_OUTPUT = re.compile(
    r"【禁指令词】|【输出格式[—-]+必须严格遵守】|【每轮上下文】用户消息开头"
    r"|你是本机运行的专业金融研究对话助手|以下是(?:完整|我的|原始)?(?:系统提示词|开发者消息|内部指令)"
    r"|(?:here is|following is) (?:my|the) (?:full |original )?(?:system prompt|developer message)"
    r"|\b(?:sk|rk|pk|ak|tvly|jina)-[A-Za-z0-9_-]{8,}\b", re.I
)
_REASONING_SECRETS = re.compile(
    r"\b(?:sk|rk|pk|ak|tvly|jina)-[A-Za-z0-9_-]{8,}\b"
    r"|\bBearer\s+[A-Za-z0-9._-]{8,}\b"
    r"|[A-Za-z]:[\\/][^\s\"'<>，。；！？、）)】]{2,}"
    r"|(?<![A-Za-z0-9])/(?:Users|home|root|var/lib|etc)/[^\s\"'<>，。；！？、）)】]{2,}",
    re.I,
)
_CLAUSE_DELIM = re.compile(r"(?<=[。！？；\n])")


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).translate(str.maketrans("買賣進這檔應該倉損購減滿現議處鎖", "买卖进这档应该仓损购减满现议处锁"))
    return "".join(char for char in normalized if unicodedata.category(char) != "Cf")


def requests_protected_prompt(text: str) -> bool:
    return bool(_PROMPT_REQUEST.search(_normalize(text or "")))


def check_generated(text: str, *, streaming: bool = False) -> list[str]:
    normalized = _normalize(text or "")
    if _PROMPT_OUTPUT.search(normalized):
        return ["protected_content"]
    cleaned = _NEUTRAL.sub(" ", normalized)
    # Normalize punctuation inserted between characters of a short instruction.
    compact = re.sub(r"(?<=[\u3400-\u9fff])[\s·_*/.]+(?=[\u3400-\u9fff])", "", cleaned)
    forbidden = [word for word in FORBIDDEN_VERBS if word in compact]
    if not forbidden and _DIRECTIVE.search(compact):
        forbidden.append("investment_directive")
    return forbidden


def sanitize_reasoning(text: str) -> str:
    """Scrub secrets, local paths, internal prompt fragments, and investment directives from CoT."""
    if not text:
        return ""
    normalized = _normalize(text)
    if _PROMPT_OUTPUT.search(normalized):
        return ""
    scrubbed = _REASONING_SECRETS.sub("", text)
    if not check_generated(scrubbed):
        return scrubbed
    kept = [part for part in _CLAUSE_DELIM.split(scrubbed) if part and not check_generated(part)]
    return "".join(kept)
