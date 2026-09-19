"""SQLite 存储层。

设计要点：

* 启动时 ``init_db`` 自动建表（文档表 / 修订历史表 / 幂等记录表）；
* 所有写操作在单个事务内完成，事务由进程内 ``threading.RLock`` 串行化，
  保证“读当前版 -> 三方合并 -> 写新修订”不会被并发请求穿插；
* JSON 统一以 ``json.dumps(..., ensure_ascii=False, sort_keys=True)``
  规范化存储，使幂等比较与 RFC 7386 的“相等即同一修改”判定稳定；
* ``request_id`` 幂等通过 ``(doc_id, request_id)`` 唯一索引实现，
  仅记录成功提交的修订。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .config import Settings


def canonical_json(value: Any) -> str:
    """规范化 JSON 文本：键排序、保留 Unicode、以逗号空格分隔。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class IdempotencyConflict(Exception):
    """同一 request_id 重放但内容与首次不同。"""


class Storage:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 基础

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._settings.db_path,
            timeout=self._settings.busy_timeout_ms / 1000.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self._settings.busy_timeout_ms}")
        return conn

    def init_db(self) -> None:
        """幂等建表，容器启动时自动执行。"""

        import os

        os.makedirs(os.path.dirname(self._settings.db_path) or ".", exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id              TEXT PRIMARY KEY,
                    current_rev     INTEGER NOT NULL,
                    content         TEXT    NOT NULL,
                    created_at      TEXT    NOT NULL,
                    updated_at      TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS revisions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id      TEXT    NOT NULL REFERENCES documents(id),
                    revision    INTEGER NOT NULL,
                    patch       TEXT,
                    base_revision INTEGER,
                    request_id  TEXT,
                    content     TEXT    NOT NULL,
                    created_at  TEXT    NOT NULL,
                    UNIQUE (doc_id, revision)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_revisions_request
                    ON revisions(doc_id, request_id)
                    WHERE request_id IS NOT NULL;
                """
            )

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """只读连接（不加写锁），供 GET 端点使用。"""

        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.rollback()
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """串行化的可写事务上下文。

        锁在整个事务期间持有，杜绝两个并发 PATCH 互相穿插造成丢更新。
        """

        conn = self._connect()
        try:
            with self._lock:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ 读取

    def get_document(self, conn: sqlite3.Connection, doc_id: str
                     ) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        return self._document_row(row) if row else None

    def get_revision(self, conn: sqlite3.Connection, doc_id: str, revision: int
                     ) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM revisions WHERE doc_id = ? AND revision = ?",
            (doc_id, revision),
        ).fetchone()
        return self._revision_row(row) if row else None

    def list_revisions(self, conn: sqlite3.Connection, doc_id: str
                       ) -> List[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM revisions WHERE doc_id = ? ORDER BY revision ASC",
            (doc_id,),
        ).fetchall()
        return [self._revision_row(r) for r in rows]

    # ------------------------------------------------------------------ 写入

    def insert_document(self, conn: sqlite3.Connection, doc_id: str,
                        content: Any, request_id: Optional[str],
                        created_at: str) -> None:
        """在给定事务内写入文档与 rev 1（初始修订）。"""

        body = canonical_json(content)
        conn.execute(
            "INSERT INTO documents (id, current_rev, content, created_at, updated_at)"
            " VALUES (?, 1, ?, ?, ?)",
            (doc_id, body, created_at, created_at),
        )
        conn.execute(
            "INSERT INTO revisions (doc_id, revision, patch, base_revision,"
            " request_id, content, created_at) VALUES (?, 1, NULL, NULL, ?, ?, ?)",
            (doc_id, request_id, body, created_at),
        )

    def add_revision(self, conn: sqlite3.Connection, doc_id: str, revision: int,
                     content: Any, patch: Any, base_revision: int,
                     request_id: Optional[str], created_at: str) -> None:
        body = canonical_json(content)
        conn.execute(
            "UPDATE documents SET current_rev = ?, content = ?, updated_at = ?"
            " WHERE id = ?",
            (revision, body, created_at, doc_id),
        )
        conn.execute(
            "INSERT INTO revisions (doc_id, revision, patch, base_revision,"
            " request_id, content, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, revision, patch if patch is None else canonical_json(patch),
             base_revision, request_id, body, created_at),
        )

    def find_idempotent(self, conn: sqlite3.Connection, doc_id: str,
                        request_id: str) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM revisions WHERE doc_id = ? AND request_id = ?",
            (doc_id, request_id),
        ).fetchone()
        return self._revision_row(row) if row else None

    # ------------------------------------------------------------------ 行映射

    @staticmethod
    def _load(value: Optional[str]) -> Any:
        return json.loads(value) if value is not None else None

    def _document_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "current_revision": row["current_rev"],
            "content": self._load(row["content"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _revision_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "doc_id": row["doc_id"],
            "revision": row["revision"],
            "base_revision": row["base_revision"],
            "request_id": row["request_id"],
            "patch": self._load(row["patch"]),
            "content": self._load(row["content"]),
            "created_at": row["created_at"],
        }
