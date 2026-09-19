"""运行期配置，全部来自环境变量，均有默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # 默认放工作目录下的 data/；容器内通过 MERGE_DB_PATH=/data/merge.db 覆盖
    db_path: str = os.getenv("MERGE_DB_PATH", "data/merge.db")
    host: str = os.getenv("MERGE_HOST", "0.0.0.0")
    port: int = int(os.getenv("MERGE_PORT", "8000"))
    # SQLite 繁忙等待（毫秒），配合写事务串行化
    busy_timeout_ms: int = int(os.getenv("MERGE_BUSY_TIMEOUT_MS", "5000"))


settings = Settings()
