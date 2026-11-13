"""Lazy cached tokenizer with an offline fallback for Chinese text."""

import hashlib
import math
import os
import tempfile
from functools import lru_cache
from pathlib import Path

_ENCODING_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"


@lru_cache(maxsize=1)
def _tokenizer():
    directory = os.environ.get("TIKTOKEN_CACHE_DIR", os.environ.get("DATA_GYM_CACHE_DIR"))
    cache = Path(directory or (Path(tempfile.gettempdir()) / "data-gym-cache"))
    cache_key = hashlib.sha1(_ENCODING_URL.encode(), usedforsecurity=False).hexdigest()
    if directory == "" or not (cache / cache_key).is_file():
        return None
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except (ImportError, OSError, ValueError):
        return None


def estimate_tokens(text: str) -> int:
    encoder = _tokenizer()
    if encoder is not None:
        return len(encoder.encode_ordinary(text))
    return math.ceil(sum(0.25 if char.isascii() else 1.5 for char in text))


def clip(text: str, budget: int) -> str:
    if budget < 1:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    lo, hi = 0, min(len(text), budget * 8)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip()
