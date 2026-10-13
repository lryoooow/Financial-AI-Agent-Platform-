"""上下文预算治理的回归测试（2.0.8）。

曾经：deepagents 对无 profile 的模型回退 170k token 才触发历史压缩，高于
DeepSeek 64K 真实窗口；chat 工具六个参数无钳制 + series 字段无条件泄漏
（单次 1.35 万 token）——"单轮工具调用即可打爆窗口"的完整事故链。
这里锁定：profile 挂窗口表、超窗报错转换、工具钳制、播种裁剪四层防线。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langchain_core.exceptions import ContextOverflowError  # noqa: E402

from finresearch import llm as llm_mod  # noqa: E402
from finresearch.agents.deep import tools as deep_tools  # noqa: E402
from finresearch.agents.deep.chat_agent import ContextOverflowAdapterMiddleware  # noqa: E402
from finresearch.api.routes import _trim_seed_history  # noqa: E402


# ────────────────────────────────────────────────────────────
# 1) 模型 → 窗口 profile：SummarizationMiddleware 的触发基准
# ────────────────────────────────────────────────────────────
def test_model_window_table_maps_known_families():
    """国产模型走 base_url 时 profile 自动加载不生效，必须手工挂窗口表。"""
    assert llm_mod.model_window_tokens("deepseek-chat") == 51_200  # 64K × 0.8
    assert llm_mod.model_window_tokens("glm-4.6") == 128_000
    assert llm_mod.model_window_tokens("kimi-k2") == 128_000
    assert llm_mod.model_window_tokens("qwen-plus") == 128_000
    assert llm_mod.model_window_tokens("gpt-5") == 400_000
    assert llm_mod.model_window_tokens("gpt-4o") == 128_000
    assert llm_mod.model_window_tokens("claude-sonnet-4") == 200_000
    # 未知模型最保守：宁早压缩勿爆仓
    assert llm_mod.model_window_tokens("vendor-unknown") == 32_000
    assert llm_mod.model_window_tokens("") == 32_000


def test_build_llm_attaches_profile(monkeypatch):
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(llm_mod, "ChatOpenAI", FakeChatOpenAI)
    llm_mod.build_llm(override={
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "k",
        "model": "deepseek-chat",
    })
    # deepagents 读 model.profile 决定压缩触发/keep/溢出裁剪——这一项修两条 agent 链路
    assert captured["profile"] == {"max_input_tokens": 51_200}


# ────────────────────────────────────────────────────────────
# 2) 超窗报错转换：DeepSeek 的报错文本必须激活压缩重试
# ────────────────────────────────────────────────────────────
def _run_adapter(exc: Exception):
    async def handler(_request):
        raise exc

    mw = ContextOverflowAdapterMiddleware()
    return asyncio.run(mw.awrap_model_call(SimpleNamespace(), handler))


def test_overflow_adapter_converts_deepseek_error_text():
    """DeepSeek 实际报错不匹配 langchain-openai 的官方 pattern，必须手动转换。"""
    err = RuntimeError(
        "Error code: 400 - {'error': {'message': 'This model's maximum context "
        "length is 65536 tokens. However, you requested 71234 tokens.'}}"
    )
    try:
        _run_adapter(err)
        raised = None
    except ContextOverflowError:
        raised = "converted"
    except Exception as e:  # noqa: BLE001
        raised = e
    assert raised == "converted"


def test_overflow_adapter_passes_unrelated_errors_through():
    """非超窗错误不得被吞成压缩重试（网络/认证错误重试也无济于事）。"""
    for err in (RuntimeError("connection refused"), RuntimeError("401 unauthorized"),
                RuntimeError("image too large")):
        try:
            _run_adapter(err)
            raised = None
        except ContextOverflowError:
            raised = "bad-convert"
        except Exception as e:  # noqa: BLE001
            raised = e
        assert raised is not None and raised != "bad-convert", str(err)


# ────────────────────────────────────────────────────────────
# 3) 工具参数钳制 + series 泄漏
# ────────────────────────────────────────────────────────────
def test_get_kline_clamps_n(monkeypatch):
    captured = {}

    def fake_kline(code, n, adjust):
        captured["n"] = n
        return {"data": [{"date": "2026-07-08", "close": 1.0}], "provenance": {}}

    monkeypatch.setattr(deep_tools, "_get_kline", fake_kline)
    result = asyncio.run(deep_tools.get_kline.ainvoke({"code": "600519.SH", "n": 900}))
    assert captured["n"] == 120  # 900 → 120，6.2 万 token 的事故不再可能
    assert "data" in result
    asyncio.run(deep_tools.get_kline.ainvoke({"code": "600519.SH", "n": 1}))
    assert captured["n"] == 5  # 下界


def test_technical_indicators_drops_series(monkeypatch):
    """series（120 行 × 16 字段 ≈ 5.4 万字符）是计算过程泄漏，不得进上下文。"""

    def fake_kline(code, n, adjust):
        return {"data": [{"date": "2026-07-08", "close": 1.0}], "provenance": {}}

    def fake_compute(code, data, meta):
        return {
            "data": {"macd": {"value": 0.1, "signal": "多"}},
            "series": [{"date": f"2026-01-{i:02d}", "ma5": 1.0} for i in range(1, 120)],
            "provenance": {},
        }

    monkeypatch.setattr(deep_tools, "_get_kline", fake_kline)
    monkeypatch.setattr(deep_tools, "_compute_indicators_from_kline", fake_compute)
    result = asyncio.run(deep_tools.get_technical_indicators.ainvoke({"code": "600519.SH"}))
    assert "series" not in result
    assert result["data"]["macd"]["signal"] == "多"


def test_search_tools_clamp_limits(monkeypatch):
    captured = {}

    def fake_news(code, limit=12):
        captured["news"] = limit
        return {"items": [{"title": "t", "content": "x" * 500, "url": "u"}], "provenance": {}}

    def fake_web(query, limit=8):
        captured["web"] = limit
        return {"items": [{"title": "t", "snippet": "y" * 500}], "provenance": {}}

    def fake_industry(name, limit=8):
        captured["industry"] = limit
        return {"items": [{"name": "光伏"}], "provenance": {}}

    def fake_resolve(query, limit=8):
        captured["resolve"] = limit
        return {"results": [], "provenance": {}}

    monkeypatch.setattr(deep_tools, "_search_news", fake_news)
    monkeypatch.setattr(deep_tools, "_web_search", fake_web)
    monkeypatch.setattr(deep_tools, "_search_industry", fake_industry)
    monkeypatch.setattr(deep_tools, "_resolve_entity", fake_resolve)

    news = asyncio.run(deep_tools.search_news.ainvoke({"code": "600519.SH", "limit": 100}))
    assert captured["news"] == 12
    assert len(news["items"][0]["content"]) == 120  # 条目文本同步裁剪

    web = asyncio.run(deep_tools.web_search.ainvoke({"query": "光伏政策", "limit": 50}))
    assert captured["web"] == 8
    assert len(web["items"][0]["snippet"]) == 120

    asyncio.run(deep_tools.search_industry.ainvoke({"name": "光伏", "limit": 498}))
    assert captured["industry"] == 8

    asyncio.run(deep_tools.resolve_entity.ainvoke({"query": "茅台", "limit": 500}))
    assert captured["resolve"] == 10


# ────────────────────────────────────────────────────────────
# 4) 播种历史预算裁剪
# ────────────────────────────────────────────────────────────
def test_seed_history_trim_keeps_newest_within_budget():
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": "字" * 100} for i in range(10)]
    kept, dropped = _trim_seed_history(msgs, 350)  # 只够 3 条 + 省略说明
    assert dropped == 7
    assert len(kept) == 4  # 3 条最新 + 1 条省略说明
    assert kept[0]["content"].startswith("（更早的 7 条")
    # 列表按时间序返回：省略说明在最前，其后是被保留的较新消息，最新一条在最后
    assert kept[-1] is msgs[9]
    assert kept[1] is msgs[7]


def test_seed_history_trim_noop_within_budget():
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
    kept, dropped = _trim_seed_history(msgs, 10000)
    assert dropped == 0 and kept == msgs


def test_seed_history_trim_always_keeps_at_least_one():
    """超预算也必须保留最新一条（本轮提问），不能裁成空。"""
    msgs = [{"role": "user", "content": "x" * 5000}]
    kept, dropped = _trim_seed_history(msgs, 10)
    assert len(kept) == 1 and dropped == 0
