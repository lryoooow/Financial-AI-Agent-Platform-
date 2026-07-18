"""模型配置必须保持供应商原子性，禁止 URL/Key/model 跨厂商拼接。"""

from finresearch import llm as llm_mod


def test_chart_override_model_wins_over_global_default(monkeypatch):
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(llm_mod, "ChatOpenAI", FakeChatOpenAI)
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
