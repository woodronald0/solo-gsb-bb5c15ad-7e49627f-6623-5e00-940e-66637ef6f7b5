# 共享 JSON 配置三方合并 API

基于 **Python + FastAPI + SQLite** 的共享 JSON 文档服务。更新采用
[RFC 7386 JSON Merge Patch](https://www.rfc-editor.org/rfc/rfc7386) 与乐观锁：
客户端携带 `base_revision` 提交补丁，基线过期时服务端递归比较
**基线 / 当前版 / 来稿** 三方内容——路径互不冲突或合并结果相同则自动重放并生成新修订；
存在分歧路径时返回 `409`，列出冲突路径及三方值，**不写入任何内容**。

---

## 一键启动

需要 Docker 20.10+ 与 Docker Compose v2：

```bash
docker compose up -d --build
```

该命令会构建镜像、启动容器，并在应用启动时**自动建表**（无需任何迁移步骤）。
SQLite 数据文件保存在 Docker 命名卷 `merge-data`（容器内 `/data/merge.db`）。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据卷
```

## 访问地址与端口

| 用途 | 地址 |
| --- | --- |
| 服务根路径（重定向到文档页） | http://localhost:8000/ |
| Swagger 交互文档 | http://localhost:8000/docs |
| OpenAPI 描述 | http://localhost:8000/openapi.json |
| 健康检查 | http://localhost:8000/healthz |
| 文档 API 前缀 | `http://localhost:8000/documents` |

宿主机端口由环境变量 `MERGE_PORT` 控制（默认 `8000`），例如改用 18000：

```bash
MERGE_PORT=18000 docker compose up -d --build
# 访问 http://localhost:18000/docs
```

## 配置项

所有配置均通过环境变量提供，全部可省略：

| 环境变量 | 默认值（容器内） | 说明 |
| --- | --- | --- |
| `MERGE_HOST` | `0.0.0.0` | uvicorn 监听地址 |
| `MERGE_PORT` | `8000` | 容器内监听端口（compose 同时用作宿主机映射端口） |
| `MERGE_DB_PATH` | `/data/merge.db` | SQLite 数据库文件路径（自动建库建表） |
| `MERGE_BUSY_TIMEOUT_MS` | `5000` | SQLite 写锁繁忙等待毫秒数 |

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/documents` | 创建文档（生成 rev 1） |
| `GET` | `/documents/{doc_id}` | 读取当前版 |
| `PATCH` | `/documents/{doc_id}` | 携带 `base_revision` / `request_id` / Merge Patch 更新 |
| `GET` | `/documents/{doc_id}/revisions` | 修订历史（按修订号升序） |
| `GET` | `/documents/{doc_id}/revisions/{revision}` | 查询指定修订 |
| `GET` | `/healthz` | 健康检查 |

### 1. 创建文档

```bash
curl -X POST http://localhost:8000/documents \
  -H 'Content-Type: application/json' \
  -d '{"id":"app-config","content":{"a":1,"b":{"x":10}},"request_id":"create-1"}'
```

* `id` 省略时服务端生成 UUID hex；
* `content` 为任意 JSON（省略或 `null` 表示初始内容为 null）；
* 同名文档已存在返回 `409 document_already_exists`。

### 2. 读取文档

```bash
curl http://localhost:8000/documents/app-config
```

### 3. 更新（核心）

```bash
curl -X PATCH http://localhost:8000/documents/app-config \
  -H 'Content-Type: application/json' \
  -d '{
        "base_revision": 1,
        "request_id": "change-a-to-2",
        "patch": {"a": 2}
      }'
```

请求字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `base_revision` | 是（≥ 1） | 来稿所基于的修订号；不存在返回 `404 base_revision_not_found` |
| `request_id` | 是（非空） | 客户端去重键，作用域为单个文档 |
| `patch` | 是（任意 JSON） | RFC 7386 Merge Patch |

#### Merge Patch 语义（RFC 7386）

* 补丁是对象：逐键合并，值为 **`null` 表示删除该键**，嵌套对象递归；
* 补丁不是对象（数组、标量、`null`）：**整体替换**目标文档；
* 标量目标遇到对象补丁时，标量先按空对象处理。

#### 基线过期时的三方合并

服务端先把补丁重放到基线上得到“来稿”，再递归比较三方：

1. 当前版、来稿相对基线**都没改** → 取当前版；
2. **只有一方改** → 直接采用该方（自动重放）；
3. 双方都改但**结果相同** → 采用该结果（不产生新修订，返回 `merged:false, reason:"no_changes"`）；
4. 当前版与来稿**都是对象** → 按键递归，互不相交的路径自动合并；
5. 同一路径双方改成不同值（含“一方删除 / 一方修改”、类型互异、对象替换标量）
   → `409 revision_conflict`，**本次请求完全不写入**。

`409` 响应示例（路径为 RFC 6901 JSON Pointer，`present:false` 表示该版本中键不存在）：

```json
{
  "error": {
    "code": "revision_conflict",
    "message": "基线 rev 1 已过期，1 个路径无法自动合并；本次请求未写入任何内容。",
    "base_revision": 1,
    "current_revision": 3,
    "conflicts": [
      {
        "path": "/a",
        "base":     {"present": true,  "value": 1},
        "current":  {"present": true,  "value": 2},
        "incoming": {"present": true,  "value": 77}
      }
    ]
  }
}
```

#### 幂等

* 同一 `(doc_id, request_id)` **且 `base_revision` 与 `patch` 相同** →
  直接返回首次生成的修订（HTTP 200，不新增修订号）；
* 同一 `request_id` 携带**不同内容** → `409 request_id_conflict`，不写入。

成功的更新响应在修订对象基础上附加：

* `merged: true/false` —— 是否在过期基线上经三方自动重放；
* `reason: "no_changes"` —— 合并结果与当前版相同（仅该情形出现）。

### 4. 修订历史

```bash
curl http://localhost:8000/documents/app-config/revisions
curl http://localhost:8000/documents/app-config/revisions/2
```

每条修订包含：`revision`、`base_revision`、`request_id`、`patch`、
合并后全文 `content`、`created_at`（UTC，ISO 8601）。

### 错误格式

所有错误统一为：

```json
{"error": {"code": "<机器可读码>", "message": "<说明>", "...": "附加上下文"}}
```

| 状态码 | code |
| --- | --- |
| 404 | `document_not_found` / `revision_not_found` / `base_revision_not_found` |
| 409 | `revision_conflict` / `request_id_conflict` / `document_already_exists` |
| 422 | 请求体校验失败（Pydantic） |

---

## 并发与一致性

* 所有写事务由进程内锁 + SQLite `BEGIN IMMEDIATE` 串行化，
  “读当前版 → 三方合并 → 写新修订”不会被并发请求穿插，无丢更新；
* SQLite 开启 WAL 模式与外键约束；
* JSON 以键排序的规范化文本存储，保证幂等比较稳定。

## 本地开发（不使用 Docker）

```bash
pip install -r requirements-dev.txt
python -m pytest                     # 43 个测试（合并算法 + API 端到端）
uvicorn app.main:app --reload        # 默认使用 ./data/merge.db
```

## 目录结构

```
app/
  config.py      # 环境变量配置
  merge.py       # RFC 7386 补丁 + 递归三方合并（纯函数，无依赖）
  storage.py     # SQLite：建表、串行化事务、幂等索引
  schemas.py     # Pydantic 请求/响应模型
  main.py        # FastAPI 路由与合并编排
tests/           # pytest 单元 / 集成测试
Dockerfile
docker-compose.yml
```
