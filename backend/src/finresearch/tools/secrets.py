"""机器绑定的轻量对称加密（供 API Key 落库前加密、读取时解密）。

设计：
- 密钥由本机特征（节点名 + MAC）经 PBKDF2-HMAC-SHA256 派生，绑定到当前机器。
  数据库被复制到其他机器后无法解密，降低整库明文泄露风险。
- 算法：随机 nonce(16B) 派生 keystream(SHA256 计数器模式) 与明文 XOR，base64 编码。
  非军用强度，但在"防止数据库明文泄露 API Key"这一有限目标上足够，且零外部依赖、
  PyInstaller 友好（无需 C 扩展或联网取 BPE 词表）。
- 前缀 "enc:" 区分密文与旧明文：读取时无前缀原样返回，升级零迁移、解密失败回退原文。

API：
- encrypt_value(plaintext) -> str  （带 "enc:" 前缀）
- decrypt_value(stored)    -> str  （无前缀原样返回；有前缀解密失败回退原文，绝不抛异常）
- encrypt_config(config)   -> dict  （深拷贝并对 api_key 加密；None/非 dict 原样返回）
- decrypt_config(config)   -> dict  （深拷贝并对 api_key 解密）
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import uuid
from functools import lru_cache

_PREFIX = "enc:"
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


def encrypt_value(plaintext: str) -> str:
    """加密单个字符串，返回带 "enc:" 前缀的 base64 文本。空串原样返回。"""
    if not plaintext:
        return plaintext
    pt = plaintext.encode("utf-8")
    nonce = os.urandom(_NONCE_LEN)
    ks = _keystream(_derive_key(), nonce, len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, ks))
    return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt_value(stored: str) -> str:
    """解密。无 "enc:" 前缀原样返回（兼容旧明文）；解密失败回退原文，绝不抛异常。"""
    if not stored or not isinstance(stored, str):
        return stored or ""
    if not stored.startswith(_PREFIX):
        return stored
    try:
        raw = base64.b64decode(stored[len(_PREFIX):])
        nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        ks = _keystream(_derive_key(), nonce, len(ct))
        pt = bytes(a ^ b for a, b in zip(ct, ks))
        return pt.decode("utf-8")
    except Exception:  # noqa: BLE001
        return stored


def encrypt_config(config):
    """深拷贝 config dict，对其中的 api_key 加密。None/非 dict 原样返回。"""
    if not isinstance(config, dict):
        return config
    out = dict(config)
    key = out.get("api_key")
    if isinstance(key, str) and key:
        out["api_key"] = encrypt_value(key)
    return out


def decrypt_config(config):
    """深拷贝 config dict，对其中的 api_key 解密。None/非 dict 原样返回。"""
    if not isinstance(config, dict):
        return config
    out = dict(config)
    key = out.get("api_key")
    if isinstance(key, str) and key:
        out["api_key"] = decrypt_value(key)
    return out
