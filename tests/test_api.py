"""端到端 API 测试（FastAPI TestClient + 临时 SQLite）。"""

from __future__ import annotations

import threading


def create_doc(client, doc_id="cfg-1", content=None, request_id=None):
    body = {"id": doc_id, "content": content}
    if request_id:
        body["request_id"] = request_id
    return client.post("/documents", json=body)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_full_lifecycle_create_get_history(client):
    r = create_doc(client, content={"a": 1, "b": {"x": 10}})
    assert r.status_code == 201, r.text
    rev1 = r.json()
    assert rev1["revision"] == 1
    assert rev1["base_revision"] is None

    r = client.get("/documents/cfg-1")
    assert r.status_code == 200
    assert r.json()["current_revision"] == 1

    r = client.get("/documents/cfg-1/revisions")
    assert r.status_code == 200
    assert len(r.json()["revisions"]) == 1


def test_create_without_id_generates_uuid(client):
    r = client.post("/documents", json={"content": {"v": 1}})
    assert r.status_code == 201
    assert len(r.json()["doc_id"]) == 32


def test_duplicate_create_returns_409(client):
    create_doc(client)
    r = create_doc(client)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "document_already_exists"


def test_get_missing_document_404(client):
    r = client.get("/documents/nope")
    assert r.status_code == 404


def test_patch_fast_forward_creates_revision(client):
    create_doc(client, content={"a": 1})
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "req-1",
        "patch": {"b": 2}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 2
    assert body["content"] == {"a": 1, "b": 2}
    assert body["base_revision"] == 1
    assert body["merged"] is False


def test_patch_null_deletes_key(client):
    create_doc(client, content={"a": 1, "b": 2})
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "req-del",
        "patch": {"a": None}})
    assert r.status_code == 200
    assert r.json()["content"] == {"b": 2}


def test_patch_non_object_replaces_whole_document(client):
    create_doc(client, content={"a": 1})
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "req-rep",
        "patch": {"a": {"nested": 1}}})
    assert r.status_code == 200
    assert r.json()["content"] == {"a": {"nested": 1}}

    # 根级替换为数组
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 2, "request_id": "req-arr",
        "patch": [1, 2, 3]})
    assert r.status_code == 200
    assert r.json()["content"] == [1, 2, 3]


def test_stale_base_disjoint_paths_auto_replays(client):
    create_doc(client, content={"a": 1, "b": 2})
    # 他人先把 rev 1 -> rev 2（修改 a）
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "other", "patch": {"a": 10}})
    assert r.status_code == 200

    # 我仍基于 rev 1，修改互不冲突的 b -> 自动重放为 rev 3
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "mine", "patch": {"b": 20}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 3
    assert body["merged"] is True
    assert body["content"] == {"a": 10, "b": 20}


def test_stale_base_same_result_auto_replays(client):
    create_doc(client, content={"a": 1, "b": 2})
    # 他人把 a 改成 10
    client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "other", "patch": {"a": 10}})
    # 我基于旧版做完全相同的修改 -> 结果相同，自动合并（no-op，不新增修订）
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "same-change", "patch": {"a": 10}})
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == 2
    assert body["merged"] is False
    assert body["reason"] == "no_changes"


def test_stale_base_diverging_path_returns_409_and_does_not_write(client):
    create_doc(client, content={"a": 1})
    client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "other", "patch": {"a": 2}})

    r = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "mine", "patch": {"a": 3}})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["base_revision"] == 1
    assert err["current_revision"] == 2
    conflict = err["conflicts"][0]
    assert conflict["path"] == "/a"
    assert conflict["base"]["value"] == 1
    assert conflict["current"]["value"] == 2
    assert conflict["incoming"]["value"] == 3

    # 文档保持 rev 2，内容未被污染
    doc = client.get("/documents/cfg-1").json()
    assert doc["current_revision"] == 2
    assert doc["content"] == {"a": 2}
    history = client.get("/documents/cfg-1/revisions").json()
    assert [r["revision"] for r in history["revisions"]] == [1, 2]


def test_delete_vs_modify_returns_409_with_present_flags(client):
    create_doc(client, content={"a": 1, "b": 2})
    # 他人删除 a
    client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "del", "patch": {"a": None}})
    # 我基于 rev 1 修改 a
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "mod", "patch": {"a": 99}})
    assert r.status_code == 409
    c = r.json()["error"]["conflicts"][0]
    assert c["path"] == "/a"
    assert c["current"]["present"] is False
    assert c["incoming"]["present"] is True
    assert c["incoming"]["value"] == 99


def test_idempotent_same_request_same_content(client):
    create_doc(client, content={"a": 1})
    payload = {"base_revision": 1, "request_id": "idem", "patch": {"a": 9}}
    r1 = client.patch("/documents/cfg-1", json=payload)
    assert r1.status_code == 200
    r2 = client.patch("/documents/cfg-1", json=payload)
    assert r2.status_code == 200
    assert r1.json()["revision"] == r2.json()["revision"] == 2
    # 历史中只有一条新修订
    history = client.get("/documents/cfg-1/revisions").json()
    assert len(history["revisions"]) == 2


def test_idempotent_replay_after_other_commits(client):
    # 第一次提交成功（rev 2）；之后他人推进到 rev 3；
    # 客户端用相同 request_id/内容重试 -> 仍幂等返回 rev 2
    create_doc(client, content={"a": 1, "b": 1})
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "idem", "patch": {"a": 2}})
    assert r.status_code == 200
    client.patch("/documents/cfg-1", json={
        "base_revision": 2, "request_id": "other", "patch": {"b": 3}})
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "idem", "patch": {"a": 2}})
    assert r.status_code == 200
    assert r.json()["revision"] == 2


def test_same_request_id_different_content_returns_409(client):
    create_doc(client, content={"a": 1})
    r1 = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "dup", "patch": {"a": 2}})
    assert r1.status_code == 200
    r2 = client.patch("/documents/cfg-1", json={
        "base_revision": 2, "request_id": "dup", "patch": {"a": 3}})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "request_id_conflict"


def test_unknown_base_revision_404(client):
    create_doc(client, content={"a": 1})
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 99, "request_id": "x", "patch": {"a": 2}})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "base_revision_not_found"


def test_revision_history_and_get_one(client):
    create_doc(client, content={"a": 1})
    client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "r1", "patch": {"a": 2}})
    client.patch("/documents/cfg-1", json={
        "base_revision": 2, "request_id": "r2", "patch": {"b": 3}})

    history = client.get("/documents/cfg-1/revisions").json()
    assert history["current_revision"] == 3
    assert [r["revision"] for r in history["revisions"]] == [1, 2, 3]
    assert history["revisions"][1]["request_id"] == "r1"
    assert history["revisions"][1]["base_revision"] == 1

    one = client.get("/documents/cfg-1/revisions/2")
    assert one.status_code == 200
    assert one.json()["content"] == {"a": 2}

    missing = client.get("/documents/cfg-1/revisions/99")
    assert missing.status_code == 404


def test_patch_nonexistent_document_404(client):
    r = client.patch("/documents/ghost", json={
        "base_revision": 1, "request_id": "x", "patch": {}})
    assert r.status_code == 404


def test_invalid_request_shape_rejected(client):
    create_doc(client)
    # 缺字段
    r = client.patch("/documents/cfg-1",
                     json={"base_revision": 1, "request_id": "x"})
    assert r.status_code == 422
    # base_revision 非法
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 0, "request_id": "x", "patch": {}})
    assert r.status_code == 422
    # 多余字段
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "x", "patch": {}, "bogus": 1})
    assert r.status_code == 422


def test_concurrent_disjoint_patches_both_commit(client):
    # 两个客户端都基于 rev 1，并发修改互不相交的路径
    create_doc(client, content={"a": 1, "b": 1})
    results = []

    def patch(key, value, req_id):
        r = client.patch("/documents/cfg-1", json={
            "base_revision": 1, "request_id": req_id,
            "patch": {key: value}})
        results.append(r.status_code)

    t1 = threading.Thread(target=patch, args=("a", 10, "pa"))
    t2 = threading.Thread(target=patch, args=("b", 20, "pb"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [200, 200]
    assert client.get("/documents/cfg-1").json()["content"] == {"a": 10, "b": 20}


def test_concurrent_same_key_patch_one_conflicts(client):
    create_doc(client, content={"a": 1})
    results = []

    def patch(value, req_id):
        r = client.patch("/documents/cfg-1", json={
            "base_revision": 1, "request_id": req_id,
            "patch": {"a": value}})
        results.append(r.status_code)

    t1 = threading.Thread(target=patch, args=(10, "p1"))
    t2 = threading.Thread(target=patch, args=(20, "p2"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [200, 409]


def test_nested_stale_replay_with_mixed_conflict(client):
    create_doc(client, content={"db": {"host": "h", "port": 5432}})
    # 他人改 host
    client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "other",
        "patch": {"db": {"host": "h2"}}})
    # 我基于 rev 1 同时改 port（不冲突）与 host（冲突）
    r = client.patch("/documents/cfg-1", json={
        "base_revision": 1, "request_id": "mine",
        "patch": {"db": {"host": "h3", "port": 6543}}})
    assert r.status_code == 409
    paths = [c["path"] for c in r.json()["error"]["conflicts"]]
    assert paths == ["/db/host"]
    # port 的修改也不得写入
    assert client.get("/documents/cfg-1").json()["content"] == {
        "db": {"host": "h2", "port": 5432}}
