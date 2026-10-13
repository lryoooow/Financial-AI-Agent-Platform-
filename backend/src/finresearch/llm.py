"""LLM 构造器（OpenAI 兼容）。

支持两种来源：
1. 调用方显式传入 override {base_url, api_key, model}（来自前端选择的模型）—— 优先使用，
   不要求环境变量配置（家用桌面：模型配置在前端 localStorage，不再只读环境变量）。
2. 否则回落到 settings.llm_*（环境变量/.env）；未配置 key 返回 None，调用方优雅降级。

根因修复（#5）：之前 build_llm 第一行 `if not settings.llm_configured: return None` 会让任何 override
静默失效——前端选了模型，后端仍读环境变量。现在 override 优先。

1.3.0 temperature 三层智能解析（修复 Kimi 回归）：
- 上一版"推理模型 1.0 / 普通模型统一 0.7"在多模型家用场景有缺陷：Kimi(Moonshot) 的 API 只允许
  temperature=1，0.7 直接 400 报错（"only 1 is allowed for this model"），而 DeepSeek 接受 0.7。
- 改为白名单制：只有经实测确认支持温度调节的模型族才用 0.7；Kimi/Moonshot 与未知模型一律
  安全兜底 1.0，保证任何模型都不会因 temperature 翻车。
"""

from __future__ import annotations

import logging
import re
from typing import Literal, Optional

import httpx
from langchain_openai import ChatOpenAI

from .config import settings

logger = logging.getLogger(__name__)
ReasoningLevel = Literal["low", "medium", "high", "max"]
_REASONING_LEVELS = {"low", "medium", "high", "max"}

# 显式超时（默认是 openai SDK 的 600s 全量超时）：
# connect 5s 让死端点快速失败；read 300s 是"两次字节间"的静默上限且无总超时——
# 长流式回复（推理模型）不会被整体截断，而挂死的连接最迟 5 分钟内断开并走重试/报错。
_LLM_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)

# ── temperature 三层智能解析 ──────────────────────────────────────────
# 第一层：推理模型（拒绝非默认 temperature，必须 1.0）
# o1/o3/o4-mini、gpt-5、QwQ、DeepSeek-R1、reasoning、rt 系等
_REASONING_PATTERNS = re.compile(
    r"(?:\bo[134](?:-|\b)|\bgpt-5(?:-|\b)|qwq|deepseek-r|reasoning|\brt\b)", re.IGNORECASE
)

# 第二层：Kimi / Moonshot —— API 强制 temperature=1，传 0.7 会 400
_KIMI_PATTERNS = re.compile(r"(?:kimi|moonshot)", re.IGNORECASE)

# 第三层：经实测支持温度调节的普通模型白名单 → 用 0.7（写作更稳）
# 注意：用具体模型族前缀，避免误伤。新增模型时按需在此扩充。
_TEMP_07_WHITELIST = re.compile(
    r"(?:"
    r"deepseek"          # deepseek-chat / deepseek-v3 / deepseek-coder
    r"|qwen"             # qwen-plus / qwen-max / qwen-turbo
    r"|glm|chatglm|glm-4"  # 智谱 GLM
    r"|\bgpt-|gpt-3|gpt-4"  # OpenAI GPT（注意 \b- 前缀，不误伤 gpt-4o-mini 这类仍可调）
    r"|claude"           # Anthropic Claude（经兼容层）
    r"|baichuan"
    r"|yi-"              # 零一万物
    r")",
    re.IGNORECASE,
)


def _resolve_temperature(model: str) -> float:
    """三层智能 temperature 解析。

    返回值保证对目标模型 API 合法：
    - 推理模型（o1/o3/qwq/deepseek-r/rt）→ 1.0（拒绝非默认）
    - Kimi/Moonshot → 1.0（API 强制，传 0.7 会 400 报错）
    - 白名单普通模型（deepseek/qwen/glm/gpt/claude）→ 0.7
    - 未知模型 → 1.0（安全兜底：宁可少一点随机性，也不能因 temperature 翻车）

    这样修复了"普通模型统一 0.7"导致的 Kimi 回归，同时让明确支持调温的模型保持 0.7，
    并保证未来任何新模型都不会因为 temperature 值不合法而无法回复。
    """
    m = (model or "").strip()
    if not m:
        return 1.0
    if _REASONING_PATTERNS.search(m):
        return 1.0
    if _KIMI_PATTERNS.search(m):
        return 1.0
    if _TEMP_07_WHITELIST.search(m):
        return 0.7
    return 1.0  # 未知模型安全兜底


def is_reasoning_model(model: str) -> bool:
    """推理模型判断（供 routes 智能体阶段标签 / 外部使用）。"""
    return bool(_REASONING_PATTERNS.search(model or ""))


# ── 上下文窗口表（模型 → 输入窗口 tokens）─────────────────────────────
# 为什么要手工挂 profile：ChatOpenAI 的 profile 自动加载只认 OpenAI 官方模型名，
# DeepSeek/GLM/Kimi/Qwen 走 base_url 时 profile 恒为 None → deepagents 的
# SummarizationMiddleware 回退到 170,000 token 才触发的保守默认——高于
# DeepSeek 64K 的真实窗口，历史压缩永远不触发，长会话直接 400 且线程永久坏死。
# 挂上 profile 后：fraction 触发（0.85）、按比例 keep、溢出尾部裁剪全部激活。
#
# 64K 档再打 0.8 折：近似计数对中文正文低估约 2 倍（1 token≈1.5-2 汉字，
# 而估算按 4 字符/token），折算后等效触发 ≈0.68×窗口，给输出与计数误差留余量。
# 未知模型 32K 最保守：宁早压缩，勿爆仓。
_WINDOW_TABLE: list[tuple[re.Pattern, int]] = [
    (re.compile(r"deepseek", re.IGNORECASE), 64_000),
    (re.compile(r"glm|chatglm|zhipu", re.IGNORECASE), 128_000),
    (re.compile(r"kimi|moonshot", re.IGNORECASE), 128_000),
    (re.compile(r"qwen|qwq", re.IGNORECASE), 128_000),
    (re.compile(r"\bgpt-5", re.IGNORECASE), 400_000),
    (re.compile(r"\bo[134]\b|\bo[134]-", re.IGNORECASE), 200_000),
    (re.compile(r"gpt-4o|gpt-4\.1|gpt-4-turbo", re.IGNORECASE), 128_000),
    (re.compile(r"claude", re.IGNORECASE), 200_000),
]
_UNKNOWN_WINDOW = 32_000


def model_window_tokens(model: str) -> int:
    """模型名 → 有效输入窗口（tokens）。64K 档打 0.8 折，见上表注释。"""
    for pat, window in _WINDOW_TABLE:
        if pat.search(model or ""):
            return int(window * 0.8) if window <= 64_000 else window
    return _UNKNOWN_WINDOW


def _model_profile(model: str) -> dict:
    """langchain ModelProfile 的 dict 形态；deepagents 的压缩/裁剪都读这里。"""
    return {"max_input_tokens": model_window_tokens(model)}


# ── 思考档位 ──────────────────────────────────────────────────────────
# 档位是用户唯一该转的详略旋钮。此前报告长度由代码常量写死（单段 320 字符硬断流），
# 选 max 和选 low 出来一样短——档位形同虚设。现在改由档位决定 completion 预算与写作深度。
#
# 为什么以 max_tokens 为主、厂商 reasoning 参数为辅：
# max_tokens 是 OpenAI 兼容协议的通用参数，DeepSeek/通义/GLM/Kimi/豆包全都认；
# 而 reasoning_effort / thinking_budget 各家键名与取值都不同，覆盖面窄且变动快。
# 把详略控制押在通用参数上，厂商专属参数只作 best-effort 增强。
#
# max 档的 16000 不是内容上限，是防跑飞的安全阀：模型陷入循环时不至于烧光额度。
_LEVEL_MAX_TOKENS = {"low": 1500, "medium": 3000, "high": 6000, "max": 16000}

_LEVEL_DEPTH = {
    "low": "【篇幅与深度】简明扼要：每部分只给最关键的结论与支撑数据，不铺陈。",
    "medium": "【篇幅与深度】正常展开：结论、依据、关键数据之间的因果链要写清楚。",
    "high": "【篇幅与深度】充分展开：多角度论证，交叉验证不同指标，并指出与结论相悖的信号。",
    "max": (
        "【篇幅与深度】尽可能详尽：穷尽事实块中每一条可用证据，逐项论证，"
        "覆盖正反两面与边界情况。不要因为篇幅顾虑而省略任何有据可依的分析。"
    ),
}


def normalize_reasoning_level(requested: Optional[str]) -> str:
    """归一化档位；非法值一律回落 low（与前端默认一致）。"""
    level = (requested or "low").strip().lower()
    return level if level in _REASONING_LEVELS else "low"


def reasoning_profile(requested: Optional[str] = None) -> tuple[str, int, str]:
    """档位 → (归一档位, completion 预算, 写作深度指令)。

    调用方把 max_tokens 传给 build_llm、把深度指令拼进 system prompt，
    这样"详略跟着档位走"对任何厂商都成立，不依赖厂商是否支持 reasoning 参数。
    """
    level = normalize_reasoning_level(requested)
    return level, _LEVEL_MAX_TOKENS[level], _LEVEL_DEPTH[level]


def resolve_reasoning_effort(
    *,
    base_url: str,
    model: str,
    requested: Optional[str] = None,
) -> Optional[str]:
    """把应用四档映射为厂商支持值；普通/未知模型不发送该参数。

    只发各厂商文档里确实存在的取值。曾经把 max 映射成 "xhigh" 下发给 OpenAI，
    而 reasoning_effort 的合法值只有 minimal/low/medium/high——那是一个稳定 400。
    """
    level = normalize_reasoning_level(requested)
    identity = f"{base_url} {model}".lower()
    if "deepseek" in identity and re.search(r"(?:reasoner|deepseek-r|\br1\b)", identity):
        return "high"
    # o1-mini / o1-preview 不支持 reasoning_effort 参数，下发即 400，需排除
    if re.search(r"\bo1-(?:mini|preview)\b", model.lower()):
        return None
    if re.search(r"(?:\bo[134](?:-|\b)|\bgpt-5(?:-|\b))", model.lower()):
        return "high" if level == "max" else level
    return None


def _reasoning_kwargs(base_url: str, model: str, override: Optional[dict]) -> dict:
    effective = resolve_reasoning_effort(
        base_url=base_url,
        model=model,
        requested=(override or {}).get("reasoning_effort"),
    )
    return {"reasoning_effort": effective} if effective else {}


def _resolve(override: Optional[dict]):
    """返回 (api_key, base_url, model) 或 None（无可用于认证的 key）。override 优先。"""
    if override and (override.get("api_key") or "").strip():
        return (
            override["api_key"],
            (override.get("base_url") or "").strip() or settings.llm_base_url,
            (override.get("model") or "").strip() or settings.llm_model,
        )
    if override:
        # 传了 override 却没有可用 key：回落环境变量并告警，便于排查"为何所选模型没生效"
        logger.warning("LLM override 缺少 api_key，回落环境变量配置。")
    # 回落环境变量
    if settings.llm_configured:
        return settings.llm_api_key, settings.llm_base_url, settings.llm_model
    return None


def llm_available(override: Optional[dict] = None) -> bool:
    """是否有可用模型（override 优先，回落环境变量）。

    存在的意义：判断"有没有配模型"不该靠 build_llm() 再看是不是 None——构造 ChatOpenAI 会
    真的建出 OpenAI/AsyncOpenAI 客户端（含 httpx 连接池），只为判空而建一个纯属浪费，
    况且真正要用的那个模型是 build_chat_agent 内部另建的，等于每请求白建两次。
    """
    return _resolve(override) is not None


def build_llm(
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    streaming: bool = False,
    max_tokens: Optional[int] = None,
    override: Optional[dict] = None,
) -> Optional[ChatOpenAI]:
    """构造分析师级 LLM。override（前端选择）优先；都无配置返回 None。

    temperature 默认由 _resolve_temperature(model) 智能决定；调用方显式传值则尊重。
    """
    resolved = _resolve(override)
    if resolved is None:
        logger.warning("LLM 未配置（无 override 且 LLM_API_KEY 为空），返回 None。")
        return None
    api_key, base_url, fallback_model = resolved
    used_model = model or fallback_model
    if temperature is None:
        temperature = _resolve_temperature(used_model)
    return ChatOpenAI(
        model=used_model,
        temperature=temperature,
        streaming=streaming,
        max_tokens=max_tokens,
        base_url=base_url,
        api_key=api_key,
        timeout=_LLM_TIMEOUT,
        profile=_model_profile(used_model),
        **_reasoning_kwargs(base_url, used_model, override),
    )


def build_chart_llm(override: Optional[dict] = None) -> Optional[ChatOpenAI]:
    """图表速读 LLM（便宜/快速档）；override 优先（含其 model）；无配置返回 None。"""
    resolved = _resolve(override)
    if resolved is None:
        return None
    api_key, base_url, fallback = resolved
    # override 是一个不可拆分的供应商配置：base_url / api_key / model 必须来自同一处。
    # 旧实现先取全局 chart_model（其默认值永远是 deepseek-chat），会把 Moonshot 的
    # URL/Key 与 DeepSeek 模型拼在一起，稳定触发 404。
    model = ((override or {}).get("model") or "").strip() or settings.chart_model or fallback
    return ChatOpenAI(
        model=model,
        temperature=_resolve_temperature(model),
        streaming=False,
        base_url=base_url,
        api_key=api_key,
        timeout=_LLM_TIMEOUT,
        profile=_model_profile(model),
        **_reasoning_kwargs(base_url, model, override),
    )


def resolved_chart_model(override: Optional[dict] = None) -> str:
    """返回 chart_analyze 实际使用的模型名（供 provenance 审计）。"""
    resolved = _resolve(override)
    if resolved is None:
        return settings.chart_model or ""
    _api, _base, fallback = resolved
    return ((override or {}).get("model") or "").strip() or settings.chart_model or fallback
