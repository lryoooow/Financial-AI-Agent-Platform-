"""审计 B1：secrets 强化（DPAPI 首选 / 旧 enc: 兼容 / 解密失败返空 / 嵌套键名）。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finresearch.tools import secrets  # noqa: E402


def test_roundtrip_current_scheme():
    cipher = secrets.encrypt_value("sk-test-1234567890")
    assert secrets.decrypt_value(cipher) == "sk-test-1234567890"
    # Windows 首选 DPAPI，否则旧自研；两者都必须可往返
    assert cipher.startswith(("dpapi:", "enc:"))


@pytest.mark.skipif(not secrets._dpapi_available(), reason="非 Windows 或无 win32crypt")
def test_dpapi_is_preferred_on_windows():
    assert secrets.encrypt_value("k").startswith("dpapi:")


def test_legacy_enc_prefix_still_decrypts():
    # 用旧方案手工构造一段密文（绕过 DPAPI 首选），验证存量数据兼容
    plaintext = "legacy-secret-key"
    pt = plaintext.encode("utf-8")
    nonce = b"\x01" * 16
    ks = secrets._keystream(secrets._derive_key(), nonce, len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, ks))
    import base64

    stored = "enc:" + base64.b64encode(nonce + ct).decode("ascii")
    assert secrets.decrypt_value(stored) == plaintext


def test_decrypt_failure_returns_empty_not_ciphertext():
    # v1.5 缺陷：解密失败返回密文原文，enc:Base64... 会被当 API Key 发给网关
    assert secrets.decrypt_value("enc:不是合法base64!!!") == ""
    assert secrets.decrypt_value("dpapi:AAAA-not-a-valid-blob") == ""


def test_plaintext_passthrough_unchanged():
    assert secrets.decrypt_value("sk-plain-value") == "sk-plain-value"
    assert secrets.decrypt_value("") == ""
    assert secrets.decrypt_value(None) == ""


def test_encrypt_skip_when_already_encrypted_nested():
    payload = {"deepseek": {"apiKey": "sk-abc", "baseUrl": "https://x"}}
    once = secrets.encrypt_nested(payload)
    key_once = once["deepseek"]["apiKey"]
    assert key_once.startswith(("dpapi:", "enc:"))
    twice = secrets.encrypt_nested(once)
    assert twice["deepseek"]["apiKey"] == key_once, "已加密值不得二次加密"
    assert secrets.decrypt_nested(once) == payload


def test_config_roundtrip_both_key_names():
    cfg = {"api_key": "sk-snake", "apiKey": "sk-camel", "model": "m"}
    enc = secrets.encrypt_config(cfg)
    assert enc["api_key"].startswith(("dpapi:", "enc:"))
    assert enc["apiKey"].startswith(("dpapi:", "enc:"))
    assert enc["model"] == "m"
    assert secrets.decrypt_config(enc) == cfg


@pytest.mark.skipif(not secrets._dpapi_available(), reason="非 Windows 或无 win32crypt")
def test_dpapi_tamper_detected():
    cipher = secrets.encrypt_value("sk-tamper-check")
    # 篡改密文主体（同前缀），DPAPI 完整性校验应失败 → 返回空串
    body = cipher.split(":", 1)[1]
    flipped = ("A" if body[0] != "A" else "B") + body[1:]
    assert secrets.decrypt_value(cipher.split(":", 1)[0] + ":" + flipped) == ""
