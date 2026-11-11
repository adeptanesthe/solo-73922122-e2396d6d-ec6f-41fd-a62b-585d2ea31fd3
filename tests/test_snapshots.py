"""快照取数：排序、翻页、跨页更正隔离、游标重试、边界。"""

from __future__ import annotations

from .conftest import (
    collect_all_pages,
    make_delete,
    make_put,
    open_snapshot,
    submit,
)

# 12 条 COD 记录，日期错开；排序键 (test_date, id) 唯一确定顺序。
SEED = [
    ("R-001", "2026-10-02", 12.5),
    ("R-002", "2026-10-03", 8.0),
    ("R-003", "2026-10-05", 30.1),
    ("R-004", "2026-10-07", 15.4),
    ("R-005", "2026-10-09", 6.6),
    ("R-006", "2026-10-12", 11.0),
    ("R-007", "2026-10-14", 9.9),
    ("R-008", "2026-10-16", 22.2),
    ("R-009", "2026-10-19", 4.4),
    ("R-010", "2026-10-21", 18.8),
    ("R-011", "2026-10-23", 7.7),
    ("R-012", "2026-10-27", 13.3),
]
SEED_IDS = [rid for rid, _, _ in SEED]
SEED_VALUES = {rid: value for rid, _, value in SEED}


def seed(client):
    ops = [make_put(rid, test_date=d, value=v) for rid, d, v in SEED]
    ops.append(make_put("X-NH3N-1", project="NH3N", test_date="2026-10-10", value=0.5))
    ops.append(make_put("X-NOV", test_date="2026-11-02", value=99.0))
    resp = submit(client, ops)
    assert resp.status_code == 200
    return resp.json()["revision"]


def open_cod_snapshot(client, page_size=5):
    resp = open_snapshot(client, page_size=page_size)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_first_page_order_and_cursor(client):
    rev = seed(client)
    snap = open_cod_snapshot(client, page_size=5)
    assert snap["revision"] == rev
    assert snap["page_size"] == 5
    page = snap["page"]
    assert page["number"] == 1
    assert [it["id"] for it in page["items"]] == SEED_IDS[:5]
    assert page["items"][0]["value"] == 12.5
    assert page["next_cursor"]


def test_paginate_to_end_exactly_once(client):
    seed(client)
    snap = open_cod_snapshot(client, page_size=5)
    items = collect_all_pages(client, snap["snapshot_id"], snap["page"])
    assert [it["id"] for it in items] == SEED_IDS  # 12 条，不重不漏
    # 页大小不被整除时末页只有 2 条
    assert len(items) == 12


def test_exact_page_boundary_ends_with_null_cursor(client):
    seed(client)
    snap = open_cod_snapshot(client, page_size=6)  # 12 = 2 * 6
    items = collect_all_pages(client, snap["snapshot_id"], snap["page"])
    assert [it["id"] for it in items] == SEED_IDS


def test_date_range_is_closed_and_project_filtered(client):
    seed(client)
    snap = open_cod_snapshot(client, page_size=100)
    items = snap["page"]["items"]
    assert {it["project"] for it in items} == {"COD"}        # NH3N 被过滤
    assert "X-NOV" not in {it["id"] for it in items}         # 11 月记录被过滤
    assert all("2026-10-01" <= it["test_date"] <= "2026-10-31" for it in items)

    # 闭区间：起止日当天记录应包含
    submit(client, [make_put("EDGE-FROM", test_date="2026-10-01"),
                    make_put("EDGE-TO", test_date="2026-10-31")])
    snap = open_cod_snapshot(client, page_size=100)
    ids = {it["id"] for it in snap["page"]["items"]}
    assert {"EDGE-FROM", "EDGE-TO"} <= ids


def test_page_size_and_range_validation(client):
    assert open_snapshot(client, page_size=0).status_code == 422
    assert open_snapshot(client, page_size=101).status_code == 422
    assert open_snapshot(client, date_from="2026-11-01", date_to="2026-10-01").status_code == 422
    assert open_snapshot(client, date_from="2026-10-1").status_code == 422


def test_empty_snapshot(client):
    resp = open_snapshot(client, project="TP")
    assert resp.status_code == 200
    page = resp.json()["page"]
    assert page["items"] == [] and page["next_cursor"] is None


def test_cross_page_corrections_do_not_leak_into_snapshot(client):
    """开快照后发生更正/删除/移出/新增：翻页仍看到原修订下的记录和值。"""
    seed(client)
    snap = open_cod_snapshot(client, page_size=5)
    page1 = snap["page"]
    assert [it["id"] for it in page1["items"]] == SEED_IDS[:5]

    # 跨页更正（修订 2）：
    submit(client, [
        make_put("R-003", test_date="2026-10-05", value=28.4),   # 改第 1 页已读记录的值
        make_delete("R-006"),                                     # 删第 2 页记录
        make_put("R-011", test_date="2026-11-02", value=7.7),     # 把第 3 页记录移出区间
        make_put("R-013", test_date="2026-10-25", value=5.5),     # 新增区间内记录
    ])

    # 继续用旧游标翻页：仍是原来的 12 条、原来的值
    items = collect_all_pages(client, snap["snapshot_id"], page1)
    assert [it["id"] for it in items] == SEED_IDS
    for item in items:
        assert item["value"] == SEED_VALUES[item["id"]]
        assert item["test_date"] == dict((r, d) for r, d, _ in SEED)[item["id"]]
    assert "R-013" not in {it["id"] for it in items}

    # 新快照才看得到修订 2 的状态（R-013 日期 10-25，按日期排在 R-012 之前）
    snap2 = open_cod_snapshot(client, page_size=100)
    ids2 = [it["id"] for it in snap2["page"]["items"]]
    expected = [i for i in SEED_IDS if i not in {"R-006", "R-011", "R-012"}]
    expected += ["R-013", "R-012"]
    assert ids2 == expected
    values2 = {it["id"]: it["value"] for it in snap2["page"]["items"]}
    assert values2["R-003"] == 28.4
    assert snap2["revision"] == snap["revision"] + 1


def test_cursor_retry_returns_same_page(client):
    seed(client)
    snap = open_cod_snapshot(client, page_size=5)
    cursor = snap["page"]["next_cursor"]

    url = f"/api/v1/snapshots/{snap['snapshot_id']}/pages"
    first = client.get(url, params={"cursor": cursor})
    assert first.status_code == 200

    # 重试之间发生新批次也不影响
    submit(client, [make_delete("R-007"), make_put("R-100", test_date="2026-10-08")])
    second = client.get(url, params={"cursor": cursor})

    assert second.status_code == 200
    assert second.json() == first.json()
    assert [it["id"] for it in second.json()["page"]["items"]] == SEED_IDS[5:10]


def test_snapshot_meta_and_unknown_snapshot(client):
    seed(client)
    snap = open_cod_snapshot(client)
    meta = client.get(f"/api/v1/snapshots/{snap['snapshot_id']}")
    assert meta.status_code == 200
    assert meta.json()["revision"] == snap["revision"]
    assert meta.json()["page_size"] == 5

    assert client.get("/api/v1/snapshots/nope").status_code == 404
    assert client.get("/api/v1/snapshots/nope/pages", params={"cursor": "v1.e30"}).status_code == 404


def test_invalid_or_mismatched_cursor(client):
    seed(client)
    snap = open_cod_snapshot(client)
    other = open_cod_snapshot(client)
    url = f"/api/v1/snapshots/{snap['snapshot_id']}/pages"

    assert client.get(url, params={"cursor": "not-a-cursor"}).status_code == 400
    assert client.get(url, params={"cursor": "v1.!!!"}).status_code == 400
    # 另一快照的游标不能串用
    cursor = other["page"]["next_cursor"]
    assert client.get(url, params={"cursor": cursor}).status_code == 400
