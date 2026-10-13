"""本地向量库与知识库（sqlite-vec，独立 rag.db 文件）。

选型：sqlite-vec（github.com/asg017/sqlite-vec）——SQLite 官方生态的向量扩展，
单个可加载文件、无服务进程、与现有 SQLite 栈零冲突，是桌面场景里"轻便高可用"的最优解。

结构（rag.db）：
- chunks            结构化知识条目：来源类型/引用/标题/正文/时间/元数据/内容哈希（去重）
- chunks_vec        vec0 虚拟表：chunk_id ↔ 256 维嵌入（embedder.embed_one）
检索：KNN 向量近邻为主，词法 LIKE 兜底（嵌入召回为空时），结果带 distance 供排序。

线程模型：sqlite3 同步连接 + 内部锁；异步调用方一律 asyncio.to_thread 包裹。
扩展加载失败（打包缺 vec0 等）时 open() 返回 None，记忆功能优雅降级、绝不阻断主链路。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, List, Optional

from .embedder import EMBED_DIM, embed_one

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    ref_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_type, ref_id);
CREATE INDEX IF NOT EXISTS idx_chunks_time ON chunks(created_at);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding float[{dim}]
);
CREATE TABLE IF NOT EXISTS rag_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_documents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ext TEXT NOT NULL DEFAULT '',
    mime TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    full_text TEXT NOT NULL DEFAULT '',
    added_at REAL NOT NULL
);
"""


def _content_hash(source_type: str, ref_id: str, text: str) -> str:
    return hashlib.sha256(f"{source_type}|{ref_id}|{text}".encode("utf-8")).hexdigest()[:32]


class RagStore:
    """同步向量库。所有方法线程安全（内部锁），失败抛异常由调用方降级。"""

    def __init__(self, db_path: str | Path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(self._db)
        # vec0 虚拟表接受 float32 序列化字节，不接受 Python list
        self._serialize = sqlite_vec.serialize_float32
        self._db.execute("PRAGMA journal_mode=WAL")
        # 注意不能用 str.format：SQL 里的 DEFAULT '{}' 会被当占位符
        dim = self.stored_dim() or EMBED_DIM
        self._db.executescript(_SCHEMA.replace("{dim}", str(dim)))
        if not self.get_meta("dim", ""):
            self.set_meta("dim", str(dim))
        self._db.commit()

    # ── 写入 ────────────────────────────────────────────────
    def add(
        self,
        *,
        source_type: str,
        text: str,
        ref_id: str = "",
        title: str = "",
        meta: Optional[dict] = None,
    ) -> Optional[int]:
        """写入一条知识块（内容哈希去重）。返回 id，重复返回 None。"""
        text = (text or "").strip()
        if not text:
            return None
        digest = _content_hash(source_type, ref_id, text)
        insert_sql = (
            "INSERT INTO chunks(content_hash, source_type, ref_id, title, text, meta, created_at) "
            "VALUES(?,?,?,?,?,?,?)"
        )
        params = (digest, source_type, ref_id, title, text,
                  json.dumps(meta or {}, ensure_ascii=False), time.time())
        with self._lock:
            try:
                cur = self._db.execute(insert_sql, params)
                chunk_id = int(cur.lastrowid)
                self._db.execute(
                    "INSERT INTO chunks_vec(chunk_id, embedding) VALUES(?, ?)",
                    (chunk_id, self._serialize(embed_one(text))),
                )
                self._db.commit()
                return chunk_id
            except sqlite3.IntegrityError:
                self._db.rollback()
                return None  # 重复内容：天然幂等
            except sqlite3.OperationalError as e:
                # 嵌入器切换窗口（表 256 维、新嵌入器 1024 维）：先入元数据——
                # 词法检索仍可命中，向量待 rebuild_embeddings 补齐
                self._db.rollback()
                if "Dimension mismatch" not in str(e):
                    raise
                try:
                    cur = self._db.execute(insert_sql, params)
                    self._db.commit()
                    return int(cur.lastrowid)
                except sqlite3.IntegrityError:
                    self._db.rollback()
                    return None

    def delete_by_ref(self, source_type: str, ref_id: str) -> int:
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM chunks WHERE source_type=? AND ref_id=?", (source_type, ref_id)
            ).fetchall()
            for (cid,) in rows:
                self._db.execute("DELETE FROM chunks_vec WHERE chunk_id=?", (cid,))
                self._db.execute("DELETE FROM chunks WHERE id=?", (cid,))
            self._db.commit()
            return len(rows)

    def purge_older_than(self, days: float, source_types: Optional[List[str]] = None) -> int:
        """清理超龄条目（对话片段默认 180 天；报告等可排除在外）。"""
        cutoff = time.time() - days * 86400
        with self._lock:
            if source_types:
                marks = ",".join("?" for _ in source_types)
                rows = self._db.execute(
                    f"SELECT id FROM chunks WHERE created_at < ? AND source_type IN ({marks})",
                    (cutoff, *source_types),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT id FROM chunks WHERE created_at < ?", (cutoff,)
                ).fetchall()
            for (cid,) in rows:
                self._db.execute("DELETE FROM chunks_vec WHERE chunk_id=?", (cid,))
                self._db.execute("DELETE FROM chunks WHERE id=?", (cid,))
            self._db.commit()
            return len(rows)

    # ── 检索 ────────────────────────────────────────────────
    def search(
        self,
        query: str,
        k: int = 5,
        source_types: Optional[List[str]] = None,
    ) -> List[dict[str, Any]]:
        """混合检索：向量 KNN 为主；为空时词法兜底。

        修复三个 bug：
        1. 词法兜底 SQL 前导逗号语法错误（IN (,?)）
        2. OR/AND 优先级导致过滤泄漏（text LIKE ? OR title LIKE ? AND ... → 需括号）
        3. KNN 后置过滤饿死（chat 块远多于 kb 时 top-N 全被 chat 占满）
        """
        query = (query or "").strip()
        if not query:
            return []
        k = max(1, int(k))
        hits: List[dict[str, Any]] = []

        # 嵌入计算在锁外（供应商档是网络调用）
        try:
            vec = self._serialize(embed_one(query))
        except Exception:  # noqa: BLE001
            vec = None

        # KNN 路径：按源比例放大候选数，防饿死
        if vec is not None:
            try:
                with self._lock:
                    # 没有过滤时直接取 k 个；有过滤时按库内源占比放大
                    if source_types:
                        total = self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                        matching = self._db.execute(
                            "SELECT COUNT(*) FROM chunks WHERE source_type IN ({})".format(
                                ",".join("?" for _ in source_types)
                            ),
                            source_types,
                        ).fetchone()[0]
                        # 目标源占比 < 50% 时放大 5 倍候选（至少 3×k）
                        knn_k = max(k * 3, int(k * (total / max(1, matching)))) if matching > 0 else k * 5
                    else:
                        knn_k = k
                    marks = ""
                    params: list[Any] = [vec, knn_k]
                    if source_types:
                        marks = "AND c.source_type IN (" + ",".join("?" for _ in source_types) + ")"
                        params += list(source_types)
                    rows = self._db.execute(
                        "SELECT c.id, c.source_type, c.ref_id, c.title, c.text, c.meta, c.created_at, v.distance "
                        f"FROM chunks_vec v JOIN chunks c ON c.id = v.chunk_id "
                        f"WHERE v.embedding MATCH ? AND k = ? {marks} ORDER BY v.distance",
                        params,
                    ).fetchall()
                    hits = [
                        {
                            "id": r[0], "source_type": r[1], "ref_id": r[2], "title": r[3],
                            "text": r[4], "meta": json.loads(r[5] or "{}"),
                            "created_at": r[6], "distance": r[7],
                        }
                        for r in rows
                    ]
            except Exception:  # noqa: BLE001  # SQLite/网络异常都降词法
                hits = []

        # 词法兜底：修复前导逗号 + OR/AND 优先级
        if not hits:
            like = f"%{query[:32]}%"
            try:
                with self._lock:
                    if source_types:
                        marks = ",".join("?" for _ in source_types)
                        sql = (
                            "SELECT id, source_type, ref_id, title, text, meta, created_at FROM chunks "
                            f"WHERE (text LIKE ? OR title LIKE ?) AND source_type IN ({marks}) "
                            "ORDER BY created_at DESC LIMIT ?"
                        )
                        rows = self._db.execute(sql, (like, like, *source_types, k)).fetchall()
                    else:
                        sql = (
                            "SELECT id, source_type, ref_id, title, text, meta, created_at FROM chunks "
                            "WHERE text LIKE ? OR title LIKE ? "
                            "ORDER BY created_at DESC LIMIT ?"
                        )
                        rows = self._db.execute(sql, (like, like, k)).fetchall()
                    hits = [
                        {
                            "id": r[0], "source_type": r[1], "ref_id": r[2], "title": r[3],
                            "text": r[4], "meta": json.loads(r[5] or "{}"),
                            "created_at": r[6], "distance": None,
                        }
                        for r in rows
                    ]
            except Exception:  # noqa: BLE001
                hits = []

        return hits[:k]

    def kb_add_doc(self, *, doc_id: str, name: str, ext: str, mime: str,
                   size_bytes: int, chunk_count: int, full_text: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO kb_documents"
                "(id, name, ext, mime, size_bytes, chunk_count, full_text, added_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (doc_id, name, ext, mime, int(size_bytes), int(chunk_count),
                 full_text, time.time()),
            )
            self._db.commit()

    def kb_get_doc(self, doc_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT id, name, ext, mime, size_bytes, chunk_count, full_text, added_at "
                "FROM kb_documents WHERE id=?", (doc_id,)
            ).fetchone()
        if not row:
            return None
        return {"id": row[0], "name": row[1], "ext": row[2], "mime": row[3],
                "size_bytes": row[4], "chunk_count": row[5],
                "full_text": row[6], "added_at": row[7]}

    def kb_list_docs(self) -> List[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, name, ext, mime, size_bytes, chunk_count, added_at "
                "FROM kb_documents ORDER BY added_at DESC"
            ).fetchall()
        return [{"id": r[0], "name": r[1], "ext": r[2], "mime": r[3],
                 "size_bytes": r[4], "chunk_count": r[5], "added_at": r[6]} for r in rows]

    def kb_delete_doc(self, doc_id: str) -> bool:
        removed = self.delete_by_ref("kb", doc_id)
        with self._lock:
            cur = self._db.execute("DELETE FROM kb_documents WHERE id=?", (doc_id,))
            self._db.commit()
        return removed > 0 or cur.rowcount > 0

    # ── 嵌入器签名与重建 ──────────────────────────────────
    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM rag_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO rag_meta(key, value) VALUES(?,?)", (key, value)
            )
            self._db.commit()

    def stored_dim(self) -> Optional[int]:
        """vec0 表当前维度（记录在 rag_meta.dim；未初始化返回 None）。

        pragma_table_info 对 vec0 虚拟表不暴露维度，自己记账。
        """
        try:
            value = self.get_meta("dim", "")
            return int(value) if value else None
        except Exception:  # noqa: BLE001
            return None

    def rebuild_embeddings(self, signature: str) -> int:
        """用当前嵌入器重建全部向量（切换本地↔供应商/换模型时调用）。

        删掉旧 vec0 表 → 按新维度重建 → 逐块重嵌（chunks.text 全量在库，无信息丢失）。
        返回重建的向量数。失败抛异常（表已重建，调用方需保证签名一致后重试）。
        """
        from .embedder import embed_many

        with self._lock:
            rows = self._db.execute("SELECT id, text FROM chunks ORDER BY id").fetchall()
        if not rows:
            dim = self._current_dim()
            with self._lock:
                self._db.execute("DROP TABLE IF EXISTS chunks_vec")
                self._db.execute(
                    "CREATE VIRTUAL TABLE chunks_vec USING vec0("
                    "chunk_id INTEGER PRIMARY KEY, embedding float[{}])".format(dim)
                )
                self._db.execute(
                    "INSERT OR REPLACE INTO rag_meta(key, value) VALUES('embedder', ?)",
                    (signature,),
                )
                self._db.execute(
                    "INSERT OR REPLACE INTO rag_meta(key, value) VALUES('dim', ?)",
                    (str(dim),),
                )
                self._db.commit()
            return 0
        vectors = embed_many([r[1] for r in rows])
        with self._lock:
            self._db.execute("DROP TABLE IF EXISTS chunks_vec")
            self._db.execute(
                "CREATE VIRTUAL TABLE chunks_vec USING vec0("
                "chunk_id INTEGER PRIMARY KEY, embedding float[{}])".format(len(vectors[0]))
            )
            self._db.executemany(
                "INSERT INTO chunks_vec(chunk_id, embedding) VALUES(?, ?)",
                [(rows[i][0], self._serialize(vectors[i])) for i in range(len(rows))],
            )
            self._db.execute(
                "INSERT OR REPLACE INTO rag_meta(key, value) VALUES('embedder', ?)",
                (signature,),
            )
            self._db.execute(
                "INSERT OR REPLACE INTO rag_meta(key, value) VALUES('dim', ?)",
                (str(len(vectors[0])),),
            )
            self._db.commit()
        return len(rows)

    def _current_dim(self) -> int:
        from .embedder import embed_one

        return len(embed_one("维度探测"))

    def count(self, source_type: Optional[str] = None) -> int:
        with self._lock:
            if source_type:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM chunks WHERE source_type=?", (source_type,)
                ).fetchone()
            else:
                row = self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()
            return int(row[0])

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass


def open_rag_store(db_path: str | Path) -> Optional[RagStore]:
    """打开/创建向量库；扩展不可用等任何失败返回 None（记忆功能优雅降级）。"""
    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        return RagStore(db_path)
    except Exception:  # noqa: BLE001
        return None
