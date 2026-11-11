"""批次写入/删除：校验、原子性、替换语义、修订号。"""

from __future__ import annotations

import pytest

from .conftest import make_delete, make_put, submit


def test_put_batch_returns_incrementing_revision(client):
    resp = submit(client, [make_put("R-1", value=12.5), make_put("R-2", value="未检出")])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"revision": 1, "applied": 2, "puts": 2, "deletes": 0}

    resp = submit(client, [make_put("R-3", value=7)])
    assert resp.json()["revision"] == 2

    revisions = client.get("/api/v1/revisions").json()
    assert [r["revision"] for r in revisions] == [1, 2]
    assert revisions[0]["puts"] == 2 and revisions[0]["deletes"] == 0
    assert revisions[1]["puts"] == 1


def test_value_types_round_trip(client):
    submit(client, [
        make_put("N-INT", value=7),
        make_put("N-FLOAT", value=0.1),
        make_put("N-STR", value="未检出"),
        make_put("N-EMPTY", value=""),
    ])
    assert client.get("/api/v1/results/N-INT").json()["value"] == 7
    assert client.get("/api/v1/results/N-FLOAT").json()["value"] == 0.1
    assert client.get("/api/v1/results/N-STR").json()["value"] == "未检出"
    assert client.get("/api/v1/results/N-EMPTY").json()["value"] == ""


def test_same_id_put_replaces(client):
    submit(client, [make_put("R-1", value=1.0)])
    submit(client, [make_put("R-1", value=2.0, test_date="2026-10-06")])
    item = client.get("/api/v1/results/R-1").json()
    assert item["value"] == 2.0
    assert item["test_date"] == "2026-10-06"


def test_delete_by_id_and_idempotency(client):
    submit(client, [make_put("R-1")])
    resp = submit(client, [make_delete("R-1")])
    assert resp.json()["deletes"] == 1
    assert client.get("/api/v1/results/R-1").status_code == 404

    # 删除不存在的 ID：幂等空操作，批次仍成功并产生修订号
    resp = submit(client, [make_delete("R-1"), make_delete("R-404")])
    assert resp.status_code == 200
    assert resp.json()["revision"] == 3


def test_mixed_batch(client):
    submit(client, [make_put("A"), make_put("B")])
    resp = submit(client, [make_put("A", value=9.9), make_delete("B"), make_put("C")])
    body = resp.json()
    assert body["puts"] == 2 and body["deletes"] == 1
    assert client.get("/api/v1/results/A").json()["value"] == 9.9
    assert client.get("/api/v1/results/B").status_code == 404
    assert client.get("/api/v1/results/C").status_code == 200


@pytest.mark.parametrize(
    "operation",
    [
        make_put("X", test_date="2026/10/01"),      # 日期格式错误
        make_put("X", test_date="2026-02-30"),      # 不存在的日期
        make_put("X", test_date="2026-13-01"),      # 月份越界
        make_put("X", value=True),                  # 布尔值
        make_put("X", value=None),                  #  null
        make_put("X", value=[1, 2]),                # 数组
        {**make_put("X"), "id": ""},              # 空 ID
        {**make_put("X"), "extra": 1},              # 多余字段
        {"op": "put", "id": "X"},                   # 字段不完整
        {"op": "delete"},                           # 删除缺 ID
        {"op": "unknown", "id": "X"},               # 未知操作
    ],
    ids=lambda op: str(op)[:60],
)
def test_invalid_operation_rejected(client, operation):
    resp = submit(client, [operation])
    assert resp.status_code == 422, resp.text


def test_non_finite_float_rejected(client):
    """严格 JSON 无法表示 NaN/Infinity，但宽松解析的客户端可能发来；服务端必须拒绝。"""
    for token in ("NaN", "Infinity", "-Infinity"):
        body = (
            '{"operations": [{"op": "put", "id": "X", "sample_code": "s", '
            '"project": "COD", "test_date": "2026-10-05", "value": ' + token + "}]}"
        )
        resp = client.post(
            "/api/v1/batches", content=body, headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 422, token
    assert client.get("/api/v1/revisions").json() == []


def test_batch_size_limits(client):
    assert submit(client, []).status_code == 422
    assert submit(client, [make_put(f"R-{i}") for i in range(101)]).status_code == 422
    assert submit(client, [make_put(f"R-{i}") for i in range(100)]).status_code == 200


def test_duplicate_ids_within_batch_rejected(client):
    resp = submit(client, [make_put("R-1", value=1), make_put("R-1", value=2)])
    assert resp.status_code == 422


def test_failed_batch_is_fully_rolled_back(client):
    submit(client, [make_put("KEEP")])
    before = client.get("/api/v1/revisions").json()

    resp = submit(client, [
        make_put("NEW-1"),
        make_delete("KEEP"),
        make_put("NEW-2", test_date="2026-02-30"),  # 非法 → 整批失败
    ])
    assert resp.status_code == 422

    # 全部不生效：无新修订、无新记录、原记录未删
    assert client.get("/api/v1/revisions").json() == before
    assert client.get("/api/v1/results/NEW-1").status_code == 404
    assert client.get("/api/v1/results/KEEP").status_code == 200
