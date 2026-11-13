"""Optional semantic reranking via a user-configured embeddings endpoint."""

import asyncio
import logging

import httpx
import numpy as np

from ...config import settings
from ..guardrails.net_guard import build_safe_async_client, validate_outbound_url
from ...tools.secrets import model_endpoint_identity

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(model_endpoint_identity(settings.embedding_base_url or "") and settings.embedding_model)


async def rerank(query: str, hits: list[dict]) -> list[dict]:
    base = model_endpoint_identity(settings.embedding_base_url or "")
    if not base or not settings.embedding_model or not hits:
        return hits
    headers = {"Authorization": f"Bearer {settings.embedding_api_key}"} if settings.embedding_api_key else {}
    inputs = [query, *(str(hit["text"])[:600] for hit in hits)]
    allow_private = not settings.fin_web or settings.fin_allow_private_endpoints
    try:
        await asyncio.to_thread(
            validate_outbound_url,
            base,
            allow_private_local=allow_private,
            self_port=settings.port,
            resolve_dns=True,
        )
        async with build_safe_async_client(
            allow_private_local=allow_private,
            self_port=settings.port,
            timeout=4.0,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                f"{base}/embeddings", headers=headers,
                json={"model": settings.embedding_model, "input": inputs},
            )
            response.raise_for_status()
            rows = sorted(response.json()["data"], key=lambda row: int(row["index"]))
        matrix = np.asarray([row["embedding"] for row in rows], dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(inputs) or not np.isfinite(matrix).all():
            return hits
        norms = np.linalg.norm(matrix, axis=1)
        if not np.all(norms > 0):
            return hits
        matrix /= norms[:, None]
        scores = matrix[1:] @ matrix[0]
        order = np.argsort(-scores, kind="stable")
        filtered = [
            {**hits[int(index)], "semantic_score": round(float(scores[index]), 5)}
            for index in order if scores[index] >= 0.20
        ]
        if filtered:
            return filtered
        return [
            {**hits[int(index)], "semantic_score": round(float(scores[index]), 5)}
            for index in order
        ]
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        logger.debug("Embedding endpoint unavailable; retaining local search results")
        return hits
