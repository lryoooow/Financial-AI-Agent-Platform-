"""Comprehensive v2.0.0 full-stack audit regression suite.

Locks in fixes for:
1. 泽璟制药 (688266.SH) 8-parallel-tool burst, zero current-turn archiving, market_io=36,
   ticker canonicalization / duplicate call reuse without burning quota, and turnaround grounding.
2. Multi-turn NumberEvidence continuity, dates/periods/MA/RSI/ticker masking, loss sign equivalence,
   and news/article numeric grounding.
3. active_symbol switch & search_industry immunity against default code injection.
4. _summarize model summary budget (>=200 chars, <=1400 chars, never 60-char clipped).
5. verify_citation numeric/sign/unit boundary truncation rejection.
6. net_guard SSRF & Web mode outbound host/URL protection (CGNAT 100.100.100.200, NAT64, mapped IPv6, self-port).
7. WebAccessTokenMiddleware reverse-proxy HTTPS Origin acceptance & cross-site CSRF rejection.
8. AST dependency completeness check across backend/src/finresearch vs backend/pyproject.toml.
"""

from __future__ import annotations

import ast
import asyncio
import sys
import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from finresearch.api.web_auth import WebAccessTokenMiddleware, router as auth_router
from finresearch.config import settings
from finresearch.core.context.assembler import _ARCHIVED, assemble
from finresearch.core.context.container import ContextContainer
from finresearch.core.guardrails.net_guard import check_ip_allowed, validate_outbound_url
from finresearch.core.guardrails.number_evidence import NumberEvidence, metric_values
from finresearch.core.messages import DomainMessage
from finresearch.engine.tools_adapter import compile_tool
from finresearch.engine.turn_context import QUOTA_LIMITS, TurnToolState, bind_turn_state
from finresearch.tools import registry
from finresearch.tools.citation_verify import verify_citation
from finresearch.tools.facts import latest_profit_turnaround, profit_turnaround_tag


def test_zelgen_688266_parallel_burst_never_archives_current_turn_and_reuses_canonical_symbol(monkeypatch):
    """Reproduce the exact 8-parallel-tool burst on 688266.SH (泽璟制药)."""
    assert QUOTA_LIMITS["market_io"] >= 36

    tool_names = [
        "resolve_entity",
        "get_company_profile",
        "get_realtime_quote",
        "get_financial_indicators",
        "get_technical_indicators",
        "get_kline",
        "search_news",
        "compute_risk_metrics",
    ]
    calls = [{"id": f"call_{i}", "name": name, "args": {"code": "688266.SH"}} for i, name in enumerate(tool_names)]
    tool_msgs = [
        DomainMessage(
            "tool",
            f"[{name}] 688266.SH 泽璟制药 详细数据片段 " + ("营业总收入 5.33亿元 归母净利润 0.18亿元 " * 20),
            tool_call_id=f"call_{i}",
            name=name,
        )
        for i, name in enumerate(tool_names)
    ]

    # Even under a very tight token budget, current-turn tool outputs must NEVER become _ARCHIVED.
    history = [
        DomainMessage("user", "请全面分析泽璟制药（688266.SH）最新财报与行情表现"),
        DomainMessage("assistant", "", tool_calls=calls),
        *tool_msgs,
    ]
    assembled = assemble(history, ContextContainer(), budget=900, system_tokens=400)
    current_tool_contents = [m.content for m in assembled if m.role == "tool"]
    assert len(current_tool_contents) == 8
    assert all(_ARCHIVED not in c for c in current_tool_contents)

    # Alternate ticker formats (688266.SH / SH:688266 / 688266 / SH688266) canonicalize to 688266.SH,
    # reuse cached summary on duplicate call, and do NOT burn extra market_io quota.
    call_counter = 0

    async def fake_timed_call(_fn, code, **_kwargs):
        nonlocal call_counter
        call_counter += 1
        return {
            "code": code,
            "name": "泽璟制药",
            "price": 78.52,
            "changePct": 3.15,
            "pe": -120.4,
            "pb": 12.6,
            "provenance": {"source": "fixture", "trading_date": "2026-11-12"},
        }, 5, None

    monkeypatch.setattr(registry, "_timed_call", fake_timed_call)
    quote_tool = compile_tool(registry.get_realtime_quote)

    state = TurnToolState(active_symbol="688266.SH")

    async def invoke_variants():
        results = []
        with bind_turn_state(state):
            for idx, variant in enumerate(["688266.SH", "SH:688266", "688266", "SH688266"]):
                # Simulate chat_agent's canonicalized fingerprint + cache behavior via SymbolArgs
                validated = quote_tool.args_schema.model_validate({"code": variant}).model_dump()
                fresh, dup_msg = state.check_and_mark_fingerprint("get_realtime_quote", validated)
                if not fresh:
                    results.append(dup_msg)
                    continue
                ok, err = state.try_reserve("get_realtime_quote")
                assert ok and err is None
                msg = await quote_tool.ainvoke(
                    {"type": "tool_call", "name": "get_realtime_quote", "id": f"q_{idx}", "args": validated}
                )
                state.remember_result("get_realtime_quote", validated, str(msg.content))
                results.append(str(msg.content))
        return results

    outputs = asyncio.run(invoke_variants())
    assert call_counter == 1
    assert state.reservations.get("market_io") == 1
    assert "78.52" in outputs[0]
    assert all("DUPLICATE_CALL" in out and "78.52" in out for out in outputs[1:])


def test_zelgen_turnaround_tag_and_number_evidence_dates_signs_and_scaling():
    """Verify 2026-09-30 report period dates, turnaround tags, loss sign equivalence, and yuan->亿 scaling."""
    fins = {
        "years": ["2024年报", "2025年报", "2026三季报"],
        "periods": ["2024-12-31", "2025-12-31", "2026-09-30"],
        "yoy_base": {"净利润": "-0.98亿"},
        "rows": [
            {"name": "营业收入", "values": ["3.86亿", "5.33亿", "5.92亿"]},
            {"name": "净利润", "values": ["-2.79亿", "-1.38亿", "0.18亿"]},
            {"name": "毛利率", "values": ["92.15%", "93.40%", "93.82%"]},
        ],
    }
    assert profit_turnaround_tag("-1.38亿", "-2.79亿") == "同比减亏"
    assert profit_turnaround_tag("0.18亿", "-0.98亿") == "扭亏为盈"
    assert latest_profit_turnaround(fins) == "扭亏为盈"

    artifact = {
        **fins,
        "marketCap": 20815000000,  # 208.15 亿元
        "max_drawdown": -0.245,    # -24.5%
    }
    facts_text = (
        "688266.SH 泽璟制药 报告期 2026-09-30：营业收入 5.92亿，净利润 0.18亿（上年同期 -0.98亿，扭亏为盈；"
        "2025-12-31 净利润 -1.38亿），毛利率 93.82%，总市值 20815000000 元，最大回撤 -24.5%。"
    )
    evidence = NumberEvidence(facts_text, metric_values(artifact))

    # The exact sentence that previously triggered false truncation must pass cleanly (including spaceless Chinese dates)!
    valid_reply = (
        "【核心结论】✅ 泽璟制药（688266.SH）截至2026-09-30已明确扭亏，"
        "于2026三季报实现营业收入 5.92亿元、归母净利润 0.18亿元（上年同期净亏损 0.98亿元），"
        "2025年归母净利润亏损 1.38亿元，销售毛利率达 93.82%，当前总市值约 208.15亿元，"
        "近 250 个交易日最大回撤为 24.5%，MA20 与 RSI(14) 处于观察区间 [1]。"
    )
    assert evidence.check(valid_reply, financial_only=True) == []

    # Fabricated numbers, flipped loss->profit signs, and Chinese-numeral fabrications MUST still be caught!
    assert evidence.check("泽璟制药 2026三季报实现归母净利润 1.66亿元。", financial_only=True) != []
    assert evidence.check("公司已明确扭亏，实现归母净利润 0.98亿元。", financial_only=True) != []
    assert evidence.check("公司最新总市值达到三十亿元。", financial_only=True) != []
    assert evidence.check("公司最新总市值达到三百五十亿元。", financial_only=True) != []


def test_active_symbol_override_and_search_industry_immunity():
    """search_industry must never get code injected from active_symbol; multi-stock explicit code wins."""
    state = TurnToolState(
        active_symbol="688266.SH",
        trusted_tool_arguments={"*": {"code": "688266.SH"}},
    )
    # Industry tool does not accept code/symbol -> must not inject code
    ind_args = state.arguments_for("search_industry", {"query": "化学制药"}, allowed_fields={"query"})
    assert ind_args == {"query": "化学制药"}
    assert "code" not in ind_args

    # Explicit peer stock in multi-stock comparison must NOT be overwritten by trusted_tool_arguments or active_symbol
    peer_args = state.arguments_for("get_realtime_quote", {"code": "600276.SH"}, allowed_fields={"code"})
    assert peer_args["code"] == "600276.SH"

    # Empty code on a symbol tool still falls back to active_symbol when allowed
    default_args = state.arguments_for("get_realtime_quote", {}, allowed_fields={"code"})
    assert default_args["code"] == "688266.SH"


def test_tool_summary_preserves_rich_evidence_without_60_char_truncation(monkeypatch):
    """Regression for the `(available - 100) // 6` bug that clipped tool summaries to ~60 chars."""
    news_items = [
        {
            "title": f"泽璟制药披露新药临床进展公告第{idx}号：关键三期数据达到主要终点且商业化放量加速",
            "content": f"公告显示第{idx}项适应症临床试验数据表现稳健。",
            "time": f"2026-11-0{idx}",
            "source": "上交所公告",
        }
        for idx in range(1, 7)
    ]

    async def fake_news(_fn, *_args, **_kwargs):
        return {
            "items": news_items,
            "provenance": {"source": "eastmoney_search", "fetched_at": "2026-11-12T15:00:00+08:00"},
        }, 4, None

    monkeypatch.setattr(registry, "_timed_call", fake_news)
    news_tool = compile_tool(registry.search_news)
    msg = asyncio.run(
        news_tool.ainvoke({"type": "tool_call", "name": "search_news", "id": "n1", "args": {"code": "688266.SH"}})
    )
    text = str(msg.content)
    assert 200 < len(text) <= 1450
    assert "临床进展公告第1号" in text
    assert "临床进展公告第5号" in text


def test_verify_citation_rejects_digit_sign_and_unit_boundary_truncations():
    """Prevent partial substring matches from flipping 12.50 -> 2.50, -5.00% -> 5.00%, or 12.50万亿元 -> 12.50万."""
    source = "公司期内营业收入为 12.50 亿元，归母净利润同比变动 -5.00%，行业规模达 12.50万亿元。"

    assert verify_citation("营业收入为 12.50 亿元", source)["verified"] is True
    assert verify_citation("归母净利润同比变动 -5.00%", source)["verified"] is True

    # Leading digit or sign truncation -> MUST reject
    assert verify_citation("2.50 亿元", source)["verified"] is False
    assert verify_citation("5.00%", source)["verified"] is False

    # Trailing digit or unit truncation -> MUST reject
    assert verify_citation("营业收入为 12.5", source)["verified"] is False
    assert verify_citation("行业规模达 12.50万", source)["verified"] is False


def test_net_guard_ssrf_blocks_metadata_cgnat_nat64_self_port_and_web_private():
    """Verify SSRF defenses across Alibaba Cloud metadata (100.100.100.200), AWS IMDS, IPv6 wrappers & Web mode."""
    # Always blocked even on desktop (allow_private_local=True):
    for forbidden_ip in (
        "169.254.169.254",
        "100.100.100.200",
        "0.0.0.0",
        "::ffff:169.254.169.254",
        "::ffff:100.100.100.200",
        "64:ff9b::a9fe:a9fe",
    ):
        with pytest.raises(ValueError):
            check_ip_allowed(forbidden_ip, allow_private_local=True)

    # Self-port loopback blocked even when private local endpoints are allowed (e.g. Ollama on 11434 allowed, self 9527 blocked):
    check_ip_allowed("127.0.0.1", allow_private_local=True, port=11434, self_port=9527)
    with pytest.raises(ValueError, match="自身服务端口"):
        check_ip_allowed("127.0.0.1", allow_private_local=True, port=9527, self_port=9527)

    # In Web deployment (allow_private_local=False), RFC1918 and loopback are blocked:
    for lan_url in (
        "http://127.0.0.1:11434/v1",
        "http://localhost:8000/v1",
        "http://192.168.1.20:8080/v1",
        "http://10.0.0.5:8000/v1",
        "http://internal-llm.local/v1",
    ):
        with pytest.raises(ValueError, match="禁止连接内网"):
            validate_outbound_url(lan_url, allow_private_local=False, resolve_dns=False)


def test_web_access_middleware_allows_reverse_proxy_https_origin_and_blocks_csrf(monkeypatch):
    """Behind TLS-terminating Nginx/Caddy, backend sees http://host while browser sends Origin: https://host."""
    monkeypatch.setattr(settings, "fin_web", True)
    monkeypatch.setattr(settings, "fin_access_token", "test-deploy-secret-token")
    monkeypatch.setattr(settings, "fin_cors_origins", "")

    app = FastAPI()
    app.state.dev_cors = False
    app.add_middleware(WebAccessTokenMiddleware)
    app.include_router(auth_router)

    @app.post("/api/protected-write")
    async def protected_write():
        return {"ok": True}

    async def scenario():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://jingyuan.example.com") as client:
            # 1. Cross-site Origin must be rejected with 403 CSRF check
            csrf_resp = await client.post(
                "/api/auth/login",
                json={"token": "test-deploy-secret-token"},
                headers={"Origin": "https://attacker.example.org"},
            )
            assert csrf_resp.status_code == 403

            # 2. Same-host HTTPS Origin behind reverse proxy (with X-Forwarded-Proto: https) must succeed
            #    and issue a Secure, HttpOnly jy_session cookie!
            login_resp = await client.post(
                "/api/auth/login",
                json={"token": "test-deploy-secret-token"},
                headers={
                    "Origin": "https://jingyuan.example.com",
                    "X-Forwarded-Proto": "https",
                },
            )
            assert login_resp.status_code == 200
            set_cookie = login_resp.headers.get("set-cookie", "")
            assert "jy_session=" in set_cookie and "Secure" in set_cookie and "HttpOnly" in set_cookie

            # 3. Authenticated POST with matching HTTPS Origin passes (using client cookie jar)
            cookie_val = login_resp.cookies.get("jy_session")
            assert cookie_val
            client.cookies.set("jy_session", cookie_val)
            write_resp = await client.post(
                "/api/protected-write",
                headers={"Origin": "https://jingyuan.example.com"},
            )
            assert write_resp.status_code == 200
            assert write_resp.json() == {"ok": True}

    asyncio.run(scenario())


def test_pyproject_declares_every_direct_third_party_import():
    """Ensure PyInstaller/Docker/uv builds never miss a direct import in backend/src/finresearch."""
    backend_dir = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((backend_dir / "pyproject.toml").read_text(encoding="utf-8"))
    raw_deps = list(pyproject["project"]["dependencies"])
    for extra_deps in pyproject["project"].get("optional-dependencies", {}).values():
        raw_deps.extend(extra_deps)

    # Map PyPI distribution names to top-level import module names
    dist_to_import = {
        "pillow": "PIL",
        "python-docx": "docx",
        "pydantic-settings": "pydantic_settings",
        "pandas-ta-classic": "pandas_ta_classic",
        "chinese-calendar": "chinese_calendar",
        "langchain-core": "langchain_core",
        "langchain-openai": "langchain_openai",
        "typing-extensions": "typing_extensions",
        "charset-normalizer": "charset_normalizer",
        "beautifulsoup4": "bs4",
    }
    declared_imports = {"finresearch", "starlette"}  # starlette is guaranteed by fastapi
    for spec in raw_deps:
        name = spec.split("[", 1)[0].split(">", 1)[0].split("<", 1)[0].split("=", 1)[0].split("~", 1)[0].strip().lower()
        declared_imports.add(name.replace("-", "_"))
        if name in dist_to_import:
            declared_imports.add(dist_to_import[name])

    stdlib = set(sys.stdlib_module_names)
    src_root = backend_dir / "src" / "finresearch"
    missing: list[str] = []
    for py_file in src_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                if mod not in stdlib and mod not in declared_imports:
                    missing.append(f"{py_file.relative_to(backend_dir)}:{node.lineno} -> {mod}")

    assert not missing, "Undeclared third-party imports:\n" + "\n".join(sorted(set(missing)))



def test_reasoning_stream_blocks_recommendations_and_multi_token_split_secrets():
    """Supervisor P1-A & P1-B regression: CoT directives & 1..4-char split secrets/paths never leak."""
    from finresearch.core.guardrails.compliance import check_generated, sanitize_reasoning
    from finresearch.core.guardrails.sanitizer import AnswerStreamSanitizer, ReasoningStreamGuard
    from finresearch.engine.event_bridge import TurnTranscript

    # 1. Direct recommendation / target price in CoT is stripped, while meta-compliance refusal stays intact
    assert sanitize_reasoning("用户问怎么操作，应当建议立即买入该股，目标价 45.00 元。") == ""
    mixed = sanitize_reasoning("先核对三季报营收与现金流。应当建议立即买入该股，目标价 45.00 元。根据规则不能给出买入或目标价建议。")
    assert "先核对三季报营收与现金流。" in mixed
    assert "不能给出买入或目标价建议" in mixed
    assert "立即买入" not in mixed and "45.00" not in mixed

    # 2. 4-char chunked secret & Windows path across streaming SSE + TurnTranscript persistence
    secret_cot = "我们先检查配置：sk-proj1234567890abcdef，路径 C:\\SecretDir\\key.json，然后再继续分析这家公司的三季报财务数据。"
    for step in (1, 3, 4):
        rg = ReasoningStreamGuard()
        tx = TurnTranscript()
        emitted: list[str] = []
        for idx in range(0, len(secret_cot), step):
            piece = secret_cot[idx : idx + step]
            tx.record({"type": "reasoning", "text": piece})
            out = rg.feed(piece)
            if out:
                emitted.append(out)
        tail = rg.flush()
        if tail:
            emitted.append(tail)
        sse_joined = "".join(emitted)
        persisted = tx.fields("completed")["reasoning_summary"]
        assert "sk-proj1234567890abcdef" not in sse_joined
        assert "SecretDir" not in sse_joined
        assert "sk-proj1234567890abcdef" not in persisted
        assert "SecretDir" not in persisted

    # 3. Inline <think> split across tiny chunks in AnswerStreamSanitizer
    inline = "<think>建议立即买入该股，密钥 sk-proj1234567890abcdef。核对公开财报数据。</think>公开数据显示营收稳健。"
    guard = AnswerStreamSanitizer(check_generated)
    r_parts: list[str] = []
    v_parts: list[str] = []
    for idx in range(0, len(inline), 3):
        d = guard.feed(inline[idx : idx + 3])
        r_parts.append(d.reasoning_text)
        v_parts.append(d.visible_text)
    end_d = guard.flush()
    r_parts.append(end_d.reasoning_text)
    v_parts.append(end_d.visible_text)
    full_r = "".join(r_parts)
    full_v = "".join(v_parts)
    assert "立即买入" not in full_r and "sk-proj1234567890abcdef" not in full_r
    assert "核对公开财报数据。" in full_r
    assert full_v == "公开数据显示营收稳健。"


def test_web_mode_llm_dns_resolution_and_pinned_ip_anti_rebinding(monkeypatch):
    """Supervisor P1-C & P2 regression: wildcard DNS to internal/CGNAT IPs & DNS rebinding are blocked."""
    import socket as sock_mod
    import httpx
    from finresearch.core.guardrails import net_guard
    from finresearch.engine import model_client

    monkeypatch.setattr(settings, "fin_web", True)
    monkeypatch.setattr(settings, "fin_allow_private_endpoints", False)

    def fake_getaddrinfo(host, port, *_args, **_kwargs):
        if host == "lan.attacker-wildcard.org":
            return [(sock_mod.AF_INET, sock_mod.SOCK_STREAM, 6, "", ("192.168.1.10", port))]
        if host == "imds.attacker-wildcard.org":
            return [(sock_mod.AF_INET, sock_mod.SOCK_STREAM, 6, "", ("100.100.100.200", port))]
        if host == "public.vendor-api.org":
            return [(sock_mod.AF_INET, sock_mod.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        raise OSError("nxdomain")

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", fake_getaddrinfo)

    # Web mode _safe_llm_base_url resolves DNS and blocks private/metadata wildcard domains immediately
    assert model_client.build_llm(
        override={"base_url": "http://lan.attacker-wildcard.org:8080/v1", "api_key": "sk-x", "model": "m"}
    ) is None
    assert model_client.build_llm(
        override={"base_url": "http://imds.attacker-wildcard.org/v1", "api_key": "sk-x", "model": "m"}
    ) is None
    assert model_client.build_llm(
        override={"base_url": "https://public.vendor-api.org/v1", "api_key": "sk-x", "model": "m"}
    ) is not None

    # Pinned-IP safe httpx client blocks TOCTOU DNS rebinding at TCP connect time and pins the validated IP
    dns_answers = ["93.184.216.34", "127.0.0.1"]
    connected_hosts: list[str] = []

    def rebinding_getaddrinfo(_host, port, *_args, **_kwargs):
        ip = dns_answers.pop(0)
        return [(sock_mod.AF_INET, sock_mod.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", rebinding_getaddrinfo)

    async def verify_pinned_transport():
        async with net_guard.build_safe_async_client(
            allow_private_local=False, self_port=9527, trust_env=False
        ) as client:
            wrapper = client._transport._pool._network_backend

            async def fake_inner_connect(ip_addr, port, **_kw):
                connected_hosts.append(ip_addr)
                raise httpx.ConnectError("simulated-network-eof")

            monkeypatch.setattr(wrapper._inner, "connect_tcp", fake_inner_connect)

            # 1st connect resolves 93.184.216.34 and connects to the PINNED IP (not hostname)
            with pytest.raises(httpx.ConnectError, match="simulated-network-eof"):
                await client.get("https://rebind.attacker-wildcard.org/v1/models")
            assert connected_hosts == ["93.184.216.34"]

            # 2nd connect when DNS flips to 127.0.0.1 is rejected BEFORE inner connect_tcp is ever called!
            with pytest.raises(httpx.ConnectError, match="禁止连接内网"):
                await client.get("https://rebind.attacker-wildcard.org/v1/models")
            assert connected_hosts == ["93.184.216.34"]

    asyncio.run(verify_pinned_transport())


def test_live_ze_jing_688266_fixes(monkeypatch):
    """Regression suite for 688266.SH (泽璟制药) live trace failures:
    - web_search domestic fallback when Serper is disabled by default
    - search_industry single-page Legu parser & PE(TTM)/PE(静) summary formatting
    - tencent_kline fallback from qfqday -> day for zero-dividend stocks & 688xxx share->lot scaling
    """
    import json
    from bs4 import BeautifulSoup
    from finresearch.tools import industry as ind
    from finresearch.tools import market_provider as mp

    # 1. Domestic web_search fallback when _web_search is default & Serper is disabled
    monkeypatch.setattr(settings, "enable_serper_search", False)
    monkeypatch.setattr(
        registry,
        "_domestic_web_search",
        lambda q, limit=8: {
            "items": [
                {
                    "title": "泽璟制药披露新药临床进展",
                    "content": "三期临床达主要终点",
                    "snippet": "三期临床达主要终点",
                    "url": "https://finance.eastmoney.com/a/1.html",
                    "time": "2026-11-12",
                    "source": "东方财富资讯",
                }
            ],
            "provenance": {"source": "eastmoney_search", "endpoint": "search/jsonp", "fetched_at": "2026-11-12"},
        },
    )
    ws_run = asyncio.run(registry.web_search.run({"query": "泽璟制药 研发管线", "limit": 5}))
    assert ws_run.status == "success"
    assert "泽璟制药披露新药临床进展 — 三期临床达主要终点" in ws_run.content

    # 2. search_industry single-page HTML parser & _summarize PE(TTM) / PE(静)
    sample_html = """
    <div id="level2Items">
      <div class="lg-industries-item-chinese-title">801151.SI</div>
      <div class="lg-industries-item-number">化学制药(150)<span>(医药生物)</span></div>
      <div class="lg-sw-industries-item-value">
        <span class="value">37.30</span>
        <span class="value">32.05</span>
        <span class="value">3.04</span>
        <span class="value">1.41</span>
      </div>
    </div>
    """
    parsed_ind = ind._parse_legu_div(BeautifulSoup(sample_html, "lxml"), "level2Items", "申万二级")
    assert len(parsed_ind) == 1
    assert parsed_ind[0]["name"] == "化学制药"
    assert parsed_ind[0]["parent"] == "医药生物"
    assert parsed_ind[0]["pe_ttm"] == 32.05
    ind_summary = registry._summarize(
        "search_industry",
        {"items": parsed_ind, "levels_ok": 3, "levels_total": 3, "provenance": {"source": "sina", "fetched_at": "2026-11-12"}},
    )
    assert "PE(TTM)=32.05" in ind_summary and "PE(静)=37.3" in ind_summary and "PB=3.04" in ind_summary

    # 3. tencent_kline falls back from qfqday -> day for 688266.SH & scales 688xxx volume from shares to lots (/100)
    fake_kline_json = json.dumps({
        "code": 0,
        "data": {
            "sh688266": {
                "day": [
                    ["2026-11-11", "89.260", "88.590", "89.430", "85.610", "4964100.000"],
                    ["2026-11-12", "88.000", "87.610", "89.990", "85.250", "4237200.000"],
                ]
            }
        },
    })
    monkeypatch.setattr(mp, "_http_get", lambda *a, **kw: fake_kline_json)
    kl = mp.tencent_kline("688266.SH", n=2, adjust="qfq")
    assert len(kl["data"]) == 2
    assert kl["data"][-1]["close"] == 87.61
    assert kl["data"][-1]["volume"] == 42372  # 4,237,200 shares -> 42,372 lots (手)
    assert kl["provenance"]["raw_value"]["adjust"] == "qfq"


def test_zelgen_688266_full_deep_analysis_number_evidence_and_streaming():
    """Regression for the 688266.SH 泽璟制药 truncation after '吉卡昔替尼片纳入医保后销量大幅增':
    - News/web_search metrics (e.g. Q3 single-quarter 营收同比增长 52%, 亏损 174 万元, 去年同期亏损 9342 万元/1.21 亿元,
      商业化营收 5.5 亿元 +44.3%, 1000 万美元里程碑, 研发费用率 24%/22%) must not fail unsupported_metric when
      get_financial_indicators is also present in the turn.
    - search_industry PE(静) 37.3, get_kline interval return -14.72%, VaR(95%) 6.05%, and integer-rounded figures
      (3407.31万 -> 约3407万, -44.65% -> 约-45%, 59.03% -> 约59%) must pass both streaming and final checks.
    """
    from finresearch.api.routes import _NUMERIC_ARTIFACTS, _TEXT_EVIDENCE_TOOLS, _numeric_evidence
    from finresearch.core.guardrails.sanitizer import AnswerStreamSanitizer

    artifacts = [
        {
            "name": "get_realtime_quote",
            "artifact": {
                "code": "688266.SH",
                "name": "泽璟制药",
                "price": 87.98,
                "changePct": 0.42,
                "open": 86.81,
                "high": 88.44,
                "low": 86.31,
                "prevClose": 87.61,
                "volume": "3904手",
                "turnover": "3407.31万",
                "amount": 34073075.0,
                "marketCap": "232.89亿",
                "pe": 40.95,
                "pb": 13.55,
            },
        },
        {
            "name": "get_company_profile",
            "artifact": {
                "code": "688266.SH",
                "name": "泽璟制药-U",
                "industry": "化学制药",
                "market_cap": "232.84亿",
                "extra": {"总市值": 23283732040.56, "流通市值": 23283732040.56},
            },
        },
        {
            "name": "get_financial_indicators",
            "artifact": {
                "financials": {
                    "years": ["2022", "2023", "2024", "2025", "2026三季报"],
                    "periods": ["2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31", "2026-09-30"],
                    "yoy_base": {"period": "2025-09-30", "净利润": "-0.93亿", "ROE": "-8.1%", "毛利率": "90.4%", "资产负债率": "61.9%"},
                    "rows": [
                        {"name": "营业收入", "values": ["3.02亿", "3.86亿", "5.33亿", "8.10亿", "15.36亿"]},
                        {"name": "净利润", "values": ["-4.57亿", "-2.79亿", "-1.38亿", "-1.63亿", "6.38亿"]},
                        {"name": "营收同比", "values": ["58.8%", "27.8%", "37.9%", "52.1%", "158.9%"]},
                        {"name": "净利润同比", "values": ["-1.4%", "39.1%", "50.5%", "-18.2%", "783.3%"]},
                        {"name": "ROE", "values": ["-60.3%", "-17.1%", "-11.0%", "-15.1%", "37.1%"]},
                        {"name": "毛利率", "values": ["91.3%", "92.4%", "93.1%", "90.4%", "93.0%"]},
                        {"name": "资产负债率", "values": ["52.8%", "43.0%", "58.3%", "63.6%", "48.0%"]},
                    ],
                }
            },
        },
        {
            "name": "get_technical_indicators",
            "artifact": {
                "data": [
                    {"name": "MA5", "value": "88.80", "signal": "空头"},
                    {"name": "MA20", "value": "92.44", "signal": "空头"},
                    {"name": "MACD", "value": "1.01", "signal": "多头"},
                    {"name": "RSI(14)", "value": "39.2", "signal": "中性"},
                    {"name": "KDJ-J", "value": "42.9", "signal": "中性"},
                    {"name": "BOLL", "value": "92.44", "signal": "空头"},
                ]
            },
        },
        {
            "name": "get_kline",
            "artifact": {
                "symbol": "688266.SH",
                "period": "daily",
                "data": [
                    {"date": "2026-05-21", "open": 100.07, "close": 103.14, "high": 160.0, "low": 99.05, "volume": 48721, "ma5": 100.64, "ma10": 102.92, "ma20": 101.9, "ma60": 94.21},
                    {"date": "2026-11-13", "open": 86.81, "close": 87.96, "high": 88.44, "low": 78.37, "volume": 3910, "ma5": 88.79, "ma10": 88.59, "ma20": 92.44, "ma60": 106.15},
                ],
            },
        },
        {
            "name": "compute_risk_metrics",
            "artifact": {
                "symbol": "688266.SH",
                "benchmark": "000300.SH",
                "sample_days": 250,
                "annualized_volatility": 0.5903213932731777,
                "beta": 1.0006562410502575,
                "sharpe_ratio": -0.024125900074756568,
                "max_drawdown": -0.446472184531886,
                "drawdown_from": "2026-07-14",
                "drawdown_to": "2026-10-30",
                "var_95": 0.06046588785046728,
                "cvar_95": 0.08479680827085487,
            },
        },
        {
            "name": "search_industry",
            "artifact": {
                "items": [{"level": "申万二级", "code": "801151.SI", "name": "化学制药", "pe_static": 37.3, "pe_ttm": 32.05, "pb": 3.04, "dividend_yield": 1.41}],
                "levels_ok": 3,
                "levels_total": 3,
            },
        },
        {
            "name": "search_news",
            "artifact": {
                "items": [
                    {
                        "title": "泽璟制药：第三季度亏损同比收窄至174万元，营收同比增长52%",
                        "content": "10月30日，泽璟制药(688266)公布2026年三季报，公司营业收入为15.4亿元，同比上升158.9%；归母净利润自去年同期亏损9342万元成功扭亏，实现归母净利润6.38亿元；扣非归母净利润自去年同期亏损1.21亿元成功扭亏。",
                    },
                    {
                        "title": "机构风向标 | 泽璟制药(688266)2026年三季度已披露前十大机构累计持仓占比17.27%",
                        "content": "共有79个机构投资者披露持有泽璟制药A股股份，合计持股量达6367.63万股，占泽璟制药总股本的24.06%。",
                    },
                ]
            },
        },
        {
            "name": "web_search",
            "artifact": {
                "items": [
                    {
                        "title": "从出海授权攀升到商业化放量：创新药正重塑药企财报",
                        "content": "去年12月30日，公司将ZG006授权艾伯维，首付款1亿美元于今年1月到账。截至2026年上半年，公司已有4款创新药上市。上半年，公司商业化产品销售收入5.5亿元，同比增长44.3%。",
                    },
                    {
                        "title": "泽璟制药：收到艾伯维1000万美元里程碑付款",
                        "content": "10月20日，泽璟制药公告收到艾伯维1000万美元里程碑付款。2026Q1-3研发费用率、销售费用率分别降至约24%、22%（2025年为39%、34%）。",
                    },
                ]
            },
        },
    ]

    evidence_blocks = ["深入分析泽璟制药情况。"]
    evidence_fields: dict[str, list] = {}
    has_text_evidence = False
    for ev in artifacts:
        s = _numeric_evidence(ev)
        if s:
            evidence_blocks.append(s)
            has_text_evidence |= ev["name"] in _TEXT_EVIDENCE_TOOLS
            if ev["name"] in _NUMERIC_ARTIFACTS:
                for m, vals in metric_values(ev["artifact"]).items():
                    evidence_fields.setdefault(m, []).extend(vals)

    ne = NumberEvidence(
        "\n".join(evidence_blocks),
        evidence_fields,
        strict_fields=bool(evidence_fields) and not has_text_evidence,
    )

    full_reply = (
        "【核心结论】💡 泽璟制药2026年三季报“扭亏为盈”的主要动力来自 ZG006 授权艾伯维的 1 亿美元首付款，"
        "产品销售本身尚未实现盈亏平衡（Q3 单季仍亏 174 万元）。因此当前 PE 40.95 倍、PB 13.55 倍的分母被一次性收入抬高；"
        "现价 87.98 元仍在 MA20 92.44 元、MA60 106.15 元之下。\n\n"
        "💊 二、真实经营主线：4 款产品商业化，处于差异化放量阶段\n"
        "· 【事实】2026 年上半年商业化产品销售收入 5.5 亿元，同比增长 44.3% [4]。\n"
        "· 【事实】公司口径的增长驱动为重组人凝血酶继续同比增长、吉卡昔替尼片纳入医保后销量大幅增长 [5]；"
        "第三季度单季营收同比增长 52%，单季归母净利润亏损同比收窄至 174 万元（去年同期归母净利润亏损 9342 万元、扣非净利润亏损 1.21 亿元）[7]。\n"
        "· 【事实】2026 年 10 月 20 日公司再收到艾伯维 1000 万美元（约 0.1 亿美元）里程碑付款；2026Q1-3 研发费用率约 24%、销售费用率约 22%（2025 年为 39%、34%）。\n"
        "💰 三、估值对比：化学制药（申万二级）PE(TTM) 32.05 倍、PE(静) 37.3 倍、PB 3.04 倍、股息率 1.41%，总市值 232.89 亿元（流通市值 232.84 亿元）。\n"
        "📈 四、技术与风险：早盘成交额 3407.31 万元（约 3407 万元），成交量 3904 手；"
        "日 K 线末根收盘 87.96 元，区间涨跌幅 -14.72%，区间高低 160.0 元 / 78.37 元；"
        "250 日年化波动率 59.03%（约 59%），Beta 1.0007，Sharpe -0.0241，最大回撤 -44.65%（约 -45%），VaR(95%) 6.05%，CVaR(95%) 8.48%。\n"
        "🏛️ 五、机构持仓：79 家机构合计持股 6367.63 万股，占总股本 24.06%，前十大机构累计持仓占比 17.27%。\n"
        "仅供参考，不构成投资建议。"
    )
    assert ne.check(full_reply, streaming=False, financial_only=True) == []

    sanitizer = AnswerStreamSanitizer(lambda ans, streaming=False: ne.check(ans, streaming=streaming, financial_only=True))
    pieces = []
    for idx in range(0, len(full_reply), 2):
        delta = sanitizer.feed(full_reply[idx : idx + 2])
        assert not delta.compliance_triggered
        pieces.append(delta.visible_text)
    tail = sanitizer.flush()
    assert not tail.compliance_triggered
    pieces.append(tail.visible_text)
    assert "".join(pieces) == full_reply

    # Unsubstantiated numbers and Chinese-numeral fabrications must still be rejected
    assert ne.check("公司 2026 三季报实现营业收入 19.88 亿元。", financial_only=True) == ["unsupported_number"]
    assert ne.check("公司总市值达到三百八十亿元。", financial_only=True) == ["unsupported_number"]


def test_zelgen_688266_intraday_negative_drop_and_bound_hardening():
    """Regression for 10:29 live cutoff ('· 现价 86' before '.8 元（跌 0.92%）') and all latent guardrail traps."""
    from finresearch.api.routes import _NUMERIC_ARTIFACTS, _TEXT_EVIDENCE_TOOLS, _numeric_evidence
    from finresearch.core.guardrails.number_evidence import NumberEvidence, metric_values
    from finresearch.core.guardrails.sanitizer import AnswerStreamSanitizer

    artifacts = [
        {
            "name": "get_realtime_quote",
            "artifact": {
                "symbol": "688266.SH",
                "name": "泽璟制药-U",
                "price": 86.8,
                "change": -0.81,
                "changePct": -0.92,
                "volume": 11547,
                "turnover": 100856432.0,
                "pe": 40.4,
                "pb": 13.37,
                "roe": 0.0,
                "marketCap": 22977000000.0,
            },
        },
        {
            "name": "get_financial_indicators",
            "artifact": {
                "data": {
                    "periods": ["2022", "2023", "2024", "2025", "2026三季报"],
                    "rows": [
                        {"name": "净利润", "values": ["-4.57亿", "-2.79亿", "-1.38亿", "-1.63亿", "+6.38亿"]},
                        {"name": "毛利率", "values": ["91.3%", "92.4%", "93.1%", "90.4%", "93.0%"]},
                        {"name": "资产负债率", "values": ["52.8%", "43.0%", "58.3%", "63.6%", "48.0%"]},
                    ],
                }
            },
        },
        {
            "name": "compute_risk_metrics",
            "artifact": {
                "symbol": "688266.SH",
                "annualized_volatility": 0.5904,
                "max_drawdown": -0.4465,
                "drawdown_from": "2026-07-14",
                "drawdown_to": "2026-10-30",
            },
        },
        {
            "name": "web_search",
            "artifact": {
                "items": [
                    {
                        "title": "泽璟制药三季报",
                        "content": "公司盈余公积为0，未分配利润仍为负值；ZG006处于1/2期临床阶段。",
                    }
                ]
            },
        },
    ]

    evidence_blocks = ["深入分析泽璟制药情况。"]
    evidence_fields: dict[str, list] = {}
    has_text_evidence = False
    for ev in artifacts:
        s = _numeric_evidence(ev)
        if s:
            evidence_blocks.append(s)
            has_text_evidence |= ev["name"] in _TEXT_EVIDENCE_TOOLS
            if ev["name"] in _NUMERIC_ARTIFACTS:
                for m, vals in metric_values(ev["artifact"]).items():
                    evidence_fields.setdefault(m, []).extend(vals)

    ne = NumberEvidence(
        "\n".join(evidence_blocks),
        evidence_fields,
        strict_fields=bool(evidence_fields) and not has_text_evidence,
    )

    reply = (
        "📊 估值与盘面（10:29:54）：\n"
        "· 现价 86.8 元（跌 0.92%，跌 0.81 元），现价跌破 90 元，总市值 229.77 亿元，成交额 1.01 亿元，成交量 1.15 万手。\n"
        "· 2022—2025年归母净利润连续亏损，分别为 4.57 亿元、2.79 亿元、1.38 亿元、1.63 亿元；"
        "毛利率连续保持在 90% 以上，资产负债率降至 50% 以下，盈余公积为 0 元。\n"
        "· 最大回撤（2026-07-14 至 2026-10-30）达 44.65%（超过 40%），年化波动率接近 60%。\n"
        "· 公司专注创新药（ZG006 处于 1/2 期临床），并非三元材料、二元结构或二手车行业，研发与商业化两手推进。\n"
        "仅供参考，不构成投资建议。"
    )
    assert ne.check(reply, streaming=False, financial_only=True) == []

    for step in (1, 2):
        sanitizer = AnswerStreamSanitizer(
            lambda ans, streaming=False: ne.check(ans, streaming=streaming, financial_only=True)
        )
        emitted_chunks = []
        for idx in range(0, len(reply), step):
            delta = sanitizer.feed(reply[idx : idx + step])
            assert not delta.compliance_triggered
            if delta.visible_text:
                # Ensure sanitizer never splits a decimal literal mid-number
                assert not delta.visible_text.endswith("86")
                emitted_chunks.append(delta.visible_text)
        tail = sanitizer.flush()
        assert not tail.compliance_triggered
        emitted_chunks.append(tail.visible_text)
        assert "".join(emitted_chunks) == reply

    # Adversarial checks: directional violations, positive-flip hallucinations, and Chinese numerals must still fail
    assert ne.check("资产负债率超过 70%。", financial_only=True) == ["unsupported_number"]
    assert ne.check("最大回撤超过 50%。", financial_only=True) == ["unsupported_number"]
    assert ne.check("最大回撤达 40%。", financial_only=True) == ["unsupported_unit"]
    assert ne.check("去年亏损 1.38 亿元，今年扭亏实现归母净利润 1.38 亿元。", financial_only=True) == ["unsupported_number"]
    assert ne.check("股价上涨至三元。", financial_only=True) == ["unsupported_number"]




