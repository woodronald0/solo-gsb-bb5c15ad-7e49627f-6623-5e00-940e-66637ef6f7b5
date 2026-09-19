"""请求 / 响应模型。

``patch`` 使用 ``model_config = {}`` 关闭额外属性限制无意义——
它需要接受任意 JSON，故类型标注为 ``Any``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CreateDocumentRequest(BaseModel):
    id: Optional[str] = Field(default=None, min_length=1,
                              description="文档 ID；省略则服务端生成 UUID")
    content: Optional[Any] = Field(default=None, description="初始 JSON 内容")
    request_id: Optional[str] = Field(default=None, min_length=1)


class UpdateDocumentRequest(BaseModel):
    model_config = {"extra": "forbid"}

    base_revision: int = Field(..., ge=1,
                               description="来稿所基于的修订号")
    request_id: str = Field(..., min_length=1,
                            description="客户端去重键；同键同内容幂等")
    patch: Any = Field(..., description="RFC 7386 JSON Merge Patch")


class RevisionOut(BaseModel):
    doc_id: str
    revision: int
    base_revision: Optional[int]
    request_id: Optional[str]
    patch: Optional[Any]
    content: Any
    created_at: str


class DocumentOut(BaseModel):
    id: str
    current_revision: int
    content: Any
    created_at: str
    updated_at: str


class ConflictEntry(BaseModel):
    path: str
    base: Dict[str, Any]
    current: Dict[str, Any]
    incoming: Dict[str, Any]


class HistoryOut(BaseModel):
    doc_id: str
    current_revision: int
    revisions: List[RevisionOut]
