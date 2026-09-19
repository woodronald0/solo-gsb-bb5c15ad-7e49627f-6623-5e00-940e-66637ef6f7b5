"""FastAPI 应用：共享 JSON 配置的三方合并 API。

端点：

* ``POST   /documents``                创建文档
* ``GET    /documents/{doc_id}``       读取当前版
* ``PATCH  /documents/{doc_id}``       携带 base_revision / request_id / Merge Patch 更新
* ``GET    /documents/{doc_id}/revisions``                  修订历史
* ``GET    /documents/{doc_id}/revisions/{revision}``       指定修订
* ``GET    /healthz``                  健康检查
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from .config import Settings, settings as default_settings
from .merge import Conflict, apply_patch, three_way
from .schemas import (
    CreateDocumentRequest,
    DocumentOut,
    HistoryOut,
    RevisionOut,
    UpdateDocumentRequest,
)
from .storage import Storage, canonical_json, utc_now_iso


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or default_settings
    storage = Storage(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动时自动建库建表（幂等）；导入模块本身不产生文件副作用
        storage.init_db()
        yield

    app = FastAPI(
        title="共享 JSON 配置三方合并 API",
        version="1.0.0",
        description="基于 JSON Merge Patch (RFC 7386) 的乐观锁 / 三方自动重放服务。",
        lifespan=lifespan,
    )
    app.state.storage = storage

    # --------------------------------------------------------------- 错误响应

    def error(status: int, code: str, message: str,
              **extra: Any) -> JSONResponse:
        body: Dict[str, Any] = {"error": {"code": code, "message": message}}
        body["error"].update(extra)
        return JSONResponse(status_code=status, content=body)

    def conflict_response(conflicts: list[Conflict], base_revision: int,
                          current_revision: int) -> JSONResponse:
        return error(
            409,
            "revision_conflict",
            f"基线 rev {base_revision} 已过期，{len(conflicts)} 个路径无法自动合并；"
            "本次请求未写入任何内容。",
            base_revision=base_revision,
            current_revision=current_revision,
            conflicts=[c.to_dict() for c in conflicts],
        )

    # --------------------------------------------------------------- 健康检查

    @app.get("/healthz", tags=["meta"])
    def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    # --------------------------------------------------------------- 创建文档

    @app.post("/documents", response_model=RevisionOut, status_code=201,
              tags=["documents"])
    def create_document(req: CreateDocumentRequest) -> Any:
        doc_id = req.id or uuid.uuid4().hex
        with storage.transaction() as conn:
            if storage.get_document(conn, doc_id) is not None:
                return error(409, "document_already_exists",
                             f"文档 {doc_id} 已存在，请改用 PATCH 更新。")
            now = utc_now_iso()
            storage.insert_document(
                conn, doc_id, req.content, req.request_id, now)
            row = storage.get_revision(conn, doc_id, 1)
            assert row is not None
        return RevisionOut(**row)

    # --------------------------------------------------------------- 读取文档

    @app.get("/documents/{doc_id}", response_model=DocumentOut,
             tags=["documents"])
    def get_document(doc_id: str) -> Any:
        with storage.read_connection() as conn:
            doc = storage.get_document(conn, doc_id)
        if doc is None:
            return error(404, "document_not_found",
                         f"文档 {doc_id} 不存在")
        return DocumentOut(**doc)

    # --------------------------------------------------------------- 更新文档

    @app.patch("/documents/{doc_id}", response_model=RevisionOut,
               tags=["documents"])
    def patch_document(doc_id: str, req: UpdateDocumentRequest) -> Any:
        with storage.transaction() as conn:
            doc = storage.get_document(conn, doc_id)
            if doc is None:
                return error(404, "document_not_found",
                             f"文档 {doc_id} 不存在")

            # 1) 幂等：同一 (doc_id, request_id) 已提交过。
            prior = storage.find_idempotent(conn, doc_id, req.request_id)
            if prior is not None:
                same_patch = canonical_json(prior["patch"]) == canonical_json(req.patch)
                same_base = prior["base_revision"] == req.base_revision
                if same_patch and same_base:
                    was_merged = (
                        prior["base_revision"] is not None
                        and prior["revision"] > prior["base_revision"] + 1
                    )
                    return JSONResponse(
                        status_code=200,
                        content=RevisionOut(**prior).model_dump()
                        | {"merged": was_merged, "idempotent": True},
                    )
                return error(
                    409,
                    "request_id_conflict",
                    f"request_id {req.request_id!r} 已用于不同的内容，"
                    "拒绝重复使用。",
                    base_revision=req.base_revision,
                    current_revision=doc["current_revision"],
                    stored={"base_revision": prior["base_revision"],
                            "patch": prior["patch"]},
                )

            # 2) 基线必须存在。
            base_row = storage.get_revision(conn, doc_id, req.base_revision)
            if base_row is None:
                return error(
                    404,
                    "base_revision_not_found",
                    f"基线 rev {req.base_revision} 不存在"
                    f"（当前最新为 rev {doc['current_revision']}）。",
                    current_revision=doc["current_revision"],
                )

            base = base_row["content"]
            current = doc["content"]

            # 3) 计算来稿（把 Merge Patch 重放到基线上）。
            incoming = apply_patch(base, req.patch)

            # 4) 三方递归比较；有冲突 -> 409，绝不写入。
            merged, conflicts = three_way(base, current, incoming)
            if conflicts:
                return conflict_response(
                    conflicts, req.base_revision, doc["current_revision"])

            # 5) 合并结果与当前版一致 -> no-op，不产生新修订号。
            if canonical_json(merged) == canonical_json(current):
                current_rev = storage.get_revision(
                    conn, doc_id, doc["current_revision"])
                assert current_rev is not None
                return JSONResponse(
                    status_code=200,
                    content=RevisionOut(**current_rev).model_dump()
                    | {"merged": False, "reason": "no_changes"},
                )

            # 6) 自动重放成功，生成新修订。
            new_revision = doc["current_revision"] + 1
            now = utc_now_iso()
            storage.add_revision(
                conn,
                doc_id=doc_id,
                revision=new_revision,
                content=merged,
                patch=req.patch,
                base_revision=req.base_revision,
                request_id=req.request_id,
                created_at=now,
            )
            row = storage.get_revision(conn, doc_id, new_revision)
            assert row is not None
            return JSONResponse(
                status_code=200,
                content=RevisionOut(**row).model_dump()
                | {"merged": new_revision > req.base_revision + 1},
            )

    # --------------------------------------------------------------- 修订历史

    @app.get("/documents/{doc_id}/revisions", response_model=HistoryOut,
             tags=["documents"])
    def list_revisions(doc_id: str) -> Any:
        with storage.read_connection() as conn:
            doc = storage.get_document(conn, doc_id)
            if doc is None:
                return error(404, "document_not_found",
                             f"文档 {doc_id} 不存在")
            revisions = storage.list_revisions(conn, doc_id)
        return HistoryOut(
            doc_id=doc_id,
            current_revision=doc["current_revision"],
            revisions=[RevisionOut(**r) for r in revisions],
        )

    @app.get("/documents/{doc_id}/revisions/{revision}",
             response_model=RevisionOut, tags=["documents"])
    def get_revision(doc_id: str, revision: int) -> Any:
        with storage.read_connection() as conn:
            doc = storage.get_document(conn, doc_id)
            if doc is None:
                return error(404, "document_not_found",
                             f"文档 {doc_id} 不存在")
            rev = storage.get_revision(conn, doc_id, revision)
        if rev is None:
            return error(404, "revision_not_found",
                         f"文档 {doc_id} 不存在 rev {revision}")
        return RevisionOut(**rev)

    return app


app = create_app()
