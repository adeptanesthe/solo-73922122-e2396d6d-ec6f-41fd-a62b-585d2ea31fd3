"""重启持久化：修订历史与游标在应用实例重建后仍可用。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import make_put, open_snapshot, submit


def test_revisions_and_cursors_survive_restart(tmp_path):
    db_path = str(tmp_path / "results.db")

    with TestClient(create_app(db_path)) as client1:
        submit(client1, [make_put(f"R-{i:03d}", test_date=f"2026-10-{i:02d}", value=i)
                         for i in range(1, 11)])
        submit(client1, [make_put("R-001", test_date="2026-10-01", value=111)])
        snap = client1.post(
            "/api/v1/snapshots",
            json={"project": "COD", "date_from": "2026-10-01",
                  "date_to": "2026-10-31", "page_size": 4},
        ).json()
        assert snap["revision"] == 2
        cursor = snap["page"]["next_cursor"]
        page2_before = client1.get(
            f"/api/v1/snapshots/{snap['snapshot_id']}/pages",
            params={"cursor": cursor},
        ).json()

    # 模拟重启：同一数据库文件上创建全新的应用实例
    with TestClient(create_app(db_path)) as client2:
        revisions = client2.get("/api/v1/revisions").json()
        assert [r["revision"] for r in revisions] == [1, 2]

        meta = client2.get(f"/api/v1/snapshots/{snap['snapshot_id']}").json()
        assert meta["revision"] == 2

        # 旧游标继续翻页，结果与重启前一致
        page2_after = client2.get(
            f"/api/v1/snapshots/{snap['snapshot_id']}/pages",
            params={"cursor": cursor},
        ).json()
        assert page2_after == page2_before
        assert [it["id"] for it in page2_after["page"]["items"]] == [
            "R-005", "R-006", "R-007", "R-008",
        ]
