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
from typing import Optional

from langchain_openai import ChatOpenAI

from .config import settings

logger = logging.getLogger(__name__)

# ── temperature 三层智能解析 ──────────────────────────────────────────
# 第一层：推理模型（拒绝非默认 temperature，必须 1.0）
# o1/o3/o4-mini、QwQ、DeepSeek-R1、reasoning、rt 系等
_REASONING_PATTERNS = re.compile(
    r"(?:\bo[134](?:-|\b)|qwq|deepseek-r|reasoning|\brt\b)", re.IGNORECASE
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
    )


def resolved_chart_model(override: Optional[dict] = None) -> str:
    """返回 chart_analyze 实际使用的模型名（供 provenance 审计）。"""
    resolved = _resolve(override)
    if resolved is None:
        return settings.chart_model or ""
    _api, _base, fallback = resolved
    return ((override or {}).get("model") or "").strip() or settings.chart_model or fallback
