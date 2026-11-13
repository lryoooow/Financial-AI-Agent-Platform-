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

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

from langchain_core.language_models.chat_models import agenerate_from_stream, generate_from_stream
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI as ProviderChatOpenAI
from openai import APIConnectionError, APIStatusError
from tenacity import AsyncRetrying, Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from ..config import settings
from ..core.guardrails.errors import ModelServiceError
from ..core.guardrails.net_guard import secure_httpx_client, validate_outbound_url
from ..core.guardrails.sanitizer import InlineReasoning, strip_inline_reasoning

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


def _safe_llm_base_url(raw_url: str) -> Optional[str]:
    allow_private = not settings.fin_web or settings.fin_allow_private_endpoints
    try:
        return validate_outbound_url(
            raw_url,
            allow_private_local=allow_private,
            self_port=settings.port,
            resolve_dns=not allow_private,
        )
    except ValueError as exc:
        logger.warning("拒绝不安全的模型端点 %s", exc)
        return None


def _resolve(override: Optional[dict]):
    """返回 (api_key, base_url, model) 或 None（无可用于认证的 key）。override 优先。"""
    if override and (override.get("api_key") or "").strip():
        candidate_url = (override.get("base_url") or "").strip() or settings.llm_base_url
        safe_url = _safe_llm_base_url(candidate_url)
        if safe_url is None:
            return None
        return (
            override["api_key"],
            safe_url,
            (override.get("model") or "").strip() or settings.llm_model,
        )
    if override:
        # 传了 override 却没有可用 key：回落环境变量并告警，便于排查"为何所选模型没生效"
        logger.warning("LLM override 缺少 api_key，回落环境变量配置。")
    # 回落环境变量
    if settings.llm_configured:
        safe_url = _safe_llm_base_url(settings.llm_base_url)
        if safe_url is None:
            return None
        return settings.llm_api_key, safe_url, settings.llm_model
    return None


def llm_available(override: Optional[dict] = None) -> bool:
    """是否有可用模型（override 优先，回落环境变量）。

    存在的意义：判断"有没有配模型"不该靠 build_llm() 再看是不是 None——构造 ChatOpenAI 会
    真的建出 OpenAI/AsyncOpenAI 客户端（含 httpx 连接池），只为判空而建一个纯属浪费，
    况且真正要用的那个模型是 build_chat_agent 内部另建的，等于每请求白建两次。
    """
    return _resolve(override) is not None



@dataclass(slots=True)
class ParsedToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(slots=True)
class InvalidToolCallRecord:
    id: str
    name: str
    raw_args: str
    error: str


@dataclass(slots=True)
class _ToolSlot:
    id: str = ""
    name: str = ""
    args: list[str] = field(default_factory=list)


class ToolCallChunkAccumulator:
    """Assemble interleaved provider fragments by index before decoding JSON."""

    def __init__(self) -> None:
        self._slots: dict[int, _ToolSlot] = {}

    def feed_chunks(self, chunks: list[dict[str, Any]] | None) -> None:
        for chunk in chunks or []:
            try:
                index = int(chunk.get("index") or 0)
            except (TypeError, ValueError):
                continue
            if index < 0 or index >= 128:
                continue
            slot = self._slots.setdefault(index, _ToolSlot())
            cid = chunk.get("id")
            if isinstance(cid, str) and cid and not slot.id:
                slot.id = cid[:256]
            cname = chunk.get("name")
            if isinstance(cname, str) and cname:
                if not slot.name:
                    slot.name = cname[:256]
                elif cname == slot.name or slot.name.startswith(cname):
                    pass
                elif cname.startswith(slot.name):
                    slot.name = cname[:256]
                else:
                    slot.name = (slot.name + cname)[:256]
            args = chunk.get("args")
            if isinstance(args, str):
                slot.args.append(args)

    def finalize(self) -> tuple[list[ParsedToolCall], list[InvalidToolCallRecord]]:
        valid: list[ParsedToolCall] = []
        invalid: list[InvalidToolCallRecord] = []
        for index, slot in sorted(self._slots.items()):
            call_id = slot.id or f"call_{uuid4().hex}"
            raw = "".join(slot.args).strip()
            if not slot.name:
                invalid.append(InvalidToolCallRecord(call_id, "", raw, "missing tool name"))
                continue
            try:
                parsed = json.loads(raw or "{}", parse_constant=_invalid_json_constant)
                if not isinstance(parsed, dict):
                    raise ValueError("tool args must be a JSON object")
            except (ValueError, RecursionError):
                invalid.append(InvalidToolCallRecord(call_id, slot.name, raw, "invalid tool arguments"))
            else:
                valid.append(ParsedToolCall(call_id, slot.name, parsed))
        return valid, invalid


def _invalid_json_constant(_value: str) -> None:
    raise ValueError("JSON constants must be finite")


def sort_tools_for_kv_cache(tools: list[Any]) -> list[Any]:
    def name(tool: Any) -> str:
        if isinstance(tool, dict):
            return str((tool.get("function") or {}).get("name") or tool.get("name") or "")
        return str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))
    return sorted(tools, key=name)


def _wire_calls(values: Any) -> list[dict]:
    out = []
    for pos, raw in enumerate(values or []):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        if not isinstance(function, dict):
            continue
        out.append({
            "index": raw.get("index", pos), "id": raw.get("id"),
            "name": function.get("name"), "args": function.get("arguments"),
        })
    return out


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return {}


def _usage(data: dict) -> dict | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    return {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
    }


class _StreamAccumulator:
    def __init__(self) -> None:
        self.calls = ToolCallChunkAccumulator()
        self.parser = InlineReasoning()
        self.usage = None
        self.finish_reason = None

    def feed(self, payload: Any) -> ChatGenerationChunk | None:
        data = _payload(payload)
        self.usage = _usage(data) or self.usage
        choices = data.get("choices") or []
        if not choices:
            return None
        choice = choices[0]
        self.finish_reason = choice.get("finish_reason") or self.finish_reason
        delta = choice.get("delta") or {}
        self.calls.feed_chunks(_wire_calls(delta.get("tool_calls")))
        value = delta.get("content")
        visible, inline = self.parser.feed(value) if isinstance(value, str) else ("", "")
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        reasoning = (reasoning if isinstance(reasoning, str) else "") + inline
        if not visible and not reasoning:
            return None
        metadata = {"reasoning_content": reasoning} if reasoning else {}
        return ChatGenerationChunk(message=AIMessageChunk(content=visible, additional_kwargs=metadata))

    def finish(self) -> ChatGenerationChunk:
        tail_visible, tail_reasoning = self.parser.flush()
        valid, invalid = self.calls.finalize()
        # Completed chunks require strict JSON. A fixed invalid value is used when the
        # vendor supplied parseable JSON alongside an invalid function name/shape.
        tool_chunks = [
            {"name": call.name, "args": json.dumps(call.args, ensure_ascii=False), "id": call.id, "index": i}
            for i, call in enumerate(valid)
        ]
        tool_chunks.extend(
            {"name": call.name or "__invalid_tool_call__", "args": "[]", "id": call.id, "index": i}
            for i, call in enumerate(invalid, len(valid))
        )
        metadata = {"reasoning_content": tail_reasoning} if tail_reasoning else {}
        return ChatGenerationChunk(
            message=AIMessageChunk(
                content=tail_visible,
                additional_kwargs=metadata,
                tool_call_chunks=tool_chunks,
                chunk_position="last",
                usage_metadata=self.usage,
            ),
            generation_info={"finish_reason": self.finish_reason} if self.finish_reason else None,
        )


def _completion(response: Any) -> ChatResult:
    data = _payload(response)
    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    raw = choice.get("message") or {}
    acc = ToolCallChunkAccumulator()
    acc.feed_chunks(_wire_calls(raw.get("tool_calls")))
    valid, invalid = acc.finalize()
    content = raw.get("content") or ""
    reasoning = raw.get("reasoning_content") or raw.get("reasoning")
    extra: dict[str, Any] = {}
    if valid and isinstance(reasoning, str) and reasoning:
        extra["reasoning_content"] = reasoning
    message = AIMessage(
        content=strip_inline_reasoning(content) if isinstance(content, str) else "",
        additional_kwargs=extra,
        tool_calls=[{"id": call.id, "name": call.name, "args": call.args} for call in valid],
        invalid_tool_calls=[
            {"id": call.id, "name": call.name, "args": "", "error": call.error} for call in invalid
        ],
        usage_metadata=_usage(data),
    )
    return ChatResult(generations=[ChatGeneration(message=message)])


def _transient(error: BaseException) -> bool:
    if isinstance(error, (APIConnectionError, TimeoutError, ConnectionError)):
        return True
    if isinstance(error, APIStatusError):
        return error.status_code in (408, 429) or 500 <= error.status_code < 600
    return False


class ReasoningChatOpenAI(ProviderChatOpenAI):
    """Chat Completions gateway over the SDK's public completion resource."""

    fallback_models: tuple[str, ...] = ()

    def bind_tools(self, tools: list[Any], *args: Any, **kwargs: Any) -> Any:
        return super().bind_tools(sort_tools_for_kv_cache(list(tools)), *args, **kwargs)

    def _wire_request(self, messages: list[BaseMessage], stop: list[str] | None, kwargs: dict) -> dict:
        payload = {
            **self.model_kwargs, **kwargs, "model": self.model_name,
            "messages": convert_to_openai_messages(messages), "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            token_field = "max_completion_tokens" if re.match(r"^(gpt-5|o[134](?:-|$))", self.model_name) else "max_tokens"
            payload.setdefault(token_field, self.max_tokens)
        if stop:
            payload["stop"] = stop
        if self.extra_body:
            payload["extra_body"] = self.extra_body
        return payload

    def _for_model(self, payload: dict, model: str) -> dict:
        if model == self.model_name:
            return payload
        adapted = {**payload, "model": model, "temperature": _resolve_temperature(model)}
        token_limit = adapted.pop("max_completion_tokens", None) or adapted.pop("max_tokens", None)
        if token_limit is not None:
            token_field = "max_completion_tokens" if re.match(r"^(gpt-5|o[134](?:-|$))", model) else "max_tokens"
            adapted[token_field] = token_limit
        extra = adapted.get("extra_body")
        if isinstance(extra, dict) and "reasoning_effort" in extra and not is_reasoning_model(model):
            cleaned = {k: v for k, v in extra.items() if k != "reasoning_effort"}
            if cleaned:
                adapted["extra_body"] = cleaned
            else:
                adapted.pop("extra_body", None)
        return adapted

    def _models(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.model_name, *self.fallback_models)))

    def _retry_args(self, emitted: Any) -> dict:
        return {
            "reraise": True, "stop": stop_after_attempt(1 + int(self.max_retries or 0)),
            "wait": wait_exponential_jitter(initial=0.4, max=4, jitter=0.4),
            "retry": retry_if_exception(lambda error: not emitted() and _transient(error)),
        }

    def _sdk(self, *, asynchronous: bool) -> Any:
        root = self.root_async_client if asynchronous else self.root_client
        secure_httpx_client(
            getattr(root, "_client", None),
            allow_private_local=not settings.fin_web or settings.fin_allow_private_endpoints,
            self_port=settings.port,
        )
        return root.with_options(max_retries=0).chat.completions

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        request = self._wire_request(messages, stop, kwargs)
        emitted = False
        for model in self._models():
            try:
                async for attempt in AsyncRetrying(**self._retry_args(lambda: emitted)):
                    with attempt:
                        acc = _StreamAccumulator()
                        stream = await self._sdk(asynchronous=True).create(
                            **{**self._for_model(request, model), "stream": True},
                        )
                        async with stream:
                            async for raw in stream:
                                chunk = acc.feed(raw)
                                if chunk is not None:
                                    emitted = True
                                    yield chunk
                        yield acc.finish()
                        return
            except Exception as exc:
                if emitted or not _transient(exc) or model == self._models()[-1]:
                    raise ModelServiceError(exc) from None

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        request = self._wire_request(messages, stop, kwargs)
        emitted = False
        for model in self._models():
            try:
                for attempt in Retrying(**self._retry_args(lambda: emitted)):
                    with attempt:
                        acc = _StreamAccumulator()
                        with self._sdk(asynchronous=False).create(
                            **{**self._for_model(request, model), "stream": True},
                        ) as stream:
                            for raw in stream:
                                chunk = acc.feed(raw)
                                if chunk is not None:
                                    emitted = True
                                    yield chunk
                        yield acc.finish()
                        return
            except Exception as exc:
                if emitted or not _transient(exc) or model == self._models()[-1]:
                    raise ModelServiceError(exc) from None

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.streaming:
            return await agenerate_from_stream(self._astream(messages, stop, run_manager, **kwargs))
        request = self._wire_request(messages, stop, kwargs)
        for model in self._models():
            try:
                async for attempt in AsyncRetrying(**self._retry_args(lambda: False)):
                    with attempt:
                        response = await self._sdk(asynchronous=True).create(
                            **{**self._for_model(request, model), "stream": False},
                        )
                        return _completion(response)
            except Exception as exc:
                if not _transient(exc) or model == self._models()[-1]:
                    raise ModelServiceError(exc) from None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.streaming:
            return generate_from_stream(self._stream(messages, stop, run_manager, **kwargs))
        request = self._wire_request(messages, stop, kwargs)
        for model in self._models():
            try:
                for attempt in Retrying(**self._retry_args(lambda: False)):
                    with attempt:
                        response = self._sdk(asynchronous=False).create(
                            **{**self._for_model(request, model), "stream": False},
                        )
                        return _completion(response)
            except Exception as exc:
                if not _transient(exc) or model == self._models()[-1]:
                    raise ModelServiceError(exc) from None


ChatOpenAI = ReasoningChatOpenAI


def _fallback_models(override: Optional[dict]) -> tuple[str, ...]:
    raw = (override or {}).get("fallback_models", ())
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(model.strip() for model in raw[:4] if isinstance(model, str) and model.strip())


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
        self._saw_think_boundary = False
        self.orphan_think_closed = False  # 网关预填 <think> 导致 content 态遇到裸 </think>

    def _filter_orphan_close(self, text: str) -> tuple[str, str]:
        if not text and not self._close_tail:
            return "", ""
        s = self._close_tail + text
        self._close_tail = ""
        orphan_rc = ""
        if not self._saw_think_boundary and _THINK_CLOSE in s:
            idx = s.find(_THINK_CLOSE)
            if _THINK_OPEN not in s[:idx]:
                self._saw_think_boundary = True
                self.orphan_think_closed = True
                orphan_rc = s[:idx]
                s = s[idx + len(_THINK_CLOSE):].lstrip("\r\n ")
        emit, self._close_tail = _hold_close_prefix(s)
        return emit, orphan_rc

    def feed(self, text: str) -> tuple[str, str]:
        """喂入一段增量，返回 (正文增量, 思维链增量)。"""
        if not text:
            return "", ""
        if self._mode == "content":
            return self._filter_orphan_close(text)
        self._buf += text
        content, reasoning = "", ""
        if self._mode == "detect":
            head = self._buf.lstrip()
            if head.startswith(_THINK_OPEN):
                self._saw_think_boundary = True
                self._mode = "think"
                self._buf = head[len(_THINK_OPEN):]
            elif head and _THINK_OPEN.startswith(head):
                return "", ""  # 可能是被拆开的 "<thi" —— 先攒着
            elif not head:
                return "", ""  # 纯前导空白：等下一块再判定
            else:
                close_idx = self._buf.find(_THINK_CLOSE)
                if close_idx >= 0 and _THINK_OPEN not in self._buf[:close_idx]:
                    self._saw_think_boundary = True
                    reasoning = self._buf[:close_idx]
                    rest = self._buf[close_idx + len(_THINK_CLOSE):].lstrip()
                    self._buf = ""
                    self._mode = "content"
                    emit, extra_rc = self._filter_orphan_close(rest)
                    return emit, reasoning + extra_rc
                self._mode = "content"
                out, self._buf = self._buf, ""
                return self._filter_orphan_close(out)
        if self._mode == "think":
            idx = self._buf.find(_THINK_CLOSE)
            if idx >= 0:
                self._saw_think_boundary = True
                reasoning = self._buf[:idx]
                rest = self._buf[idx + len(_THINK_CLOSE):].lstrip()
                self._buf = ""
                self._mode = "content"
                content, extra_rc = self._filter_orphan_close(rest)
                reasoning += extra_rc
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
    if fallback := _fallback_models(override):
        kwargs["fallback_models"] = fallback
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
        **({"fallback_models": fallback_models} if (fallback_models := _fallback_models(override)) else {}),
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
