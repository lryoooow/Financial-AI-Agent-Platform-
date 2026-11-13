"""Safe document ingestion through the bounded child-process parser."""

from pathlib import PurePosixPath
from uuid import uuid4

from ..guardrails.isolated_worker import parse_document


async def ingest_document(filename: str, data: bytes) -> dict:
    from ...knowledge import store
    from ...tools.entity import normalize_ticker, stock_ticker
    import asyncio
    import re

    title = PurePosixPath(filename.replace("\\", "/")).name[:200]
    if not title:
        raise ValueError("请选择文件。")
    text = await parse_document(PurePosixPath(title).suffix.lower(), data)
    chunks = [("文档", item) for item in store.split_text(text) if item.strip()]
    if not chunks:
        raise ValueError("没有可检索的文档文本。")
    ticker = ""
    head_sample = f"{title}\n{text[:4000]}"
    candidate = (
        re.search(r"(?<!\d)\d{6}\.(?:SH|SZ|BJ)(?!\w)", head_sample, re.IGNORECASE)
        or re.search(r"(?:证券代码|股票代码|证券简称及代码|代码)\s*[：:（(]?\s*((?:00|30|60|68|8\d|92|43)\d{4})(?!\d)", head_sample)
    )
    raw_cand = candidate.group(1) if (candidate and candidate.lastindex) else (candidate.group(0) if candidate else "")
    if raw_cand and normalize_ticker(raw_cand):
        try:
            ticker = stock_ticker(raw_cand)
        except ValueError:
            pass
    doc_id = await asyncio.to_thread(
        store.upsert_document, kind="external", ref_id=uuid4().hex,
        title=title, ticker=ticker, chunks=chunks, source_file=title,
    )
    return {"id": doc_id, "title": title, "ticker": ticker, "chunks": len(chunks)}
