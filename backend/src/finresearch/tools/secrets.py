"""机器绑定的密钥保护（供 API Key 落库前加密、读取时解密）。

v1.6.0（审计 B1）强化，两级方案：
- 首选 **Windows DPAPI**（win32crypt.CryptProtectData，entropy 绑定本机特征）：
  系统级用户凭据保护，非本机/本用户不可解，且带完整性校验（篡改可检出）。
  前缀 "dpapi:"。
- 回落 **旧自研方案**（"enc:"，主机名+MAC 派生密钥的 XOR 流）：仅非 Windows 或
  DPAPI 不可用时使用。保留解密能力以平滑迁移存量数据；下次保存自动升级为 dpapi:。

行为铁律：
- 解密失败返回 **空串**（v1.5 及之前返回密文原文是缺陷：`enc:Base64...` 会被当作
  API Key 发给模型网关，产生极难排查的鉴权失败）。
- 无前缀原样返回（兼容历史明文数据），升级零迁移。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import uuid
from functools import lru_cache

_PREFIX = "enc:"
_DPAPI_PREFIX = "dpapi:"
_NONCE_LEN = 16


def _machine_fingerprint() -> bytes:
    """本机特征：节点名 + 第一块网卡 MAC。跨机器不同；同机稳定。"""
    node = socket.gethostname() or "unknown-host"
    try:
        mac = uuid.getnode()
        mac_str = ":".join(f"{(mac >> ele) & 0xff:02x}" for ele in range(40, -1, -8))
    except Exception:  # noqa: BLE001
        mac_str = "00:00:00:00:00:00"
    return f"{node}|{mac_str}".encode("utf-8")


@lru_cache
def _derive_key() -> bytes:
    """由机器特征派生 32B 密钥。"""
    return hashlib.pbkdf2_hmac(
        "sha256", _machine_fingerprint(), b"finresearch-secrets-salt", 100_000, dklen=32
    )


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """SHA256 计数器模式 keystream。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(nonce + counter.to_bytes(8, "big") + key).digest()
        counter += 1
    return bytes(out[:length])


def _dpapi_available() -> bool:
    if os.name != "nt":
        return False
    try:
        from win32 import win32crypt  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _dpapi_protect(plaintext: str) -> str:
    from win32 import win32crypt

    blob = win32crypt.CryptProtectData(
        plaintext.encode("utf-8"), "finresearch", _machine_fingerprint(), None, None, 0
    )
    return _DPAPI_PREFIX + base64.b64encode(blob).decode("ascii")


def _dpapi_unprotect(stored: str) -> str:
    from win32 import win32crypt

    blob = base64.b64decode(stored[len(_DPAPI_PREFIX):])
    _desc, data = win32crypt.CryptUnprotectData(blob, _machine_fingerprint(), None, None, 0)
    return data.decode("utf-8")


def encrypt_value(plaintext: str) -> str:
    """加密单个字符串。Windows 首选 DPAPI（"dpapi:"），否则回落自研（"enc:"）。空串原样返回。"""
    if not plaintext:
        return plaintext
    if _dpapi_available():
        try:
            return _dpapi_protect(plaintext)
        except Exception:  # noqa: BLE001
            pass  # DPAPI 异常时回落旧方案，绝不因加密失败阻塞保存
    pt = plaintext.encode("utf-8")
    nonce = os.urandom(_NONCE_LEN)
    ks = _keystream(_derive_key(), nonce, len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, ks))
    return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt_value(stored: str) -> str:
    """解密。无前缀原样返回（兼容旧明文）；解密失败返回空串（绝不返回密文冒充明文）。"""
    if not stored or not isinstance(stored, str):
        return stored or ""
    if stored.startswith(_DPAPI_PREFIX):
        try:
            return _dpapi_unprotect(stored)
        except Exception:  # noqa: BLE001
            return ""
    if not stored.startswith(_PREFIX):
        return stored
    try:
        raw = base64.b64decode(stored[len(_PREFIX):])
        nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        ks = _keystream(_derive_key(), nonce, len(ct))
        pt = bytes(a ^ b for a, b in zip(ct, ks))
        return pt.decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


# 密钥字段两种键名并存：后端 LLM 配置用 snake_case（api_key），
# 前端 model-configs 用 camelCase（apiKey）。
_KEY_FIELDS = ("api_key", "apiKey")


def encrypt_config(config):
    """深拷贝 config dict，对其中的密钥字段加密。None/非 dict 原样返回。"""
    if not isinstance(config, dict):
        return config
    out = dict(config)
    for field in _KEY_FIELDS:
        key = out.get(field)
        if isinstance(key, str) and key:
            out[field] = encrypt_value(key)
    return out


def decrypt_config(config):
    """深拷贝 config dict，对其中的密钥字段解密。None/非 dict 原样返回。"""
    if not isinstance(config, dict):
        return config
    out = dict(config)
    for field in _KEY_FIELDS:
        key = out.get(field)
        if isinstance(key, str) and key:
            out[field] = decrypt_value(key)
    return out


def encrypt_nested(obj):
    """递归加密 dict/list 树中所有密钥字段（api_key / apiKey）。

    供 model-configs 这类「{厂商id: {apiKey: ...}}」嵌套形状使用。
    已加密值（dpapi:/enc: 前缀）原样跳过，杜绝二次加密。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _KEY_FIELDS and isinstance(v, str) and v and not v.startswith((_DPAPI_PREFIX, _PREFIX)):
                out[k] = encrypt_value(v)
            else:
                out[k] = encrypt_nested(v)
        return out
    if isinstance(obj, list):
        return [encrypt_nested(x) for x in obj]
    return obj


def decrypt_nested(obj):
    """递归解密 dict/list 树中所有密钥字段；无前缀值（历史明文）原样保留。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _KEY_FIELDS and isinstance(v, str) and v:
                out[k] = decrypt_value(v)
            else:
                out[k] = decrypt_nested(v)
        return out
    if isinstance(obj, list):
        return [decrypt_nested(x) for x in obj]
    return obj
