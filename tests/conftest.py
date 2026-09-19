"""pytest 公共夹具：每个测试使用独立的临时 SQLite 数据库。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "test.db"
    settings = Settings(db_path=str(db_path), port=8000)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
