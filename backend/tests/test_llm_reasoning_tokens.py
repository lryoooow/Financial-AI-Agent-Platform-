"""审计 P0-A5：推理模型 + max_tokens=480 → 四段全空回退模板。

推理模型（deepseek-r / qwq / o 系 / rt）的 max_tokens 官方语义包含思维链输出，
且 langchain-openai 不把 reasoning_content 提进 .content —— 480 token 全被 CoT
吃掉时 finish_reason=length、content 为空串，报告四段成片静默回退模板（生产日志实证）。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finresearch.agents.workers import report as rep  # noqa: E402
from finresearch.llm import is_reasoning_model  # noqa: E402


class _FakeStreamLLM:
    def __init__(self, tokens):
        self._tokens = list(tokens)

    async def astream(self, _messages):
        for t in self._tokens:
            yield type("Chunk", (), {"content": t})()


_QUOTE = {
    "name": "贵州茅台", "industry": "白酒",
    "price": 1523.0, "changePct": 1.24, "pe": 22.4, "pb": 8.1,
    "marketCap": "1.9万亿", "as_of": "2026-08-28 15:00",
    "provenance": {"source": "eastmoney-direct"},
}
_FINS = {
    "years": ["2024", "2025"],
    "rows": [
        {"name": "营业收入", "values": ["100.0亿", "120.0亿"]},
        {"name": "净利润", "values": ["10.0亿", "9.0亿"]},
        {"name": "ROE", "values": ["15.0%", "14.0%"]},
    ],
}


def _run_section_with_model(monkeypatch, model: str) -> dict:
    captured = {}

    def fake_build_llm(**kwargs):
        captured.update(kwargs)
        return _FakeStreamLLM(["事实", "分析", "。"])

    monkeypatch.setattr(rep, "build_llm", fake_build_llm)
    asyncio.run(rep._llm_section(
        {"template": "equity_deep", "normalized_ticker": "600519.SH",
         "llm_config": {"base_url": "https://x", "api_key": "k", "model": model}},
        rep._SPEC_VALUATION,
        {"quote": _QUOTE, "fins": _FINS, "profile": {}},
    ))
    return captured


def test_reasoning_model_gets_enlarged_token_budget(monkeypatch):
    captured = _run_section_with_model(monkeypatch, "deepseek-reasoner")
    assert captured["max_tokens"] >= 4096, "推理模型 CoT 计入 max_tokens，480 必空返回"


def test_normal_model_keeps_configured_cap(monkeypatch):
    captured = _run_section_with_model(monkeypatch, "deepseek-chat")
    assert captured["max_tokens"] == rep.settings.report_section_max_tokens


def test_is_reasoning_model_patterns():
    assert is_reasoning_model("deepseek-reasoner")
    assert is_reasoning_model("deepseek-r1")
    assert is_reasoning_model("qwq-32b")
    assert is_reasoning_model("o3-mini")
    assert is_reasoning_model("QwQ-Plus")
    assert not is_reasoning_model("deepseek-chat")
    assert not is_reasoning_model("deepseek-v4-pro")
    assert not is_reasoning_model("")
    assert not is_reasoning_model(None)
