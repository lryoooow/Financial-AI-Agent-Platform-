"""Select diverse evidence while keeping multiple sections of one document."""

import numpy as np


def select_mmr(candidates: list[tuple[dict, float, np.ndarray]], limit: int, weight: float = 0.7) -> list[dict]:
    chosen: list[tuple[dict, float, np.ndarray]] = []
    doc_counts: dict[str, int] = {}
    remaining = list(candidates)
    while remaining and len(chosen) < limit:
        def score(entry):
            row, relevance, vector = entry
            if doc_counts.get(row["doc_id"], 0) >= 2:
                return float("-inf")
            overlap = max((float(vector @ other[2]) for other in chosen), default=0.0)
            return weight * relevance - (1 - weight) * max(0.0, overlap)

        selected = max(remaining, key=score)
        if score(selected) == float("-inf"):
            break
        remaining = [entry for entry in remaining if entry[0]["chunk_id"] != selected[0]["chunk_id"]]
        chosen.append(selected)
        row = selected[0]
        doc_counts[row["doc_id"]] = doc_counts.get(row["doc_id"], 0) + 1
    return [entry[0] for entry in chosen]
