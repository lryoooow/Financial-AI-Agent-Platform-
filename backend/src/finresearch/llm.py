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

from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI

from .config import settings

logger = logging.getLogger(__name__)

# ── temperature 三层智能解析 ──────────────────────────────────────────
# 第一层：推理模型（拒绝非默认 temperature，必须 1.0）
# o1/o3/o4-mini、QwQ、DeepSeek-R1、reasoning、rt 系等；
# v1.6.5 补 thinking —— 中转站常以 *-thinking 后缀暴露思考模型（gemini-3-pro-thinking、
# qwen3-thinking），且这类名字几乎从不命中其余模式（自定义模型名不受厂商命名约束）。
_REASONING_PATTERNS = re.compile(
    r"(?:"
    r"\bo[134](?:-|\b)"
    r"|\bgpt-5(?:\b|[.-])"
    r"|qwq"
    r"|deepseek-r"
    r"|gemini-(?:2\.5-pro|3(?:\.\d+)?-pro)"
    r"|glm-5(?:\.\d+|\b)"
    r"|hunyuan-t1"
    r"|grok-4(?:\.20|-reasoning|\b)"
    r"|doubao-seed-2-0-pro"
    r"|minimax-m3"
    r"|kimi-k2\.7"
    r"|reasoning|\brt\b|thinking|seed-thinking"
    r")",
    re.IGNORECASE,
)

# 第二层：Kimi / Moonshot —— API 强制 temperature=1，传 0.7 会 400
_KIMI_PATTERNS = re.compile(r"(?:kimi|moonshot)", re.IGNORECASE)

# 第三层：经实测支持温度调节的普通模型白名单 → 用 0.7（写作更稳）
# 注意：用具体模型族前缀，避免误伤 gpt-5* 等强制默认温度的推理模型。
_TEMP_07_WHITELIST = re.compile(
    r"(?:"
    r"deepseek"          # deepseek-chat / deepseek-v3 / deepseek-v4-flash（v4-pro 已由第一层拦为 1.0）
    r"|qwen"             # qwen-plus / qwen-max / qwen-turbo / qwen3-max
    r"|glm|chatglm"      # 智谱 GLM-4 系列（glm-5 已由第一层拦为 1.0）
    r"|\bgpt-(?:3\.5|4(?:\b|[o.-]))"  # OpenAI GPT-3.5 / GPT-4 / GPT-4o / GPT-4.1（严禁匹配 gpt-5*）
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


# ── 思考深度档位（v1.6.5）──────────────────────────────────────────────
# OpenAI 兼容的 reasoning_effort 参数（one-api/new-api 等中转站普遍透传，各家端点
# 自行映射：OpenAI o 系/gpt-5 原生、Gemini 端映射 thinkingBudget、Anthropic 端映射
# thinking.budget_tokens）。仅当用户在 Settings 显式选择档位时才下发——乱值/未设置
# 一律不发（部分严格端点会对未知参数 400，宁可不生效也不能发错）。
REASONING_EFFORTS = frozenset({"low", "medium", "high"})


def reasoning_effort_of(override: Optional[dict]) -> str:
    """从 override（前端所选配置）提取合法思考深度；未设置/非法值返回空串。"""
    effort = str(((override or {}).get("reasoning_effort") or "")).strip().lower()
    return effort if effort in REASONING_EFFORTS else ""


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


class ReasoningChatOpenAI(ChatOpenAI):
    """流式时保留 OpenAI 兼容端点的思维链增量（delta.reasoning_content）。

    langchain-openai（1.6.0 与 1.3.3，两版该函数签名与逻辑同形）的
    _convert_delta_to_message_chunk 只提 function_call/tool_calls 进
    additional_kwargs，reasoning_content 在 delta→chunk 转换时被静默丢弃
    ——字段本身还在（openai SDK 层 model_config extra="allow"，model_dump()
    里有），只是转换层不开门。此处补进 gen.message.additional_kwargs：
    - langchain_core 一等支持该键（_extract_reasoning_from_additional_kwargs，
      注释点名 DeepSeek/Ollama/XAI/Groq），AIMessageChunk 合并时字符串拼接；
    - _convert_message_to_dict 回传白名单不含它 → 历史/checkpointer 里带着
      CoT 也不会被发回 API（回传安全，有测试锁定）。
    仅供流式（streaming=True）；非流式走 _create_chat_result，本重写是 no-op。
    """

    def _convert_chunk_to_generation_chunk(  # noqa: D102（签名与 1.6.0/1.3.3 逐参一致）
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ):
        safe_chunk = (
            {**chunk, "choices": []}
            if isinstance(chunk, dict) and chunk.get("choices") is None
            else chunk
        )
        gen = super()._convert_chunk_to_generation_chunk(
            safe_chunk, default_chunk_class, base_generation_info
        )
        try:
            choices = (safe_chunk.get("choices") if isinstance(safe_chunk, dict) else None) or []
            delta = choices[0].get("delta") if choices and isinstance(choices[0], dict) else None
            rc = (delta or {}).get("reasoning_content") or (delta or {}).get("reasoning")
        except Exception:  # noqa: BLE001  chunk 形状异常：宁丢思维链不炸流
            rc = None
        if not isinstance(rc, str) or not rc:
            return gen  # 无思维链：原样返回（含 gen=None 的 beta 流 / usage-only 尾 chunk）
        msg = getattr(gen, "message", None)
        if isinstance(msg, AIMessageChunk):
            # chunk 内新建的 message 对象，原地写安全；分段到达时拼接
            prev = msg.additional_kwargs.get("reasoning_content") or ""
            msg.additional_kwargs["reasoning_content"] = prev + rc
        return gen


_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"


def _hold_close_prefix(s: str) -> tuple[str, str]:
    """若字符串末尾是 </think> 的前缀（如 '<'、'</th'），拆分为 (安全前缀, 待定尾巴)。"""
    if not s:
        return "", ""
    max_k = min(len(_THINK_CLOSE) - 1, len(s))
    for k in range(max_k, 0, -1):
        if _THINK_CLOSE.startswith(s[-k:]):
            return s[:-k], s[-k:]
    return s, ""


class ThinkTagSplitter:
    """把正文里内联的 <think>…</think>（含网关预填 <think> 仅输出 </think>）拆成思维链。

    部分中转站 / 自部署推理端（DeepSeek-R1、QwQ、Qwen3 经 vLLM 等）不走
    delta.reasoning_content，而是把思维链塞进 content 开头的 <think> 标签，或由网关预填
    prompt 末尾的 <think>、响应流直接输出思考并在中途以裸 </think> 收口。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._close_tail = ""
        self._mode = "detect"  # detect → think → content
        self.orphan_think_closed = False  # 网关预填 <think> 导致 content 态遇到裸 </think>

    def _filter_orphan_close(self, text: str) -> str:
        if not text and not self._close_tail:
            return ""
        s = self._close_tail + text
        self._close_tail = ""
        if _THINK_CLOSE in s:
            idx = s.find(_THINK_CLOSE)
            if _THINK_OPEN not in s[:idx]:
                self.orphan_think_closed = True
                after = s[idx + len(_THINK_CLOSE):].lstrip("\r\n ")
                s = s[:idx] + after
        emit, self._close_tail = _hold_close_prefix(s)
        return emit

    def feed(self, text: str) -> tuple[str, str]:
        """喂入一段增量，返回 (正文增量, 思维链增量)。"""
        if not text:
            return "", ""
        if self._mode == "content":
            return self._filter_orphan_close(text), ""
        self._buf += text
        content, reasoning = "", ""
        if self._mode == "detect":
            head = self._buf.lstrip()
            if head.startswith(_THINK_OPEN):
                self._mode = "think"
                self._buf = head[len(_THINK_OPEN):]
            elif head and _THINK_OPEN.startswith(head):
                return "", ""  # 可能是被拆开的 "<thi" —— 先攒着
            elif not head:
                return "", ""  # 纯前导空白：等下一块再判定
            else:
                close_idx = self._buf.find(_THINK_CLOSE)
                if close_idx >= 0 and _THINK_OPEN not in self._buf[:close_idx]:
                    reasoning = self._buf[:close_idx]
                    rest = self._buf[close_idx + len(_THINK_CLOSE):].lstrip()
                    self._buf = ""
                    self._mode = "content"
                    return self._filter_orphan_close(rest), reasoning
                self._mode = "content"
                out, self._buf = self._buf, ""
                return self._filter_orphan_close(out), ""
        if self._mode == "think":
            idx = self._buf.find(_THINK_CLOSE)
            if idx >= 0:
                reasoning = self._buf[:idx]
                rest = self._buf[idx + len(_THINK_CLOSE):].lstrip()
                self._buf = ""
                self._mode = "content"
                content = self._filter_orphan_close(rest)
            else:
                keep = len(_THINK_CLOSE) - 1  # 尾部可能是被拆开的 "</thi"
                if len(self._buf) > keep:
                    reasoning, self._buf = self._buf[:-keep], self._buf[-keep:]
        return content, reasoning

    def flush(self) -> tuple[str, str]:
        """流结束：把缓冲按当前模式吐出（未闭合的 <think> 整段算思维链）。"""
        buf, self._buf = self._buf, ""
        tail, self._close_tail = self._close_tail, ""
        if self._mode == "think":
            return "", buf + tail
        return buf + tail, ""


def strip_think(text: str) -> str:
    """非流式结果的同口径清洗：去掉开头的 <think>…</think> 或预填 <think> 留下的前缀…</think>。"""
    raw = text or ""
    close_idx = raw.find(_THINK_CLOSE)
    if close_idx >= 0 and _THINK_OPEN not in raw[:close_idx]:
        return raw[close_idx + len(_THINK_CLOSE):].lstrip()
    sp = ThinkTagSplitter()
    content, _ = sp.feed(raw)
    tail, _ = sp.flush()
    return content + tail


def build_llm(
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    streaming: bool = False,
    max_tokens: Optional[int] = None,
    override: Optional[dict] = None,
    timeout: float = 120.0,
    max_retries: int = 1,
) -> Optional[ChatOpenAI]:
    """构造分析师级 LLM。override（前端选择）优先；都无配置返回 None。

    temperature 默认由 _resolve_temperature(model) 智能决定；调用方显式传值则尊重。

    v1.6.0（审计 B11）：默认 timeout=120s / max_retries=1。此前不传时 openai 客户端
    默认 600s 总超时 + 2 次重试 —— 厂商挂起时对话流可卡 10 分钟且 SSE 无任何产出。
    120s 是 read 超时（两次字节之间的间隔上限），长回复流式不受影响。

    v1.6.5（思考深度档位）：override 带合法 reasoning_effort 时 ——
    - extra_body 透传该参数（显式选择才发，默认不传）；
    - temperature 强制 1.0（思考型 API 拒绝非默认温度，白名单不得再命中 0.7）；
    - 读超时放宽到 ≥300s：思维链不流式的端点首字节可能数分钟不出，
      120s 会错杀 + max_retries 重试一遍 = 双倍等待后失败。名字命中思考模型
      （is_reasoning_model，含 *-thinking）同样放宽超时，即使没选档位。
    """
    resolved = _resolve(override)
    if resolved is None:
        logger.warning("LLM 未配置（无 override 且 LLM_API_KEY 为空），返回 None。")
        return None
    api_key, base_url, fallback_model = resolved
    used_model = model or fallback_model
    effort = reasoning_effort_of(override)
    if temperature is None:
        temperature = 1.0 if effort else _resolve_temperature(used_model)
    if effort or is_reasoning_model(used_model):
        timeout = max(float(timeout), 300.0)
    kwargs: dict = {
        "model": used_model,
        "temperature": temperature,
        "streaming": streaming,
        "max_tokens": max_tokens,
        "base_url": base_url,
        "api_key": api_key,
        "timeout": timeout,
        "max_retries": max_retries,
    }
    if effort:
        kwargs["extra_body"] = {"reasoning_effort": effort}
    # v1.7.0：换 ReasoningChatOpenAI —— 流式补回 delta.reasoning_content（思维链透出）。
    # 非流式调用（streaming=False）该子类与 ChatOpenAI 行为完全一致（重写是 no-op）。
    return ReasoningChatOpenAI(**kwargs)


def build_chart_llm(override: Optional[dict] = None) -> Optional[ChatOpenAI]:
    """图表速读 LLM（便宜/快速档）；override 优先（含其 model）；无配置返回 None。

    v1.6.5：思考型模型（显式档位或名字命中推理模式）读超时同样放宽到 ≥300s。
    根因：该构造器还承担研究链路主循环的轻量档（research_agent.loop_model——
    未单独配轻量档时即用户所选模型），120s 会在思维链静默期杀掉规划调用并让整个
    研究任务失败，而同一模型的分段撰写（build_llm 路径）已是 300s。速读场景前端
    按悬停点随时 abort（useChartAI），放宽不会拖住 UI；reasoning_effort 参数本身
    仍不下发——速读要快，不主动要思考。
    """
    resolved = _resolve(override)
    if resolved is None:
        return None
    api_key, base_url, fallback = resolved
    # override 是一个不可拆分的供应商配置：base_url / api_key / model 必须来自同一处。
    # 旧实现先取全局 chart_model（其默认值永远是 deepseek-chat），会把 Moonshot 的
    # URL/Key 与 DeepSeek 模型拼在一起，稳定触发 404。
    model = ((override or {}).get("model") or "").strip() or settings.chart_model or fallback
    effort = reasoning_effort_of(override)
    timeout = 120.0
    if effort or is_reasoning_model(model):
        timeout = max(timeout, 300.0)
    return ChatOpenAI(
        model=model,
        temperature=1.0 if effort else _resolve_temperature(model),
        streaming=False,
        base_url=base_url,
        api_key=api_key,
        # v1.6.3（D3）：与 build_llm 同款加固（此前 chart 速读仍是 openai 默认 600s+2 重试）
        timeout=timeout,
        max_retries=1,
    )


def resolved_chart_model(override: Optional[dict] = None) -> str:
    """返回 chart_analyze 实际使用的模型名（供 provenance 审计）。"""
    resolved = _resolve(override)
    if resolved is None:
        return settings.chart_model or ""
    _api, _base, fallback = resolved
    return ((override or {}).get("model") or "").strip() or settings.chart_model or fallback


# ────────────────────────────────────────────────────────────
# v1.8.0：上下文窗口推断（自动压缩的预算基准）
# ────────────────────────────────────────────────────────────
# 中转站 / 国产模型走 ChatOpenAI 时没有 model.profile，deepagents 的内置摘要会回落到
# 固定 170k token 才触发 —— 远超 DeepSeek 64k / 多数 128k 窗口，长对话先撞窗口报错，
# 压缩从不发生。这里按模型名给出保守窗口（宁小勿大：压缩早一点只多一次摘要调用，
# 估大了就是直接报错），设置页可手动覆盖。
_CONTEXT_WINDOWS: tuple[tuple[tuple[str, ...], int], ...] = (
    (("qwen-long",), 500_000),
    (("deepseek-v4",), 256_000),
    (("claude-fable", "claude-opus-4-8", "claude-sonnet-5"), 256_000),
    (("claude",), 200_000),
    (("gemini-2.5", "gemini-3"), 256_000),
    (("gemini",), 200_000),
    (("kimi-k2",), 256_000),
    (("kimi", "moonshot"), 128_000),
    (("qwen3", "qwen-plus", "qwen-flash"), 256_000),
    (("qwen", "qwq"), 128_000),
    (("glm-5",), 200_000),
    (("glm", "zhipu"), 128_000),
    (("doubao-seed-2", "hunyuan", "minimax-m3"), 256_000),
    (("minimax-m2",), 200_000),
    (("doubao", "minimax", "abab"), 128_000),
    (("grok-4",), 256_000),
    (("mistral-large", "mistral-small"), 256_000),
    (("mistral",), 128_000),
    (("baichuan4",), 192_000),
    (("deepseek",), 64_000),
    (("ernie", "yi-", "baichuan"), 32_000),
    (("gpt-4o", "gpt-4-turbo", "llama", "gpt-oss"), 128_000),
    # 厂商名都没命中才看 OpenAI 系列（"o1/o3" 这类短串放最后，避免误命中其他模型名）
    (("gpt-5", "gpt-4.1", "o1", "o3", "o4"), 200_000),
)
DEFAULT_CONTEXT_WINDOW = 64_000


def context_window_tokens(model: str, override_k: int = 0) -> int:
    """模型上下文窗口（token）。override_k>0 时以设置页手动值为准。"""
    if override_k and int(override_k) > 0:
        return int(override_k) * 1000
    name = (model or "").strip().lower()
    for keys, size in _CONTEXT_WINDOWS:
        if any(k in name for k in keys):
            return size
    return DEFAULT_CONTEXT_WINDOW
