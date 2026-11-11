"""并发：多线程/多进程同时写入、开快照、翻页时规则仍成立。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ProcessPoolExecutor
from types import SimpleNamespace

from app.db import Database


def _op(result_id, value=1.0, test_date="2026-10-05", project="COD"):
    return SimpleNamespace(
        op="put",
        id=result_id,
        sample_code=f"WS-{result_id}",
        project=project,
        test_date=test_date,
        value=value,
    )


def _del(result_id):
    return SimpleNamespace(op="delete", id=result_id)


def _worker(db_path, tag, count):
    """子进程入口：各自建连接，连续提交批次。"""
    db = Database(db_path)
    revisions = []
    for i in range(count):
        revisions.append(db.apply_batch([_op(f"{tag}-{i:03d}", value=i)])["revision"])
    return revisions


def test_concurrent_batches_from_threads(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    errors = []

    def work(tag):
        try:
            for i in range(10):
                db.apply_batch([_op(f"{tag}-{i}")])
        except Exception as exc:  # pragma: no cover - 失败时记录
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(t,)) for t in "abcd"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    revisions = db.list_revisions()
    assert len(revisions) == 40
    assert len({r["revision"] for r in revisions}) == 40  # 修订号不重复
    for tag in "abcd":
        for i in range(10):
            assert db.get_current(f"{tag}-{i}") is not None


def test_two_processes_share_database_file(tmp_path):
    db_path = str(tmp_path / "shared.db")
    Database(db_path)  # 先初始化 schema

    with ProcessPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(_worker, db_path, "a", 10)
        fut_b = pool.submit(_worker, db_path, "b", 10)
        revisions = fut_a.result() + fut_b.result()

    assert len(set(revisions)) == 20
    db = Database(db_path)
    assert len(db.list_revisions()) == 20
    assert db.get_current("a-000")["value"] == 0
    assert db.get_current("b-009")["value"] == 9


def test_snapshot_stable_while_concurrent_writes(tmp_path):
    """翻页期间另一连接持续更正/删除/新增，快照页内容不变。"""
    db = Database(str(tmp_path / "s.db"))
    ops = [_op(f"R-{i:03d}", value=i, test_date=f"2026-10-{(i % 27) + 1:02d}")
           for i in range(30)]
    db.apply_batch(ops)
    original = {op.id: op.value for op in ops}

    snapshot = db.create_snapshot("COD", "2026-10-01", "2026-10-31", 7)
    stop = threading.Event()
    write_errors = []

    def writer():
        n = 0
        try:
            while not stop.is_set():
                n += 1
                db.apply_batch([
                    _op("R-000", value=1000 + n),            # 更正已在快照中的记录
                    _del("R-010"),                           # 删除快照中的记录
                    _op(f"NEW-{n:03d}", value=n),            # 新增记录
                    _op("R-029", test_date="2026-11-30", value=-1),  # 移出区间
                ])
        except Exception as exc:  # pragma: no cover
            write_errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        collected = []
        cursor_date = cursor_id = None
        while True:
            rows = db.fetch_page(snapshot, cursor_date, cursor_id, 8)
            collected.extend(rows[:7])
            if len(rows) <= 7:
                break
            cursor_date, cursor_id = rows[6]["test_date"], rows[6]["id"]
            time.sleep(0.01)  # 让写线程有机会插入新修订
    finally:
        stop.set()
        thread.join()

    assert not write_errors
    assert len(collected) == 30
    assert {item["id"] for item in collected} == set(original)
    for item in collected:  # 值仍是快照修订下的原值
        assert item["value"] == original[item["id"]]
    assert not any(item["id"].startswith("NEW-") for item in collected)
