"""测试公共夹具与构造辅助。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "results.db"))
    with TestClient(app) as test_client:
        yield test_client


def make_put(
    result_id: str,
    project: str = "COD",
    test_date: str = "2026-10-05",
    value=1.0,
    sample_code: str | None = None,
) -> dict:
    return {
        "op": "put",
        "id": result_id,
        "sample_code": sample_code or f"WS-{result_id}",
        "project": project,
        "test_date": test_date,
        "value": value,
    }


def make_delete(result_id: str) -> dict:
    return {"op": "delete", "id": result_id}


def submit(client: TestClient, operations: list[dict]):
    return client.post("/api/v1/batches", json={"operations": operations})


def open_snapshot(client: TestClient, project="COD", date_from="2026-10-01",
                  date_to="2026-10-31", page_size=5):
    return client.post(
        "/api/v1/snapshots",
        json={
            "project": project,
            "date_from": date_from,
            "date_to": date_to,
            "page_size": page_size,
        },
    )


def collect_all_pages(client: TestClient, snapshot_id: str, first_page: dict) -> list[dict]:
    """从第一页起翻到底，返回全部条目。"""
    items = list(first_page["items"])
    cursor = first_page["next_cursor"]
    while cursor is not None:
        resp = client.get(f"/api/v1/snapshots/{snapshot_id}/pages", params={"cursor": cursor})
        assert resp.status_code == 200, resp.text
        page = resp.json()["page"]
        items.extend(page["items"])
        cursor = page["next_cursor"]
    return items
