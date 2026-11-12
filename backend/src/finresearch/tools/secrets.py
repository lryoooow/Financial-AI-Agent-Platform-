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
import ctypes
import ctypes.wintypes
import hashlib
import os
import re
import socket
import uuid
from functools import lru_cache

_PREFIX = "enc:"
_DPAPI_PREFIX = "dpapi:"
_DPAPI_V2_ENTROPY = b"finresearch-dpapi-v2"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_MASK_PREFIX = "••••••••"
_MASK_EXACT_RE = re.compile(r"^•{8}(?:.{4})?$")
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


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


@lru_cache(maxsize=1)
def _dpapi_funcs():
    """加载 Windows 系统自带 crypt32/kernel32 DPAPI 函数（零第三方依赖，打包与虚拟环境均原生生效）。"""
    if os.name != "nt":
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        protect = crypt32.CryptProtectData
        protect.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            ctypes.wintypes.LPCWSTR,
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        protect.restype = ctypes.wintypes.BOOL

        unprotect = crypt32.CryptUnprotectData
        unprotect.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        unprotect.restype = ctypes.wintypes.BOOL

        local_free = kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        return protect, unprotect, local_free
    except Exception:  # noqa: BLE001
        return None


def _make_blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), buf), buf


def _dpapi_available() -> bool:
    return _dpapi_funcs() is not None


def _dpapi_protect(plaintext: str) -> str:
    funcs = _dpapi_funcs()
    if funcs is None:
        raise RuntimeError("DPAPI unavailable")
    protect, _unprotect, local_free = funcs
    # v1.8.1：Windows DPAPI 已由操作系统绑定到当前登录用户凭据与本机密钥环，
    # 不再把易变的网卡 MAC（Wi-Fi 随机 MAC / 切换热点 / VPN 虚拟网卡）混入 entropy，
    # 避免切换网络后已保存的 API Key 静默解密为空。
    in_blob, _in_buf = _make_blob(plaintext.encode("utf-8"))
    ent_blob, _ent_buf = _make_blob(_DPAPI_V2_ENTROPY)
    out_blob = _DATA_BLOB()
    ok = protect(
        ctypes.byref(in_blob),
        "finresearch",
        ctypes.byref(ent_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            local_free(out_blob.pbData)
    return _DPAPI_PREFIX + base64.b64encode(raw).decode("ascii")


def _dpapi_unprotect_raw(blob_bytes: bytes, entropy: bytes) -> bytes:
    funcs = _dpapi_funcs()
    if funcs is None:
        raise RuntimeError("DPAPI unavailable")
    _protect, unprotect, local_free = funcs
    in_blob, _in_buf = _make_blob(blob_bytes)
    ent_blob, _ent_buf = _make_blob(entropy)
    out_blob = _DATA_BLOB()
    ok = unprotect(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(ent_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            local_free(out_blob.pbData)


def _dpapi_unprotect(stored: str) -> str:
    blob = base64.b64decode(stored[len(_DPAPI_PREFIX):])
    try:
        return _dpapi_unprotect_raw(blob, _DPAPI_V2_ENTROPY).decode("utf-8")
    except Exception:  # noqa: BLE001
        # 平滑兼容 v1.6~v1.8.0 旧版以 _machine_fingerprint() 为 entropy 写入的密文
        return _dpapi_unprotect_raw(blob, _machine_fingerprint()).decode("utf-8")


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


def is_masked_secret(val) -> bool:
    """严格判断是否为未改动的脱敏占位符（•••••••• 或 ••••••••xxxx）。
    绝不用 startswith：防止用户误在脱敏串后粘贴新 Key（如 ••••••••1234sk-xxx）被误判为未修改而静默丢弃。
    """
    return isinstance(val, str) and bool(_MASK_EXACT_RE.fullmatch(val))


def mask_secret(stored_or_plain: str) -> str:
    """将明文或密文 API Key 转为展示用脱敏串（仅保留末 4 位，短密钥不露尾）。"""
    if not stored_or_plain or not isinstance(stored_or_plain, str):
        return ""
    if is_masked_secret(stored_or_plain):
        return stored_or_plain
    plain = decrypt_value(stored_or_plain).strip()
    if not plain:
        return ""
    if len(plain) <= 6:
        return _MASK_PREFIX
    return _MASK_PREFIX + plain[-4:]


def encrypt_config(config):
    """深拷贝 config dict，对其中的密钥字段加密。None/非 dict 原样返回。"""
    if not isinstance(config, dict):
        return config
    out = dict(config)
    for field in _KEY_FIELDS:
        key = out.get(field)
        if isinstance(key, str) and key:
            if is_masked_secret(key):
                out[field] = ""
            elif not key.startswith((_DPAPI_PREFIX, _PREFIX)):
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
            out[field] = "" if is_masked_secret(key) else decrypt_value(key)
    return out


def mask_config(config):
    """深拷贝 config dict，将其中的密钥字段替换为安全摘要（不含明文或密文）。"""
    if not isinstance(config, dict):
        return config
    out = {}
    has_key = False
    for k, v in config.items():
        if k in _KEY_FIELDS:
            if isinstance(v, str) and v and bool(decrypt_value(v).strip() if not is_masked_secret(v) else True):
                has_key = True
            continue
        out[k] = v
    out["has_api_key"] = has_key
    return out


def merge_masked_nested(incoming, existing):
    """保存嵌套配置时，若前端回传的是未改动的脱敏占位符，保留数据库中既有的密文/明文。"""
    if isinstance(incoming, dict):
        ex_dict = existing if isinstance(existing, dict) else {}
        out = {}
        for k, v in incoming.items():
            if k in _KEY_FIELDS and isinstance(v, str) and is_masked_secret(v):
                prev = ex_dict.get(k)
                out[k] = prev if isinstance(prev, str) and not is_masked_secret(prev) else ""
            else:
                out[k] = merge_masked_nested(v, ex_dict.get(k))
        return out
    if isinstance(incoming, list):
        ex_list = existing if isinstance(existing, list) else []
        return [
            merge_masked_nested(x, ex_list[i] if i < len(ex_list) else None)
            for i, x in enumerate(incoming)
        ]
    return incoming


def mask_nested(obj):
    """递归将 dict/list 树中所有密钥字段脱敏为 ••••••••xxxx，严禁明文密钥经公共 API 外泄。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _KEY_FIELDS and isinstance(v, str):
                out[k] = mask_secret(v)
            else:
                out[k] = mask_nested(v)
        return out
    if isinstance(obj, list):
        return [mask_nested(x) for x in obj]
    return obj


def encrypt_nested(obj):
    """递归加密 dict/list 树中所有密钥字段（api_key / apiKey）。

    供 model-configs 这类「{厂商id: {apiKey: ...}}」嵌套形状使用。
    已加密值（dpapi:/enc: 前缀）原样跳过，杜绝二次加密；脱敏占位符兜底置空。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _KEY_FIELDS and isinstance(v, str) and v:
                if is_masked_secret(v):
                    out[k] = ""
                elif v.startswith((_DPAPI_PREFIX, _PREFIX)):
                    out[k] = v
                else:
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
