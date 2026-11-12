"""本地知识库（v1.8.0）：研究报告 / 对话压缩摘要入库，供 agent RAG 检索。

存储：%APPDATA%/FinResearch/knowledge.db（标准库 sqlite3，独立于业务库，桌面离线可用）。
- kb_documents：一份报告或一段对话摘要一行（kind + ref_id 唯一，重复入库即覆盖）；
- kb_chunks：按章节分块的正文；
- kb_fts：FTS5 全文索引（unicode61 分词器 + 预切分 token 列）。

中文检索：unicode61 不切中文词，这里自己把 CJK 连续串切成**单字 + 双字**、英数串按词，
写入 token 列再交给 FTS5 做 bm25 排序 —— 不引入分词库与向量模型（打包体积/离线约束），
对「茅台 估值」「现金流」「600519」这类金融检索词召回足够稳定。
查询端优先用双字（精确），只有单字查询才退回单字匹配。

对外不暴露任何路径：返回值只含 doc/report id、标题、片段文本。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_DB_PATH: Optional[Path] = None
_SCHEMA_READY: set[str] = set()

_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+|[A-Za-z0-9]+")
_CHUNK_MAX = 600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_documents (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    ticker TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    template TEXT NOT NULL DEFAULT '',
    as_of TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_kb_documents_ref ON kb_documents(kind, ref_id);
CREATE INDEX IF NOT EXISTS ix_kb_documents_ticker ON kb_documents(ticker);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kb_chunks_doc ON kb_chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(tokens, tokenize='unicode61');
"""


# ── 连接 ─────────────────────────────────────────────────
def db_path() -> Path:
    """知识库文件位置（测试可经 FINRESEARCH_KNOWLEDGE_DB 覆盖）。"""
    global _DB_PATH
    override = os.environ.get("FINRESEARCH_KNOWLEDGE_DB")
    if override:
        return Path(override)
    if _DB_PATH is None:
        from ..config import app_data_dir

        _DB_PATH = Path(app_data_dir()) / "knowledge.db"
    return _DB_PATH


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """短连接 + 进程内锁（家用单进程；写入量很小，串行化最简单可靠）。"""
    path = db_path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            key = str(path)
            if key not in _SCHEMA_READY:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(_SCHEMA)
                from .profile import ensure_schema as _profile_schema

                _profile_schema(conn)
                _SCHEMA_READY.add(key)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init() -> bool:
    """启动时建库建表（幂等）；失败只记日志，不影响主流程。"""
    try:
        with connect():
            pass
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("知识库初始化失败：%s", e)
        return False


# ── 切分 ─────────────────────────────────────────────────
def _is_cjk(s: str) -> bool:
    return bool(s) and "㐀" <= s[0] <= "﫿"


_CJK_GAP = re.compile(r"(?<=[㐀-鿿])\s+(?=[㐀-鿿])")
_SINGLE_CJK_RUN = re.compile(r"(?<![㐀-鿿])([㐀-鿿](?:[ \t]+[㐀-鿿])+)(?![㐀-鿿])")


def _squeeze(text: str) -> str:
    """索引端去掉中文字之间的空白：数据源常把简称写成「五 粮 液」，不去掉就切不出「五粮」「粮液」。"""
    norm = unicodedata.normalize("NFKC", text or "")
    return _CJK_GAP.sub("", norm)


def _squeeze_query(query: str) -> str:
    """查询端只合并散字简称（如「五 粮 液」→「五粮液」），保留多字词之间的空格边界（如「茅台 估值」不拼出伪词「台估」）。"""
    norm = unicodedata.normalize("NFKC", query or "")
    return _SINGLE_CJK_RUN.sub(lambda m: re.sub(r"\s+", "", m.group(0)), norm)


def tokenize(text: str) -> str:
    """索引端：CJK 单字 + 双字，英数按词（小写）。"""
    out: list[str] = []
    for m in _CJK_RUN.finditer(_squeeze(text)):
        w = m.group()
        if _is_cjk(w):
            out.extend(w)
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
        else:
            out.append(w.lower())
    return " ".join(out)


def build_match(query: str) -> str:
    """查询端：CJK 优先双字（单字词才用单字），英数按词；各 token OR 连接交给 bm25 排序。"""
    toks: list[str] = []
    for m in _CJK_RUN.finditer(_squeeze_query(query)):
        w = m.group()
        if _is_cjk(w):
            toks.extend([w] if len(w) == 1 else [w[i:i + 2] for i in range(len(w) - 1)])
        else:
            toks.append(w.lower())
    seen: list[str] = []
    for t in toks:
        if t not in seen:
            seen.append(t)
    return " OR ".join(f'"{t}"' for t in seen[:32])


def split_text(text: str, limit: int = _CHUNK_MAX) -> list[str]:
    """按段落/句读切成 ≤limit 的块（不切断句子，超长句硬切兜底）。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    pieces = [p for p in re.split(r"(?<=[。！？；\n])", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        while len(p) > limit:
            if cur:
                chunks.append(cur.strip())
                cur = ""
            chunks.append(p[:limit].strip())
            p = p[limit:]
        if len(cur) + len(p) > limit:
            chunks.append(cur.strip())
            cur = ""
        cur += p
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if c]


TEMPLATE_LABELS = {
    "equity_deep": "个股深度",
    "industry": "行业研究",
    "earnings": "财报点评",
    "risk": "风险评估",
}


def report_chunks(doc: dict) -> list[tuple[str, str]]:
    """把 ReportDoc 拆成 (小标题, 正文) 块：摘要 / 各章节 / 财务表 / 催化 / 风险。"""
    out: list[tuple[str, str]] = []
    for t in split_text(doc.get("summary") or ""):
        out.append(("投资摘要", t))
    for sec in doc.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        for t in split_text(sec.get("body") or ""):
            out.append((str(sec.get("title") or "章节"), t))
    fins = doc.get("financials") or {}
    years = fins.get("years") or []
    rows = fins.get("rows") or []
    if years and rows:
        lines = []
        for r in rows:
            vals = r.get("values") or []
            pairs = "；".join(f"{y} {v}" for y, v in zip(years, vals))
            lines.append(f"{r.get('name', '')}：{pairs}")
        for t in split_text("\n".join(lines)):
            out.append(("财务数据", t))
    if (doc.get("catalyst") or "").strip():
        for t in split_text(doc["catalyst"].strip()):
            out.append(("催化与观察", t))
    risks = [str(r) for r in (doc.get("risks") or []) if str(r).strip()]
    if risks:
        for t in split_text("；".join(risks)):
            out.append(("风险提示", t))
    return out


# ── 写入 ─────────────────────────────────────────────────
def _delete_doc(conn: sqlite3.Connection, doc_id: str) -> None:
    ids = [r["id"] for r in conn.execute("SELECT id FROM kb_chunks WHERE doc_id=?", (doc_id,))]
    if ids:
        conn.executemany("DELETE FROM kb_fts WHERE rowid=?", [(i,) for i in ids])
    conn.execute("DELETE FROM kb_chunks WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM kb_documents WHERE id=?", (doc_id,))


def upsert_document(
    *,
    kind: str,
    ref_id: str,
    title: str,
    chunks: list[tuple[str, str]],
    ticker: str = "",
    name: str = "",
    template: str = "",
    as_of: str = "",
    meta: Optional[dict] = None,
) -> str:
    """写入/覆盖一份文档（同 kind+ref_id 先删后写，保证重复入库幂等）。返回 doc_id。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM kb_documents WHERE kind=? AND ref_id=?", (kind, ref_id)
        ).fetchone()
        if row:
            _delete_doc(conn, row["id"])
        doc_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO kb_documents(id, kind, ref_id, title, ticker, name, template, as_of, created_at, meta)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (doc_id, kind, ref_id, title, ticker, name, template, as_of, time.time(),
             json.dumps(meta or {}, ensure_ascii=False)),
        )
        context = f"{title} {name} {ticker} {TEMPLATE_LABELS.get(template, '')}"
        for seq, (heading, text) in enumerate(chunks):
            cur = conn.execute(
                "INSERT INTO kb_chunks(doc_id, seq, heading, text) VALUES(?,?,?,?)",
                (doc_id, seq, heading, text),
            )
            conn.execute(
                "INSERT INTO kb_fts(rowid, tokens) VALUES(?, ?)",
                (cur.lastrowid, tokenize(f"{context} {heading} {text}")),
            )
        return doc_id


def index_report(report_id: str, doc: dict) -> Optional[str]:
    """研究报告入库（报告落库后调用；失败只记日志）。"""
    if not report_id or not isinstance(doc, dict):
        return None
    try:
        name = str(doc.get("name") or "")
        ticker = str(doc.get("ticker") or "")
        template = str(doc.get("template") or "")
        title = f"{name or ticker} · {TEMPLATE_LABELS.get(template, '研究报告')}"
        return upsert_document(
            kind="report", ref_id=report_id, title=title, chunks=report_chunks(doc),
            ticker=ticker, name=name, template=template, as_of=str(doc.get("as_of") or ""),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("报告入知识库失败 report=%s：%s", report_id, e)
        return None


def index_conversation_summary(conversation_id: str, title: str, summary: str, generation: int) -> Optional[str]:
    """对话压缩摘要入库（跨会话记忆）：同一会话只保留最新一代摘要。"""
    if not conversation_id or not (summary or "").strip():
        return None
    try:
        chunks = [("对话摘要", t) for t in split_text(summary)]
        return upsert_document(
            kind="conversation", ref_id=conversation_id, title=title or "历史对话",
            chunks=chunks, as_of=time.strftime("%Y-%m-%d %H:%M"), meta={"generation": generation},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("对话摘要入知识库失败 conv=%s：%s", conversation_id, e)
        return None


def delete_by_ref(kind: str, ref_id: str) -> bool:
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT id FROM kb_documents WHERE kind=? AND ref_id=?", (kind, ref_id)
            ).fetchone()
            if not row:
                return False
            _delete_doc(conn, row["id"])
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("知识库删除失败 %s/%s：%s", kind, ref_id, e)
        return False


def indexed_refs(kind: str) -> set[str]:
    with connect() as conn:
        return {r["ref_id"] for r in conn.execute("SELECT ref_id FROM kb_documents WHERE kind=?", (kind,))}


def prune_missing_refs(kind: str, valid_refs: set[str]) -> int:
    """清理业务库已不存在而知识库残留的孤儿文档。"""
    with connect() as conn:
        rows = conn.execute("SELECT id, ref_id FROM kb_documents WHERE kind=?", (kind,)).fetchall()
        stale_ids = [r["id"] for r in rows if r["ref_id"] not in valid_refs]
        for doc_id in stale_ids:
            _delete_doc(conn, doc_id)
        return len(stale_ids)


def clear(kind: Optional[str] = None) -> int:
    with connect() as conn:
        if kind:
            ids = [r["id"] for r in conn.execute("SELECT id FROM kb_documents WHERE kind=?", (kind,))]
        else:
            ids = [r["id"] for r in conn.execute("SELECT id FROM kb_documents")]
        for i in ids:
            _delete_doc(conn, i)
        return len(ids)


def _escape_like(val: str) -> str:
    return val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ── 检索 ─────────────────────────────────────────────────
def search(
    query: str,
    *,
    limit: int = 6,
    ticker: str = "",
    kind: str = "",
) -> list[dict[str, Any]]:
    """bm25 检索，返回命中块（同一文档最多 2 块，窗口函数先按文档限流，避免超长单篇报告挤掉其余文档）。"""
    match = build_match(query)
    if not match:
        return []
    limit = max(1, min(20, int(limit)))
    where_sql = " WHERE kb_fts MATCH ?"
    args: list[Any] = [match]
    if ticker:
        raw_ticker = ticker.strip().upper()
        code = _escape_like(raw_ticker.split(".")[0])
        where_sql += " AND (d.ticker LIKE ? ESCAPE '\\' OR d.ticker = ?)"
        args += [code + "%", raw_ticker]
    if kind:
        where_sql += " AND d.kind = ?"
        args.append(kind)
    sql = (
        "WITH matched AS MATERIALIZED ("
        " SELECT c.id AS chunk_id, c.heading, c.text, d.id AS doc_id, d.kind, d.ref_id, d.title,"
        " d.ticker, d.name, d.template, d.as_of, bm25(kb_fts) AS score"
        " FROM kb_fts JOIN kb_chunks c ON c.id = kb_fts.rowid JOIN kb_documents d ON d.id = c.doc_id"
        + where_sql
        + ") SELECT * FROM ("
        " SELECT matched.*, ROW_NUMBER() OVER (PARTITION BY doc_id ORDER BY score) AS doc_rn"
        " FROM matched"
        ") ranked WHERE doc_rn <= 2 ORDER BY score LIMIT ?"
    )
    args.append(limit)
    try:
        with connect() as conn:
            rows = conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning("知识库检索失败：%s", e)
        return []
    per_doc: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for r in rows:
        if per_doc.get(r["doc_id"], 0) >= 2:
            continue
        per_doc[r["doc_id"]] = per_doc.get(r["doc_id"], 0) + 1
        out.append({
            "doc_id": r["doc_id"],
            "kind": r["kind"],
            "report_id": r["ref_id"] if r["kind"] == "report" else "",
            "conversation_id": r["ref_id"] if r["kind"] == "conversation" else "",
            "title": r["title"],
            "ticker": r["ticker"],
            "name": r["name"],
            "template": r["template"],
            "as_of": r["as_of"],
            "heading": r["heading"],
            "text": r["text"],
            "score": round(-float(r["score"]), 3),
        })
        if len(out) >= limit:
            break
    return out


def stats() -> dict[str, Any]:
    try:
        with connect() as conn:
            docs = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, COUNT(*) AS n FROM kb_documents GROUP BY kind")}
            chunks = conn.execute("SELECT COUNT(*) AS n FROM kb_chunks").fetchone()["n"]
            last = conn.execute("SELECT MAX(created_at) AS t FROM kb_documents").fetchone()["t"]
        return {
            "reports": int(docs.get("report", 0)),
            "conversations": int(docs.get("conversation", 0)),
            "chunks": int(chunks),
            "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else "",
        }
    except sqlite3.Error as e:
        logger.warning("知识库统计查询失败：%s", e)
        return {"reports": 0, "conversations": 0, "chunks": 0, "updated_at": ""}
