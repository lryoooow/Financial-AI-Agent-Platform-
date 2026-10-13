"""模型配置必须保持供应商原子性，禁止 URL/Key/model 跨厂商拼接。

v1.6.5 增补：思考深度档位（reasoning_effort）的分发与超时分档。
"""

from finresearch import llm as llm_mod


def _capture_build(monkeypatch):
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    # v1.7.0：build_llm 构造 ReasoningChatOpenAI（思维链透出子类），build_chart_llm 仍用
    # ChatOpenAI —— 两个名字都指向同一个 fake，捕获面覆盖两条构造路径。
    monkeypatch.setattr(llm_mod, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setattr(llm_mod, "ReasoningChatOpenAI", FakeChatOpenAI)
    return captured


_RELAY = {"base_url": "https://relay.example.com/v1", "api_key": "sk-relay", "model": "relay-model"}


def test_chart_override_model_wins_over_global_default(monkeypatch):
    captured = _capture_build(monkeypatch)
    monkeypatch.setattr(llm_mod.settings, "llm_model", "deepseek-chat")
    monkeypatch.setattr(llm_mod.settings, "chart_analyze_model", None)

    override = {
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": "moonshot-key",
        "model": "kimi-k2.6",
    }
    llm_mod.build_chart_llm(override)

    assert captured["base_url"] == override["base_url"]
    assert captured["api_key"] == override["api_key"]
    assert captured["model"] == override["model"]
    assert llm_mod.resolved_chart_model(override) == override["model"]


# ── v1.6.5 思考深度档位 ────────────────────────────────────────────────

def test_reasoning_effort_flows_via_extra_body(monkeypatch):
    """合法档位 → extra_body 透传 + 温度强制 1.0 + 读超时放宽到 300s。"""
    captured = _capture_build(monkeypatch)
    llm_mod.build_llm(override={**_RELAY, "model": "gpt-5.5", "reasoning_effort": "high"})
    assert captured["extra_body"] == {"reasoning_effort": "high"}
    # 白名单模型名（gpt → 本该 0.7），但显式档位 = 思考型 API，必须 1.0
    assert captured["temperature"] == 1.0
    assert captured["timeout"] == 300.0


def test_reasoning_effort_invalid_value_is_not_sent(monkeypatch):
    """乱档位一律不发（严格端点对未知参数会 400），超时/温度也不放宽。"""
    captured = _capture_build(monkeypatch)
    llm_mod.build_llm(override={**_RELAY, "model": "gpt-5.5", "reasoning_effort": "ultra"})
    assert "extra_body" not in captured
    assert captured["timeout"] == 120.0
    assert captured["temperature"] == 0.7  # 白名单普通模型维持 0.7


def test_thinking_suffixed_model_gets_long_timeout_without_effort(monkeypatch):
    """名字命中思考模式（中转站 *-thinking 后缀）→ 超时放宽，但不发 extra_body。"""
    captured = _capture_build(monkeypatch)
    llm_mod.build_llm(override={**_RELAY, "model": "gemini-3-pro-thinking"})
    assert "extra_body" not in captured
    assert captured["timeout"] == 300.0
    assert captured["temperature"] == 1.0  # 未知思考模型安全兜底 1.0


def test_normal_model_keeps_default_timeout(monkeypatch):
    """普通模型不受影响：120s / 原温度策略（回归守卫）。"""
    captured = _capture_build(monkeypatch)
    llm_mod.build_llm(override={**_RELAY, "model": "deepseek-chat"})
    assert "extra_body" not in captured
    assert captured["timeout"] == 120.0
    assert captured["temperature"] == 0.7


def test_chart_llm_scales_timeout_for_thinking_model_but_no_effort(monkeypatch):
    """v1.6.5：chart 档（兼研究主循环轻量档）对思考模型放宽读超时；但速读不发档位参数。"""
    captured = _capture_build(monkeypatch)
    llm_mod.build_chart_llm({**_RELAY, "model": "relay-x", "reasoning_effort": "low"})
    assert captured["timeout"] == 300.0
    assert "extra_body" not in captured


def test_chart_llm_keeps_fast_timeout_for_normal_model(monkeypatch):
    captured = _capture_build(monkeypatch)
    llm_mod.build_chart_llm({**_RELAY, "model": "deepseek-chat"})
    assert captured["timeout"] == 120.0


def test_chart_without_override_uses_environment_model(monkeypatch):
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(llm_mod, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setattr(llm_mod.settings, "llm_api_key", "env-key")
    monkeypatch.setattr(llm_mod.settings, "llm_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(llm_mod.settings, "llm_model", "deepseek-chat")
    monkeypatch.setattr(llm_mod.settings, "chart_analyze_model", None)
    llm_mod.build_chart_llm()
    assert captured["model"] == "deepseek-chat"
