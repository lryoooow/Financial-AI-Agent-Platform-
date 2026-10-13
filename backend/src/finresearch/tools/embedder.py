"""文本嵌入（本地、确定性、零依赖）。

双档设计：
- 默认档：字符 n-gram 特征哈希（hashing trick）。中文按字符 2/3-gram、拉丁词按整词，
  CRC32 定桶定符号，L2 归一化。离线可用、跨会话确定性（同文本恒同向量）、无模型体积。
  召回以词汇重叠为主，对本项目的"回忆旧对话/旧报告"场景够用。
- 供应商档（预留，配置驱动）：任意 OpenAI 兼容 /embeddings 端点，配置后语义召回更强。
  切换嵌入器需重建向量（RagStore.rebuild 未启用前，换档=清空 rag.db 重摄入即可）。
"""

from __future__ import annotations

import math
import re
import threading
import zlib
from typing import Iterable, List, Optional

EMBED_DIM = 256

# ── 供应商嵌入档（配置驱动；未配置时用本地哈希档）─────────────
# 配置来源：主库 LocalSetting "embedding-config"（api_key 加密存储）。
# 切换嵌入器（本地↔供应商/换模型）会改变向量维度——rag.db 按 embedder
# 签名检测并自动重建。
_provider_cfg: Optional[dict] = None  # {base_url, api_key, model, dim}
_provider_lock = threading.Lock()


def set_provider_embedding(cfg: Optional[dict]) -> None:
    """设置/清除供应商嵌入配置（None = 回到本地哈希档）。"""
    global _provider_cfg
    with _provider_lock:
        _provider_cfg = dict(cfg) if cfg else None


def provider_embedding_active() -> bool:
    return _provider_cfg is not None


def provider_config() -> Optional[dict]:
    """当前供应商嵌入配置的副本（未配置返回 None）。"""
    with _provider_lock:
        return dict(_provider_cfg) if _provider_cfg else None


def embedder_signature() -> str:
    """当前嵌入器的签名（签名变化 = 向量库需要重建）。"""
    with _provider_lock:
        if _provider_cfg:
            return "provider:{}:{}".format(_provider_cfg.get("model"), _provider_cfg.get("dim"))
    return "local:{}".format(EMBED_DIM)


def _provider_embed(texts: List[str]) -> List[List[float]]:
    """调用 OpenAI 兼容 /embeddings（批量）。失败抛异常——绝不静默回退本地档：
    混维度向量会让 KNN 检索全错。"""
    with _provider_lock:
        cfg = dict(_provider_cfg or {})
    if not cfg:
        raise RuntimeError("供应商嵌入未配置")
    import httpx

    url = cfg["base_url"].rstrip("/") + "/embeddings"
    headers = {"Authorization": "Bearer " + cfg["api_key"]}
    out: List[List[float]] = []
    batch = 16
    for i in range(0, len(texts), batch):
        part = [t[:6000] or " " for t in texts[i : i + batch]]
        resp = httpx.post(url, headers=headers, timeout=20.0,
                          json={"model": cfg["model"], "input": part})
        resp.raise_for_status()
        data = resp.json().get("data") or []
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        out.extend([d["embedding"] for d in ordered])
    if len(out) != len(texts):
        raise RuntimeError("嵌入返回数量不符：{}/{}".format(len(out), len(texts)))
    return out


def probe_provider_embedding(base_url: str, api_key: str, model: str) -> int:
    """连通性探测：返回该模型的向量维度（失败抛异常）。"""
    import httpx

    resp = httpx.post(
        base_url.rstrip("/") + "/embeddings",
        headers={"Authorization": "Bearer " + api_key},
        timeout=20.0,
        json={"model": model, "input": ["连通性测试"]},
    )
    resp.raise_for_status()
    data = resp.json().get("data") or []
    if not data:
        raise RuntimeError("嵌入接口未返回向量")
    return len(data[0]["embedding"])

_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _normalize(text: str) -> str:
    return "".join((text or "").upper().split())


def _ngrams(text: str) -> Iterable[str]:
    """中文 2/3-gram + 拉丁整词（小写化后与中文区分）。"""
    norm = _normalize(text)
    for word in _WORD_RE.findall(text):
        yield "w:" + word.lower()
    n = len(norm)
    for size in (2, 3):
        for i in range(n - size + 1):
            yield norm[i : i + size]


def embed_one(text: str) -> List[float]:
    """单文本 → 向量。供应商档返回该模型原生维度；本地档为确定性哈希向量。"""
    if _provider_cfg is not None:
        return _provider_embed([text])[0]
    return _embed_local(text)


def _embed_local(text: str) -> List[float]:
    """本地哈希档：L2 归一化的 EMBED_DIM 维向量（确定性）。"""
    vec = [0.0] * EMBED_DIM
    counted = False
    for gram in _ngrams(text):
        h = zlib.crc32(gram.encode("utf-8"))
        bucket = h % EMBED_DIM
        sign = 1.0 if (h >> 31) & 1 else -1.0
        vec[bucket] += sign
        counted = True
    if not counted:
        return vec
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def embed_many(texts: List[str]) -> List[List[float]]:
    """批量嵌入（供应商档一次请求多条，效率高）。"""
    if _provider_cfg is not None:
        return _provider_embed(list(texts))
    return [_embed_local(t) for t in texts]
