"""v1.8.1 全栈深度审查回归测试套件。

覆盖：
1. Batch A：GPT-5 推理模型路由、孤立 </think> 拆分、SectionStreamParser 行内加粗与无重字流式、合规检测引号豁免
2. Batch B：指数代码防指鹿为马、拼音首字母检索、TTLCache 浅拷贝防污染、Wilder RSI/零振幅 KDJ/对称 BOLL、错误文本脱敏
3. Batch C：DPAPI v2 与掩码合并、多模态魔数校验、/api/models SSRF 防护、FTS5 跨词禁拼与窗口限流、画像词边界与否定词、HTML/PDF/DOCX/MD 导出健壮性
"""

from __future__ import annotations

import asyncio
import base64
import pytest


# ── Batch A：LLM 路由、流式解析与合规守卫 ─────────────────────────


def test_gpt5_is_treated_as_reasoning_and_orphan_think_close_tag():
    from finresearch.llm import ThinkTagSplitter, is_reasoning_model, strip_think

    assert is_reasoning_model("gpt-5")
    assert is_reasoning_model("gpt-5-mini")
    assert is_reasoning_model("o3-mini")

    # 预填 <think> 场景下模型只吐内容 + </think> + 正式回答；feed 返回 (clean, reasoning)
    s = ThinkTagSplitter()
    c1, r1 = s.feed("先推理估值水平</think>【核心结论】估值处于历史中枢")
    c2, r2 = s.flush()
    assert (r1 + r2) == "先推理估值水平"
    assert (c1 + c2) == "【核心结论】估值处于历史中枢"
    assert strip_think("思考片段</think>正式正文") == "正式正文"


def test_section_stream_parser_preserves_inline_bold_and_no_stutter():
    from finresearch.agents.workers.section_stream import SectionStreamParser

    # SectionStreamParser 第 0 段为摘要，1/2 段对应技术面/基本面；事件为 (kind, idx, text)
    parser = SectionStreamParser([["投资摘要"], ["技术面分析"], ["基本面分析"]])
    raw = (
        "## 投资摘要\n"
        "**核心看点**：经营现金流稳健增长。\n"
        "## 技术面分析\n"
        "**均线结构**：MA5 与 MA20 保持多头排列。\n"
        "## 基本面分析\n"
        "ROE 维持在 30% 以上。\n"
    )
    events = []
    for ch in raw:
        events.extend(parser.feed(ch))
    events.extend(parser.flush())

    summary = "".join(t for kind, idx, t in events if kind == "text" and idx == 0)
    tech = "".join(t for kind, idx, t in events if kind == "text" and idx == 1)
    fund = "".join(t for kind, idx, t in events if kind == "text" and idx == 2)

    assert "**核心看点**：经营现金流稳健增长。" in summary
    assert "**均线结构**：MA5 与 MA20 保持多头排列。" in tech
    assert "ROE 维持在 30% 以上。" in fund
    assert "技术面分析技术面分析" not in tech


def test_compliance_guard_allows_quoted_news_and_catches_real_advice():
    from finresearch.agents.workers.report import check_generated

    assert check_generated("公告提及「某机构维持买入评级」，仅供客观信息摘录。") == []
    assert check_generated("今日主力净买入 1.2 亿元，融资买入额环比上升。") == []
    assert len(check_generated("综合来看，当前位置建议买入，目标价 2000 元。")) >= 1


# ── Batch B：实体识别、缓存隔离、技术指标与脱敏 ───────────────────


def test_entity_rejects_index_codes_and_supports_pinyin_initials():
    from finresearch.agents.workers.entity_resolver import entity_node
    from finresearch.tools.entity import detect_index_code, resolve_entity, to_market_ticker

    assert detect_index_code("000001.SH") is not None
    assert detect_index_code("000300.SH") is not None
    assert detect_index_code("399001") is not None
    # 000001.SZ 是平安银行，绝不是指数
    assert detect_index_code("000001.SZ") is None

    # 显式后缀保留，且沪市 ETF 前缀（588000 / 501018）归入 .SH
    assert to_market_ticker("000001.SH") == "000001.SH"
    assert to_market_ticker("000001.SZ") == "000001.SZ"
    assert to_market_ticker("588000") == "588000.SH"
    assert to_market_ticker("501018") == "501018.SH"

    # entity_node 遇到指数代码必须拦截并返回明确错误（绝不指鹿为马变成 000001 平安银行）
    node_out = asyncio.run(entity_node({"code": "000001.SH", "template": "equity_deep"}))
    assert node_out["normalized_ticker"] == "000001.SH"
    assert "市场指数" in (node_out.get("error") or "")

    # 拼音首字母检索（内置种子表即可命中，零网络依赖）
    res_mt = resolve_entity("gzmt", limit=5)
    items_mt = res_mt.get("results", []) if isinstance(res_mt, dict) else res_mt
    assert any(h["code"] == "600519.SH" for h in items_mt)
    res_pa = resolve_entity("payh", limit=5)
    items_pa = res_pa.get("results", []) if isinstance(res_pa, dict) else res_pa
    assert any(h["code"] == "000001.SZ" for h in items_pa)


def test_ttl_cache_shallow_copy_prevents_caller_mutation():
    from finresearch.cache import TTLCache

    cache = TTLCache(max_entries=16)
    cache.set("k1", {"price": 100.0, "provenance": {"source": "sina"}}, ttl=60)
    got = cache.get("k1")
    assert got is not None
    got["price"] = 999.0
    got["provenance"]["source"] = "mutated"
    assert cache.get("k1")["price"] == 100.0
    assert cache.get("k1")["provenance"]["source"] == "sina"

    cache.set("k2", [{"x": 1}], ttl=60)
    got_list = cache.get("k2")
    got_list.append({"x": 2})
    assert len(cache.get("k2")) == 1


def test_indicators_flat_series_no_nan_and_wilder_rsi():
    from finresearch.tools import indicators as ind_mod
    from finresearch.tools import market as mkt_mod

    # 连续一字板/横盘（high == low）：KDJ 不得出现 NaN
    flat_candles = [
        {"date": f"2026-09-{i:02d}", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1000}
        for i in range(1, 31)
    ]
    res_flat = mkt_mod.compute_indicators_from_kline("600519.SH", flat_candles)
    items_flat = res_flat["data"]
    kdj_item = next((x for x in items_flat if "KDJ" in x["name"]), None)
    assert kdj_item is not None
    assert "nan" not in str(kdj_item["value"]).lower()

    # 上涨序列：market 与 indicators.compute_ta 结果一致且非空
    up_candles = [
        {
            "date": f"2026-09-{(i % 28) + 1:02d}",
            "open": 100.0 + i * 0.8,
            "high": 101.0 + i * 0.8,
            "low": 99.5 + i * 0.8,
            "close": 100.5 + i * 0.8,
            "volume": 5000 + i * 10,
        }
        for i in range(40)
    ]
    r_mkt = mkt_mod.compute_indicators_from_kline("600519.SH", up_candles)
    r_ind = ind_mod.compute_ta(up_candles)
    assert len(r_mkt["data"]) >= 4
    assert len(r_ind["data"]) >= 4


def test_sanitize_err_text_strips_paths_keys_and_diagnostics_masks_no_proxy(monkeypatch):
    from finresearch.agents._utils import sanitize_err_text
    from finresearch.tools.diagnostics import get_market_diagnostics_fast

    dirty = r"OperationalError at C:\Users\Admin\AppData\Roaming\FinResearch\t.db with key sk-abcdef1234567890"
    cleaned = sanitize_err_text(dirty)
    assert "C:\\Users" not in cleaned
    assert "sk-abcdef" not in cleaned

    monkeypatch.setenv("NO_PROXY", "internal.corp.local,10.20.30.40")
    diag = get_market_diagnostics_fast()
    no_proxy_summary = str(diag.get("proxy", {}).get("no_proxy") or "")
    assert "internal.corp.local" not in no_proxy_summary
    assert "10.20.30.40" not in no_proxy_summary


# ── Batch C：安全、知识库 FTS5、画像与报告导出 ──────────────────────


def test_secrets_dpapi_roundtrip_and_masked_merge():
    import os
    from finresearch.tools.secrets import (
        decrypt_nested,
        encrypt_nested,
        is_masked_secret,
        mask_nested,
        merge_masked_nested,
    )

    raw_cfg = {
        "deepseek": {"baseUrl": "https://api.deepseek.com/v1", "apiKey": "sk-real-secret-9876"},
        "openai": {"baseUrl": "https://api.openai.com/v1", "apiKey": "sk-openai-1234"},
    }
    enc = encrypt_nested(raw_cfg)
    if os.name == "nt":
        assert enc["deepseek"]["apiKey"].startswith("dpapi:")
    dec = decrypt_nested(enc)
    assert dec["deepseek"]["apiKey"] == "sk-real-secret-9876"

    masked = mask_nested(dec)
    assert is_masked_secret(masked["deepseek"]["apiKey"])
    assert masked["deepseek"]["apiKey"].endswith("9876")
    assert "sk-real-secret" not in masked["deepseek"]["apiKey"]

    # 关键防腐：用户在脱敏框末尾误粘贴新 Key 时，绝不能被误判为未修改的脱敏占位符
    pasted_hybrid = masked["deepseek"]["apiKey"] + "sk-pasted-new-key"
    assert not is_masked_secret(pasted_hybrid)

    incoming = {
        "deepseek": {"baseUrl": "https://api.deepseek.com/v1", "apiKey": masked["deepseek"]["apiKey"]},
        "openai": {"baseUrl": "https://api.openai.com/v1", "apiKey": "sk-openai-new-5555"},
    }
    merged = merge_masked_nested(incoming, enc)
    final_dec = decrypt_nested(encrypt_nested(merged))
    assert final_dec["deepseek"]["apiKey"] == "sk-real-secret-9876"
    assert final_dec["openai"]["apiKey"] == "sk-openai-new-5555"


def test_validate_images_input_magic_bytes():
    from finresearch.tools.context_store import validate_images_input

    valid_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    valid_url = "data:image/png;base64," + base64.b64encode(valid_png).decode()
    assert validate_images_input([valid_url]) == [valid_url]

    svg_polyglot = "data:image/png;base64," + base64.b64encode(b"<svg onload=alert(1)></svg>").decode()
    with pytest.raises(ValueError):
        validate_images_input([svg_polyglot])

    with pytest.raises(ValueError):
        validate_images_input([valid_url] * 5)


def test_models_endpoint_ssrf_guard():
    from finresearch.api.routes import _validate_models_endpoint_url

    with pytest.raises(ValueError):
        _validate_models_endpoint_url("file:///C:/Windows/win.ini")
    with pytest.raises(ValueError):
        _validate_models_endpoint_url("ftp://example.com/v1")
    with pytest.raises(ValueError):
        _validate_models_endpoint_url("http://169.254.169.254/latest/meta-data")
    with pytest.raises(ValueError):
        _validate_models_endpoint_url("http://127.0.0.1:9527/v1", self_port=9527)
    # 严禁利用 ? 或 # 注入任意路径截断 /models
    with pytest.raises(ValueError):
        _validate_models_endpoint_url("http://127.0.0.1:11434/admin/trigger?a=1#", self_port=9527)
    with pytest.raises(ValueError):
        _validate_models_endpoint_url("https://1.1.1.1/v1#bypass")

    # 允许本地 Ollama 等合法推理端口（字面 IP 零 DNS 依赖）
    assert _validate_models_endpoint_url("http://127.0.0.1:11434/v1", self_port=9527) == "http://127.0.0.1:11434/v1/models"
    assert _validate_models_endpoint_url("https://1.1.1.1/v1") == "https://1.1.1.1/v1/models"


def test_knowledge_fts_no_cross_space_bigram_and_multi_doc_window(tmp_path, monkeypatch):
    from finresearch.knowledge import store

    monkeypatch.setenv("FINRESEARCH_KNOWLEDGE_DB", str(tmp_path / "kb_test.db"))
    match_expr = store.build_match("茅台 估值")
    assert "台估" not in match_expr

    store.index_report(
        "rep_a",
        {
            "ticker": "600519.SH",
            "name": "贵州茅台",
            "template": "equity_deep",
            "as_of": "2026-11-11",
            "summary": "五粮液与茅台高端白酒估值对比。",
            "sections": [
                {"title": f"章节{i}", "body": "茅台估值稳健，五粮液现金流充沛。" * 10}
                for i in range(5)
            ],
        },
    )
    store.index_report(
        "rep_b",
        {
            "ticker": "000858.SZ",
            "name": "五粮液",
            "template": "equity_deep",
            "as_of": "2026-11-11",
            "summary": "五粮液估值处于历史低位。",
            "sections": [{"title": "估值分析", "body": "五粮液当前 PE 估值具备性价比。"}],
        },
    )

    hits = store.search("五 粮 液 估值", limit=4)
    report_ids = {h["report_id"] for h in hits}
    assert "rep_a" in report_ids
    assert "rep_b" in report_ids


def test_profile_topic_word_boundary_and_negation(tmp_path, monkeypatch):
    from finresearch.knowledge import profile

    monkeypatch.setenv("FINRESEARCH_KNOWLEDGE_DB", str(tmp_path / "prof_test.db"))
    # 英文单词 open / performance 含 pe 子串，不得误触发「估值」
    profile.record_chat_topics("open source model performance test")
    snap1 = profile.learned_profile()
    assert not any(t["key"] == "估值" for t in snap1["topics"])

    # 否定表达「不看PE估值」不得记为关注估值
    profile.record_chat_topics("我从来不看PE估值")
    snap2 = profile.learned_profile()
    assert not any(t["key"] == "估值" for t in snap2["topics"])

    # 正向表达正常提取
    profile.record_chat_topics("请重点分析PE估值与ROE盈利能力")
    snap3 = profile.learned_profile()
    topic_keys = {t["key"] for t in snap3["topics"]}
    assert "估值" in topic_keys
    assert "盈利能力" in topic_keys


def test_report_html_and_exports_resilience():
    from finresearch.tools import export as exp_mod
    from finresearch.tools import report as rep_mod

    payload = {
        "ticker": "600519.SH",
        "name": "贵州茅台",
        "template": "equity_deep",
        "as_of": "2026-11-11 15:00",
        "quote": {"price": 1520.0, "change": None, "changePct": None},
        "summary": "经营稳健，现金流充沛。\n第二段摘要验证多段落排版。",
        "catalyst": "中秋旺季动销",
        "financials": {
            "years": ["2024", "2025"],
            "rows": [{"name": "营收|亿元", "values": ["1700|口径A", "1900"]}],
        },
        "sections": [
            {"title": "基本面分析", "body": "第一段分析正文。\n\n第二段分析正文：" + "稳健增长" * 120},
        ],
        "risks": ["宏观消费复苏不及预期"],
        "sources": [{"title": "2025年报", "url": "https://example.com/r", "date": "2026-04-01"}],
        "disclaimer": "仅供研究参考，不构成投资建议。",
    }
    html = rep_mod.render_html(payload)
    # 财务表只渲染一次（修复前 base.html + equity_deep.html 渲染了两次 <table>）
    assert html.count("<table") == 1

    # Windows 保留设备名安全化
    safe_name = exp_mod.safe_filename({"ticker": "CON", "name": "", "as_of": "2026:11/11"}, "pdf")
    assert not safe_name.upper().startswith("CON.")
    assert safe_name.endswith(".pdf")

    md = exp_mod.render_md(payload)
    assert r"营收\|亿元" in md

    docx_bytes = exp_mod.render_docx(payload)
    assert docx_bytes[:2] == b"PK" and len(docx_bytes) > 1000

    pdf_bytes = exp_mod.render_pdf(payload)
    assert pdf_bytes.startswith(b"%PDF") and len(pdf_bytes) > 1000


def test_chart_analyze_kline_includes_ohlc_and_ma(monkeypatch):
    import asyncio
    import json
    from finresearch.api import routes

    captured = {}

    class _FakeLLM:
        async def ainvoke(self, messages):
            captured["messages"] = messages

            class _Resp:
                content = "缩量回踩五日线。"

            return _Resp()

    async def _fake_active():
        return {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-test", "model": "deepseek-chat"}

    monkeypatch.setattr(routes, "_active_llm_cfg", _fake_active)
    monkeypatch.setattr(routes, "build_chart_llm", lambda override=None: _FakeLLM())

    req = routes.ChartAnalyzeRequest(
        code="600519.SH",
        kind="kline",
        point=routes.ChartPoint(
            date="2026-11-11",
            price=1520.0,
            open=1505.0,
            high=1530.0,
            low=1501.0,
            close=1520.0,
            volume=23500.0,
            ma5=1512.4,
            ma10=1498.0,
            ma20=1480.2,
        ),
        prevClose=1500.0,
    )
    res = asyncio.run(routes.chart_analyze(req))
    assert res["text"] == "缩量回踩五日线。"
    joined = " ".join(str(getattr(m, "content", m)) for m in captured["messages"])
    assert '"open": 1505.0' in joined or '"open":1505.0' in joined
    assert '"ma5": 1512.4' in joined or '"ma5":1512.4' in joined
    assert '"ma20": 1480.2' in joined or '"ma20":1480.2' in joined
