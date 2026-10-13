"""v1.7.0：ReasoningChatOpenAI 思维链透出（delta.reasoning_content）。

langchain-openai（1.6.0 venv / 1.3.3 系统 PyInstaller 环境，两版同形）在
delta→chunk 转换时丢弃 reasoning_content —— 子类把它补进
gen.message.additional_kwargs。本文件锁住：提取正确、边界不炸、super 路径
无回归、合并拼接正确、回传白名单安全。

注意：本文件必须同时在两个 Python 环境可跑（打包环境锁 1.3.3），
不得依赖 1.6.0 才有的 API。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langchain_core.messages import AIMessage  # noqa: E402

from finresearch.llm import ReasoningChatOpenAI, build_llm  # noqa: E402


def _model() -> ReasoningChatOpenAI:
    return ReasoningChatOpenAI(
        model="relay-x", api_key="sk-test", base_url="https://relay.example.com/v1"
    )


def _chunk(delta: dict | None, *, role: str | None = "assistant"):
    d: dict = {}
    if role is not None:
        d["role"] = role
    if delta is not None:
        d["content"] = delta.get("content")
        if "reasoning_content" in delta:
            d["reasoning_content"] = delta["reasoning_content"]
        if "tool_calls" in delta:
            d["tool_calls"] = delta["tool_calls"]
    return {"id": "c1", "choices": [{"index": 0, "delta": d}]}


def _convert(model, chunk):
    from langchain_core.messages import AIMessageChunk

    return model._convert_chunk_to_generation_chunk(
        chunk, AIMessageChunk, {}
    )


def test_pure_reasoning_chunk_is_extracted():
    gen = _convert(_model(), _chunk({"reasoning_content": "想"}))
    assert gen is not None
    assert gen.message.additional_kwargs.get("reasoning_content") == "想"
    assert gen.message.content == ""


def test_reasoning_and_content_same_chunk_both_kept():
    gen = _convert(_model(), _chunk({"reasoning_content": "思考", "content": "答"}))
    assert gen.message.additional_kwargs.get("reasoning_content") == "思考"
    assert gen.message.content == "答"


def test_usage_only_chunk_no_crash():
    """choices 为空的尾 chunk（usage-only）不抛错、原样返回（内容为空）。"""
    gen = _convert(_model(), {"id": "c1", "choices": []})
    # 版本差异容忍：1.6.0 返回带空 message 的 chunk；若未来版本返回 None 也算不炸
    assert gen is None or not (getattr(gen.message, "content", None) or "")


def test_none_delta_returns_gen_without_crash():
    chunk = {"id": "c1", "choices": [{"index": 0, "delta": None}]}
    gen = _convert(_model(), chunk)
    assert gen is None  # 与 super 行为一致（delta None → None）


def test_non_str_reasoning_ignored():
    gen = _convert(_model(), _chunk({"reasoning_content": ["nope"]}))
    assert "reasoning_content" not in (gen.message.additional_kwargs if gen else {})


def test_super_path_regression_content_and_tool_calls():
    """无 reasoning 的普通 chunk 经子类后与父类语义一致（super 路径无回归）。"""
    m = _model()
    chunk = _chunk({"content": "你好", "tool_calls": [
        {"index": 0, "id": "t1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    ]})
    gen = _convert(m, chunk)
    assert gen.message.content == "你好"
    assert gen.message.tool_calls and gen.message.tool_calls[0]["name"] == "f"
    assert "reasoning_content" not in gen.message.additional_kwargs


def test_chunk_merge_concatenates_reasoning():
    """分段 reasoning 经 AIMessageChunk 相加正确拼接（merge_dicts 字符串拼接）。"""
    g1 = _convert(_model(), _chunk({"reasoning_content": "先看"}))
    g2 = _convert(_model(), _chunk({"reasoning_content": "行情"}))
    merged = g1.message + g2.message
    assert merged.additional_kwargs.get("reasoning_content") == "先看行情"


def test_convert_message_to_dict_never_sends_reasoning_back():
    """回传安全：additional_kwargs 里的 reasoning_content 不进请求体（白名单锁定）。"""
    from langchain_openai.chat_models.base import _convert_message_to_dict

    msg = AIMessage(content="答", additional_kwargs={"reasoning_content": "秘密思考"})
    out = _convert_message_to_dict(msg)
    assert "reasoning_content" not in out
    assert out.get("content") == "答"


def test_build_llm_returns_reasoning_subclass(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-env")
    m = build_llm(streaming=True, override={
        "base_url": "https://relay.example.com/v1", "api_key": "sk-relay", "model": "relay-x",
    })
    assert isinstance(m, ReasoningChatOpenAI)
