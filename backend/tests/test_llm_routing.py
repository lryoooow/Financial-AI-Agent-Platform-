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


def test_reasoning_effort_never_emits_illegal_values():
    """只许下发厂商文档里存在的取值。

    曾经把 max 映射成 "xhigh" 发给 OpenAI，而 reasoning_effort 的合法值只有
    minimal/low/medium/high —— 那是个稳定 400，用户选了 max 反而整条链路挂掉。
    DeepSeek 那边同理，曾经原样下发 "max"。
    """
    legal = {"minimal", "low", "medium", "high", None}
    for base_url, model in (
        ("https://api.openai.com/v1", "gpt-5"),
        ("https://api.openai.com/v1", "o3-mini"),
        ("https://api.deepseek.com/v1", "deepseek-reasoner"),
        ("https://api.deepseek.com/v1", "deepseek-chat"),
        ("https://open.bigmodel.cn/api/paas/v4", "glm-5"),
    ):
        for level in ("low", "medium", "high", "max", "bogus", None):
            got = llm_mod.resolve_reasoning_effort(
                base_url=base_url, model=model, requested=level
            )
            assert got in legal, f"{base_url} {model} {level} -> {got}"


def test_reasoning_effort_maps_deepseek_reasoner_to_high():
    for level in ("low", "medium", "high", "max"):
        assert llm_mod.resolve_reasoning_effort(
            base_url="https://api.deepseek.com/v1",
            model="deepseek-reasoner",
            requested=level,
        ) == "high"


def test_reasoning_profile_scales_budget_with_level():
    """篇幅必须跟着档位走——这是"不写死字数"的落点。

    此前报告长度由代码常量决定（单段 320 字符硬断流），选 max 和选 low 出来一样短。
    现在档位映射到 completion 预算，max_tokens 是 OpenAI 兼容协议的通用参数，
    DeepSeek/通义/GLM/Kimi 全都认，不依赖厂商是否支持 reasoning_effort。
    """
    budgets = [llm_mod.reasoning_profile(lv)[1] for lv in ("low", "medium", "high", "max")]
    assert budgets == sorted(budgets), "预算必须随档位单调递增"
    assert len(set(budgets)) == 4, "四个档位的预算不能有重复"
    assert budgets[0] >= 1000, "最低档也要够写一份完整报告"

    for lv in ("low", "medium", "high", "max"):
        level, _tokens, depth = llm_mod.reasoning_profile(lv)
        assert level == lv
        assert depth.strip(), f"{lv} 档缺少写作深度指令"

    # 非法/缺省一律回落 low（与前端默认一致）
    for bad in (None, "", "bogus", "MAX "):
        assert llm_mod.reasoning_profile(bad)[0] in {"low", "max"}
    assert llm_mod.reasoning_profile(None)[0] == "low"


def test_unknown_model_omits_reasoning_parameter(monkeypatch):
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(llm_mod, "ChatOpenAI", FakeChatOpenAI)
    llm_mod.build_llm(override={
        "base_url": "https://vendor.example/v1",
        "api_key": "key",
        "model": "vendor-chat",
        "reasoning_effort": "max",
    })

    assert "reasoning_effort" not in captured
    assert llm_mod.resolve_reasoning_effort(
        base_url="https://api.openai.com/v1",
        model="gpt-4o",
        requested="high",
    ) is None


def test_gpt5_family_temperature_must_be_default_one():
    """gpt-5* 是推理模型，只接受默认 temperature=1；下发 0.7 是稳定 400。

    曾经 _REASONING_PATTERNS 不含 gpt-5，白名单 \bgpt- 抢先命中 → 0.7 →
    用户选任何 gpt-5 系模型整条链路必挂（Kimi 回归的同款问题换了模型族）。
    """
    for model in ("gpt-5", "gpt-5-mini", "gpt-5.1", "gpt-5-chat-latest"):
        assert llm_mod._resolve_temperature(model) == 1.0, model
        assert llm_mod.is_reasoning_model(model) is True, model
    # 普通 GPT 与国产普通模型仍应走 0.7 写作档
    assert llm_mod._resolve_temperature("gpt-4o") == 0.7
    assert llm_mod._resolve_temperature("deepseek-chat") == 0.7


def test_o1_mini_preview_never_receive_reasoning_effort():
    """o1-mini / o1-preview 不支持 reasoning_effort，参数必须整体省略。"""
    for model in ("o1-mini", "o1-preview", "o1-mini-2024-09-12"):
        assert llm_mod.resolve_reasoning_effort(
            base_url="https://api.openai.com/v1", model=model, requested="high"
        ) is None, model
    # 正牌 o 系与 gpt-5 照常下发合法档位
    for model in ("o1", "o3", "o3-mini", "o4-mini", "gpt-5"):
        assert llm_mod.resolve_reasoning_effort(
            base_url="https://api.openai.com/v1", model=model, requested="high"
        ) == "high", model
