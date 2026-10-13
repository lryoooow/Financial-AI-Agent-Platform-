"""用户知识库：文档上传 → 解析 → 分块 → 嵌入（rag.db）。

支持的格式：.txt / .md / .pdf / .docx（pypdf + python-docx，均为轻量纯 Python）。
原文双保险：rag.db 的 kb_documents.full_text（检索/深读用）+ kb/ 目录的原始文件副本
（用户资产永不丢）。同名/同内容重传按文件名+内容哈希幂等替换。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import app_data_dir
from . import memory

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024       # 单文件 20MB
MAX_TEXT_CHARS = 2_000_000              # 提取文本上限（约几百万字）
MAX_CHUNKS_PER_DOC = 600                # 单文档块上限（防误传超大文档撑爆向量库）
_CHUNK_SIZE = 800                       # 分块目标长度
_CHUNK_OVERLAP = 100                    # 相邻块重叠（跨块语义不断裂）

SUPPORTED_EXT = {".txt", ".md", ".pdf", ".docx"}


def kb_files_dir() -> Path:
    d = Path(app_data_dir()) / "kb"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _extract_pdf(data: bytes) -> str:
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(pages)


def _extract_docx(data: bytes) -> str:
    import io

    import docx

    doc = docx.Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_text(ext: str, data: bytes) -> str:
    ext = ext.lower()
    if ext in (".txt", ".md"):
        for enc in ("utf-8", "gbk", "utf-16"):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode("utf-8", errors="replace")
    if ext == ".pdf":
        return _extract_pdf(data)
    if ext == ".docx":
        return _extract_docx(data)
    raise ValueError(f"不支持的文档格式：{ext}（支持 txt/md/pdf/docx）")


def chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> List[str]:
    """按段落边界分块：优先在空行/换行处切，避免把句子切半。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text) and len(chunks) < MAX_CHUNKS_PER_DOC:
        end = min(start + size, len(text))
        if end < len(text):
            # 在窗口尾部找最近的换行，块边界落在行尾
            cut = text.rfind("\n", start + size // 2, end)
            if cut > start:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def ingest_document_sync(filename: str, data: bytes) -> dict:
    """解析+入库一份用户文档。返回文档元数据；重复内容幂等替换。"""
    if not memory.memory_available():
        raise RuntimeError("记忆/知识库未启用（向量库不可用）")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("文档不能超过 20MB")
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise ValueError(f"不支持的格式 {ext or '(无扩展名)'}，支持 txt/md/pdf/docx")
    text = extract_text(ext, data).strip()
    if not text:
        raise ValueError("未能从文档中提取到文本（可能是扫描版 PDF）")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]

    content_digest = hashlib.sha256(data).hexdigest()[:12]
    doc_id = "kb_" + uuid.uuid5(uuid.NAMESPACE_URL, f"{filename}|{content_digest}").hex[:20]
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("文档分块后为空")

    store = memory._store
    # 幂等：同 id 重传 = 替换（先清旧块）
    store.delete_by_ref(memory.SOURCE_KB, doc_id)
    added = 0
    for i, piece in enumerate(chunks):
        if store.add(
            source_type=memory.SOURCE_KB, ref_id=doc_id,
            title=Path(filename).stem, text=piece,
            meta={"chunk": i, "total": len(chunks)},
        ) is not None:
            added += 1
    # 原始文件副本（用户资产永不丢）
    target = kb_files_dir() / f"{doc_id}{ext}"
    target.write_bytes(data)
    store.kb_add_doc(
        doc_id=doc_id, name=filename, ext=ext, mime="",
        size_bytes=len(data), chunk_count=added, full_text=text,
    )
    logger.info("知识库入库：%s（%d 块，%d 字）", filename, added, len(text))
    meta = store.kb_get_doc(doc_id) or {}
    meta.pop("full_text", None)
    return meta


def list_documents_sync() -> List[dict]:
    if not memory.memory_available():
        return []
    return memory._store.kb_list_docs()


def delete_document_sync(doc_id: str) -> bool:
    if not memory.memory_available():
        return False
    doc = memory._store.kb_get_doc(doc_id)
    ok = memory._store.kb_delete_doc(doc_id)
    if ok and doc:
        # 元数据行已在 kb_delete_doc 删除；清理原始文件副本
        for ext in SUPPORTED_EXT:
            target = kb_files_dir() / f"{doc_id}{ext}"
            if target.is_file():
                try:
                    target.unlink()
                except OSError:
                    pass
                break
    return ok


def search_documents_sync(query: str, limit: int = 5) -> dict:
    """知识库范围内检索（UI 检索测试框用；agent 走 search_memory 全域）。"""
    if not memory.memory_available():
        return {"items": [], "provenance": {"error": "记忆库未启用"}}
    limit = max(1, min(int(limit or 5), 10))
    hits = memory._store.search(query, k=limit, source_types=[memory.SOURCE_KB])
    items = [{
        "doc_id": h.get("ref_id") or "",
        "doc_name": h.get("title") or "",
        "text": (h.get("text") or "")[:300],
        "ref": f"{memory.REF_KB}{h.get('ref_id') or ''}",
    } for h in hits]
    return {"items": items, "provenance": {"source": "sqlite-vec", "note": "知识库文档检索"}}
