"""FastAPI 入口：文档创建 / 读取 / 修订历史 / 带三方合并的更新。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from . import db
from .config import settings
from .merge import apply_merge_patch, present_conflict, three_way_merge
from .schemas import CreateDocument, UpdateDocument


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    # 一键启动：确保目录存在并自动建表
    db.init_db()
    yield


app = FastAPI(
    title="共享 JSON 配置三方合并 API",
    version="1.0.0",
    description="基于 JSON Merge Patch (RFC 7386) 的共享配置存储；"
    "基线过期时递归三方合并，路径冲突返回 409。",
    lifespan=lifespan,
)


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def err(status_code: int, code: str, **extra: Any) -> HTTPException:
    detail: dict[str, Any] = {"error": code}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def row_content(row: sqlite3.Row) -> Any:
    return json.loads(row["content"])


# ---------------------------------------------------------------- 健康检查


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {"service": "json-config-merge", "status": "ok", "docs": "/docs"}


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------- 创建


@app.post("/documents", status_code=status.HTTP_201_CREATED, tags=["documents"])
def create_document(
    body: CreateDocument, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    doc_id = body.id or hashlib.sha1(db.utcnow().encode()).hexdigest()[:16]
    now = db.utcnow()
    try:
        with db.transaction(conn):
            if db.get_document(conn, doc_id) is not None:
                raise err(
                    status.HTTP_409_CONFLICT,
                    "document_exists",
                    document_id=doc_id,
                    reason="文档 ID 已存在",
                )
            db.insert_document(conn, doc_id, body.content, body.author, now)
    except HTTPException:
        raise
    return {
        "id": doc_id,
        "revision": 1,
        "content": body.content,
        "author": body.author,
        "created_at": now,
    }


# ---------------------------------------------------------------- 读取


@app.get("/documents", tags=["documents"])
def list_documents(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT id, current_revision, created_at, updated_at FROM documents"
        " ORDER BY id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "limit": limit,
        "offset": offset,
    }


@app.get("/documents/{doc_id}", tags=["documents"])
def get_document(
    doc_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    rev = db.get_revision(conn, doc_id, doc["current_revision"])
    return {
        "id": doc_id,
        "revision": doc["current_revision"],
        "content": row_content(rev),
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
    }


@app.get("/documents/{doc_id}/revisions", tags=["documents"])
def get_revisions(
    doc_id: str,
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    limit = min(limit, settings.history_max_limit)
    rows = db.list_revisions(conn, doc_id, limit, offset, desc=(order == "desc"))
    items = [dict(r) for r in rows]
    for item in items:
        item["replayed"] = bool(item["replayed"])
    return {"document_id": doc_id, "items": items, "limit": limit, "offset": offset}


@app.get("/documents/{doc_id}/revisions/{revision}", tags=["documents"])
def get_one_revision(
    doc_id: str,
    revision: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    if db.get_document(conn, doc_id) is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    row = db.get_revision(conn, doc_id, revision)
    if row is None:
        raise err(
            status.HTTP_404_NOT_FOUND,
            "revision_not_found",
            document_id=doc_id,
            revision=revision,
        )
    return {
        "document_id": doc_id,
        "revision": row["revision"],
        "content": json.loads(row["content"]),
        "patch": json.loads(row["patch"]) if row["patch"] is not None else None,
        "base_revision": row["base_revision"],
        "request_id": row["request_id"],
        "replayed": bool(row["replayed"]),
        "author": row["author"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------- 更新（三方合并）


@app.patch("/documents/{doc_id}", tags=["documents"])
def update_document(
    doc_id: str,
    body: UpdateDocument,
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)

    # 幂等指纹：request_id 相同且 (base_revision, patch) 相同 → 同一请求
    fingerprint = hashlib.sha256(
        f"{body.base_revision}\n{db.canonical(body.patch)}".encode("utf-8")
    ).hexdigest()

    existing = db.get_request(conn, doc_id, body.request_id)
    if existing is not None:
        if existing["fingerprint"] != fingerprint:
            raise err(
                status.HTTP_409_CONFLICT,
                "request_id_conflict",
                document_id=doc_id,
                request_id=body.request_id,
                reason="同一 request_id 已用于 base_revision/patch 不同的请求",
            )
        rev_row = db.get_revision(conn, doc_id, existing["revision"])
        return JSONResponse(
            {
                "id": doc_id,
                "revision": existing["revision"],
                "base_revision": body.base_revision,
                "request_id": body.request_id,
                "replayed": None,
                "changed": bool(existing["changed"]),
                "idempotent": True,
                "content": json.loads(rev_row["content"]),
                "created_at": rev_row["created_at"],
            }
        )

    base_row = db.get_revision(conn, doc_id, body.base_revision)
    if base_row is None:
        raise err(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "unknown_base_revision",
            document_id=doc_id,
            base_revision=body.base_revision,
            current_revision=doc["current_revision"],
            reason="base_revision 不存在",
        )

    base_content = json.loads(base_row["content"])

    # 写事务内重读，消除“检查后写入”的并发窗口（BEGIN IMMEDIATE 取写锁）
    with db.transaction(conn):
        doc = db.get_document(conn, doc_id)
        existing = db.get_request(conn, doc_id, body.request_id)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise err(
                    status.HTTP_409_CONFLICT,
                    "request_id_conflict",
                    document_id=doc_id,
                    request_id=body.request_id,
                    reason="同一 request_id 已用于 base_revision/patch 不同的请求",
                )
            current_row = db.get_revision(conn, doc_id, existing["revision"])
            payload = {
                "id": doc_id,
                "revision": existing["revision"],
                "base_revision": body.base_revision,
                "request_id": body.request_id,
                "replayed": None,
                "changed": bool(existing["changed"]),
                "idempotent": True,
                "content": json.loads(current_row["content"]),
                "created_at": current_row["created_at"],
            }
            return JSONResponse(payload)

        current_revision = doc["current_revision"]
        current_content = json.loads(
            db.get_revision(conn, doc_id, current_revision)["content"]
        )

        # 第一步：把 Merge Patch 应用到基线上，得到“来稿想要的结果”
        theirs = apply_merge_patch(base_content, body.patch)

        if body.base_revision == current_revision:
            # 基线就是当前版：快路径，无需三方比较
            merged, replayed = theirs, False
        else:
            # 基线过期：递归比较 base / current / theirs
            merged, raw_conflicts = three_way_merge(
                base_content, current_content, theirs
            )
            replayed = True
            if raw_conflicts:
                # 冲突：事务回滚，绝不写入
                conflicts_out = [present_conflict(c) for c in raw_conflicts]
                raise err(
                    status.HTTP_409_CONFLICT,
                    "conflict",
                    document_id=doc_id,
                    request_id=body.request_id,
                    base_revision=body.base_revision,
                    current_revision=current_revision,
                    conflicts=conflicts_out,
                )

        changed = db.canonical(merged) != db.canonical(current_content)
        now = db.utcnow()
        if changed:
            new_revision = current_revision + 1
            db.insert_revision(
                conn,
                doc_id,
                new_revision,
                merged,
                body.patch,
                body.base_revision,
                body.request_id,
                replayed,
                body.author,
                now,
            )
        else:
            # 自动重放后结果与当前版相同：不生成新修订
            new_revision = current_revision

        db.insert_request(
            conn, doc_id, body.request_id, fingerprint, new_revision, changed
        )

    return JSONResponse(
        {
            "id": doc_id,
            "revision": new_revision,
            "base_revision": body.base_revision,
            "request_id": body.request_id,
            "replayed": replayed,
            "changed": changed,
            "idempotent": False,
            "content": merged,
            "created_at": now if changed else doc["updated_at"],
        }
    )
