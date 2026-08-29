"""审计 B3/B8/B11：shutdown 端点、行业 ticker 透传、LLM 默认超时、回放窗口常量。"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finresearch.api import routes  # noqa: E402


def test_shutdown_endpoint_sets_should_exit(monkeypatch):
    monkeypatch.setattr(routes, "_shutdown_token", lambda: "tok-123")
    fake_server = SimpleNamespace(should_exit=False)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(uvicorn_server=fake_server)),
        headers={"x-shutdown-token": "tok-123"},
    )
    result = asyncio.run(routes.shutdown_backend(request))
    assert result["ok"] is True
    assert fake_server.should_exit is True


def test_shutdown_endpoint_rejects_missing_or_wrong_token(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(routes, "_shutdown_token", lambda: "tok-123")
    fake_server = SimpleNamespace(should_exit=False)
    for headers in ({}, {"x-shutdown-token": "wrong"}, {"x-shutdown-token": ""}):
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(uvicorn_server=fake_server)),
            headers=headers,
        )
        try:
            asyncio.run(routes.shutdown_backend(request))
            raise AssertionError(f"应 403：{headers}")
        except HTTPException as e:
            assert e.status_code == 403
    assert fake_server.should_exit is False, "被拒请求不得触发停机"


def test_shutdown_endpoint_without_server_handle(monkeypatch):
    monkeypatch.setattr(routes, "_shutdown_token", lambda: "tok-123")
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
        headers={"x-shutdown-token": "tok-123"},
    )
    result = asyncio.run(routes.shutdown_backend(request))
    assert result["ok"] is False  # 源码直跑模式（无挂载）不误报成功


def test_llm_default_timeout_and_retries(monkeypatch):
    import finresearch.llm as llm_mod

    captured = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(llm_mod, "ChatOpenAI", _FakeChatOpenAI)
    llm_mod.build_llm(override={"api_key": "k", "model": "m", "base_url": "http://x"})
    assert captured["timeout"] == 120.0, "此前 openai 客户端默认 600s 总超时"
    assert captured["max_retries"] == 1
    # 显式覆盖尊重调用方
    llm_mod.build_llm(override={"api_key": "k", "model": "m", "base_url": "http://x"},
                      timeout=30, max_retries=0)
    assert captured["timeout"] == 30 and captured["max_retries"] == 0


def test_replay_window_covers_full_section_stream():
    """600→3000：四段真流式 ≈1200-1700 帧 section_token，旧窗口把摘要/早期段落挤出回放。"""
    import inspect

    src = inspect.getsource(routes)
    assert "len(runtime.history) > 3000" in src
    assert "len(runtime.history) - 3000" in src


def test_industry_ticker_passthrough(monkeypatch):
    """B8：申万行业码 801735 不得被 to_market_ticker 错转成 .SZ。"""
    import finresearch.agents.deep.research_tools as rt

    pushed = {}
    monkeypatch.setattr(rt, "_stream_push", lambda msg: pushed.update(msg))
    rt._emit_summary({"normalized_ticker": "801735", "facts": {}}, "光伏行业景气上行")
    assert pushed["ticker"] == "801735", "行业码必须原样透传"

    pushed.clear()
    rt._emit_summary({"normalized_ticker": "801735", "is_industry": True, "facts": {}}, "摘要")
    assert pushed["ticker"] == "801735", "is_industry 标志生效"

    pushed.clear()
    rt._emit_summary({"normalized_ticker": "600519", "facts": {}}, "摘要")
    assert pushed["ticker"] == "600519.SH", "六位个股代码仍转市场后缀格式"


def test_chat_lock_helper_caps_and_returns_lock():
    lock = routes._chat_lock("conv-1")
    assert lock is routes._chat_lock("conv-1")
    assert isinstance(lock, asyncio.Lock)


def test_version_consistency():
    """D5：版本单一来源（pyproject == __version__ == 前端 package.json），main.py 引用 __version__。"""
    import json
    import tomllib

    from finresearch import __version__

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    assert data["project"]["version"] == __version__
    fronted_pkg = Path(__file__).resolve().parent.parent.parent / "fronted" / "package.json"
    assert json.loads(fronted_pkg.read_text(encoding="utf-8"))["version"] == __version__, \
        "前端 package.json 版本必须与后端一致"
    main_src = (Path(__file__).resolve().parent.parent / "src" / "finresearch" / "main.py").read_text(encoding="utf-8")
    assert 'version=__version__' in main_src, "FastAPI(version=) 必须引用 __version__"
