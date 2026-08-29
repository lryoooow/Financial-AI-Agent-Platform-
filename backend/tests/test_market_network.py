"""审计 B6：行情网络层（注册表代理物化 / 通配符清洗 / 熔断）。"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_ENV_KEYS = ("NO_PROXY", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY")


def _snapshot_env():
    return {k: os.environ.get(k) for k in _ENV_KEYS}


def _restore_env(snapshot):
    for k, v in snapshot.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_registry_proxy_materialized_before_no_proxy(monkeypatch):
    """仅设 NO_PROXY 会让 getproxies_environment() 非空从而屏蔽注册表代理 —— 先物化再 bypass。"""
    import urllib.request

    import finresearch.tools.market_provider as mp

    snap = _snapshot_env()
    try:
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        monkeypatch.setattr(
            urllib.request, "getproxies_registry",
            lambda: {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
        )
        summary = mp.configure_market_network()
        assert os.environ.get("HTTP_PROXY") == "http://127.0.0.1:7890"
        assert os.environ.get("HTTPS_PROXY") == "http://127.0.0.1:7890"
        # 行情域名仍进 NO_PROXY（bypass 与代理共存）
        assert "push2.eastmoney.com" in os.environ.get("NO_PROXY", "")
        assert summary.get("materialized_registry_proxy")
    finally:
        _restore_env(snap)


def test_existing_env_proxy_not_overwritten(monkeypatch):
    import urllib.request

    import finresearch.tools.market_provider as mp

    snap = _snapshot_env()
    try:
        os.environ["HTTP_PROXY"] = "http://env-proxy:1"
        monkeypatch.setattr(
            urllib.request, "getproxies_registry",
            lambda: {"http": "http://registry-proxy:2"},
        )
        mp.configure_market_network()
        assert os.environ["HTTP_PROXY"] == "http://env-proxy:1", "env 显式配置优先"
    finally:
        _restore_env(snap)


def test_wildcard_no_proxy_entries_normalized():
    import finresearch.tools.market_provider as mp

    snap = _snapshot_env()
    try:
        os.environ["NO_PROXY"] = "*.eastmoney.com,localhost"
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
        mp.configure_market_network()
        val = os.environ["NO_PROXY"]
        assert "*.eastmoney.com" not in val, "Python endswith 匹配吃不下通配符形态"
        assert "eastmoney.com" in val and "localhost" in val
    finally:
        _restore_env(snap)


def test_company_profile_direct_circuit_breaker(monkeypatch):
    """push2 直连失败后 5 分钟熔断：窗口内不再发网络请求。"""
    import finresearch.tools.fundamentals as fundamentals

    calls = {"n": 0}
    snap = _snapshot_env()

    class _Boom(Exception):
        pass

    def fake_get(*_a, **_k):
        calls["n"] += 1
        raise _Boom("connection reset")

    monkeypatch.setattr(fundamentals, "_direct_session", lambda: type("S", (), {"get": staticmethod(fake_get)})())
    fundamentals._DIRECT_FAIL_AT = 0.0
    try:
        for _ in range(3):
            try:
                fundamentals._company_profile_direct("600519")
            except (_Boom, ConnectionError):
                pass  # 首次 _Boom（并触发熔断记时），后续窗口内是 ConnectionError（不发网络）
        assert calls["n"] == 1, "首次失败后熔断，窗口内不再发请求"
        # 熔断中的失败是 ConnectionError（不发网络）
        try:
            fundamentals._company_profile_direct("600519")
            raised = None
        except ConnectionError as e:
            raised = e
        assert raised is not None and "熔断" in str(raised)
        assert calls["n"] == 1
        # 窗口过后恢复发请求
        fundamentals._DIRECT_FAIL_AT = time.time() - 301
        try:
            fundamentals._company_profile_direct("600519")
        except _Boom:
            pass
        assert calls["n"] == 2
    finally:
        fundamentals._DIRECT_FAIL_AT = 0.0
        _restore_env(snap)
