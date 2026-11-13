"""Local float32 retrieval and rank fusion for mixed Chinese/ASCII evidence."""

import hashlib
import re
import unicodedata
from collections import Counter

import numpy as np

from .mmr import select_mmr
from .pgvector import rank_ids

_TERMS = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+")
_CJK_SPACES = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")
_QUERY_FILLER = re.compile(r"(?:请问|帮我|查看|查询|查下|看看|一下|之前|以前|上次|历史|关于|怎么|如何|什么|哪些|我们|聊过|生成的|里的|中的)")
DIMENSIONS = 768
MIN_SIMILARITY = 0.35


def _features(text: str) -> tuple[list[str], list[str]]:
    main = []
    singles = []
    normalized = _CJK_SPACES.sub("", unicodedata.normalize("NFKC", text).lower())
    for term in _TERMS.findall(normalized):
        if "\u3400" <= term[0] <= "\u9fff":
            singles.extend(term)
            main.extend(term[i:i + 2] for i in range(len(term) - 1))
        else:
            main.append(term)
    return main or singles, singles


def embed(text: str) -> np.ndarray:
    terms, singles = _features(text)
    vector = np.zeros(DIMENSIONS, dtype="<f4")
    for token, count in Counter([*terms, *terms, *singles]).items():
        index = int.from_bytes(hashlib.blake2s(token.encode(), digest_size=4).digest(), "little")
        vector[index % DIMENSIONS] += np.sqrt(count)
    norm = float(np.linalg.norm(vector))
    if norm:
        vector /= norm
    return vector


def chunk_text(row: dict) -> str:
    title = " ".join(str(row.get(key) or "") for key in ("title", "ticker", "name", "heading"))
    return f"{title} {title} {row.get('text') or ''}"


def embedding_bytes(row: dict) -> bytes:
    return embed(chunk_text(row)).tobytes()


def rank(
    query: str, rows: list[dict], lexical_ids: list[int], limit: int, *, min_similarity: float = MIN_SIMILARITY,
) -> list[dict]:
    if not rows:
        return []
    query_vector = embed(query)
    raw_terms, _ = _features(query)
    core_terms, _ = _features(_QUERY_FILLER.sub(" ", query))
    terms = set(core_terms or raw_terms)
    if not terms:
        return []
    vectors = []
    for row in rows:
        blob = row.get("embedding_blob")
        vectors.append(np.frombuffer(blob, dtype="<f4") if blob and len(blob) == DIMENSIONS * 4 else embed(chunk_text(row)))
    matrix = np.vstack(vectors)
    similarities = matrix @ query_vector
    depth = max(60, limit * 6)
    remote = rank_ids([row["chunk_id"] for row in rows], matrix, query_vector, depth)
    vector_top = remote if remote is not None else [
        rows[int(index)]["chunk_id"] for index in np.argsort(-similarities, kind="stable")[:depth]
    ]
    vector_rank = {chunk_id: pos for pos, chunk_id in enumerate(vector_top, 1)}
    lexical_rank = {chunk_id: pos for pos, chunk_id in enumerate(lexical_ids, 1)}

    candidates = []
    for index, row in enumerate(rows):
        chunk_id = row["chunk_id"]
        vector_position, lexical_position = vector_rank.get(chunk_id), lexical_rank.get(chunk_id)
        if vector_position is None and lexical_position is None:
            continue
        document_terms, _ = _features(chunk_text(row))
        overlap_count = len(terms.intersection(document_terms))
        coverage = overlap_count / len(terms)
        cosine = float(similarities[index])
        similarity = max(cosine, coverage * 0.8 + cosine * 0.2)
        if lexical_position is not None and overlap_count >= 1:
            gate = min(min_similarity, 0.14 if (overlap_count >= 2 or len(terms) <= 2) else 0.22)
        else:
            gate = min_similarity
        if similarity < gate:
            continue
        rrf = sum(1 / (60 + position) for position in (vector_position, lexical_position) if position)
        reranked = similarity * 0.55 + coverage * 0.25 + min(1.0, rrf * 30) * 0.2
        candidates.append(({**row, "score": round(reranked, 5)}, reranked, vectors[index]))
    return select_mmr(candidates, limit)
