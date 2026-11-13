"""Optional PostgreSQL vector ranking for the current SQLite retrieval scope."""

import logging

import numpy as np

from ...config import settings

logger = logging.getLogger(__name__)


def _vector(values: np.ndarray) -> str:
    return "[" + ",".join(format(float(value), ".8g") for value in values) + "]"


def rank_ids(chunk_ids: list[int], matrix: np.ndarray, query: np.ndarray, limit: int) -> list[int] | None:
    url = (settings.fin_pgvector_url or "").strip().replace("postgresql+psycopg://", "postgresql://", 1)
    if not url:
        return None
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=3, options="-c statement_timeout=4000") as conn, conn.cursor() as cursor:
            cursor.execute("CREATE TEMP TABLE jingyuan_candidates (id bigint, embedding vector) ON COMMIT DROP")
            with cursor.copy("COPY jingyuan_candidates (id, embedding) FROM STDIN") as stream:
                for chunk_id, vector in zip(chunk_ids, matrix, strict=True):
                    stream.write_row((chunk_id, _vector(vector)))
            cursor.execute(
                "SELECT id FROM jingyuan_candidates ORDER BY embedding <=> %s::vector NULLS LAST, id LIMIT %s",
                (_vector(query), limit),
            )
            return [row[0] for row in cursor.fetchall()]
    except Exception:
        # SQLite remains authoritative when the optional vector server is offline.
        logger.debug("pgvector ranking unavailable; retaining local vectors")
        return None
