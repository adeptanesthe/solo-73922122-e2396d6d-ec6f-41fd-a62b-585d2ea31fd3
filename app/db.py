"""SQLite 存储层：修订号、结果版本链与取数快照。

设计要点：

- ``result_versions`` 只追加、从不原地修改或删除历史行。每行在修订区间
  ``[valid_from, valid_to)`` 内生效，``valid_to`` 为 NULL 表示当前生效；
  删除用 ``deleted=1`` 的墓碑版本表示。因此任意历史修订下的可见状态都可重建。
- 每个成功批次独占一个递增修订号（``revisions`` 自增主键），批次内全部
  写入/删除与修订行在同一事务提交；任何失败整体回滚，不产生修订号。
- 快照（``snapshots``）只记录创建时固定的修订号与筛选条件；翻页用键集游标
  ``(test_date, id)`` + ``LIMIT`` 按页取数。应用从不整表载入记录，
  也不为取数复制整份结果表。
- WAL + busy_timeout + BEGIN IMMEDIATE：两个服务进程共享同一数据库文件时，
  并发写入、开启快照与翻页仍满足原子性与快照一致性。
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS revisions (
    revision   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    puts       INTEGER NOT NULL,
    deletes    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS result_versions (
    id          TEXT NOT NULL,
    valid_from  INTEGER NOT NULL,
    valid_to    INTEGER,
    sample_code TEXT,
    project     TEXT,
    test_date   TEXT,
    value_json  TEXT,
    deleted     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (id, valid_from)
);

CREATE INDEX IF NOT EXISTS idx_result_versions_scope
    ON result_versions (project, test_date, id);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    date_from   TEXT NOT NULL,
    date_to     TEXT NOT NULL,
    page_size   INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
"""

# 键集分页：只取修订区间内可见、未删除、满足筛选且位于游标之后的一页。
PAGE_SQL = """
SELECT id, sample_code, project, test_date, value_json
FROM result_versions
WHERE valid_from <= :revision
  AND (valid_to IS NULL OR valid_to > :revision)
  AND deleted = 0
  AND project = :project
  AND test_date >= :date_from
  AND test_date <= :date_to
  AND (
      :cursor_date IS NULL
      OR test_date > :cursor_date
      OR (test_date = :cursor_date AND id > :cursor_id)
  )
ORDER BY test_date ASC, id ASC
LIMIT :limit
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _item_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "sample_code": row["sample_code"],
        "project": row["project"],
        "test_date": row["test_date"],
        "value": json.loads(row["value_json"]),
    }


class Database:
    """对 SQLite 数据库文件的薄封装；每次操作使用独立连接，进程间安全。"""

    def __init__(self, path: str, busy_timeout_ms: int = 30000):
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,  # 自动提交；事务用显式 BEGIN/COMMIT 控制
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        return conn

    # ------------------------------------------------------------------
    # 批次写入 / 删除
    # ------------------------------------------------------------------
    def apply_batch(self, operations: Sequence[Any]) -> dict[str, int]:
        """原子地应用一批 put/delete，返回新修订号与计数。

        operations 中的对象需有 ``op``、``id`` 属性；put 还需
        ``sample_code``、``project``、``test_date``、``value``。
        """
        puts = sum(1 for op in operations if op.op == "put")
        deletes = len(operations) - puts

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO revisions (created_at, puts, deletes) VALUES (?, ?, ?)",
                (_utcnow(), puts, deletes),
            )
            revision = cur.lastrowid
            for op in operations:
                if op.op == "put":
                    # 同 ID 写入替换：关闭旧版本（含墓碑），写入新版本。
                    conn.execute(
                        "UPDATE result_versions SET valid_to = ? "
                        "WHERE id = ? AND valid_to IS NULL",
                        (revision, op.id),
                    )
                    conn.execute(
                        "INSERT INTO result_versions (id, valid_from, valid_to, "
                        "sample_code, project, test_date, value_json, deleted) "
                        "VALUES (?, ?, NULL, ?, ?, ?, ?, 0)",
                        (
                            op.id,
                            revision,
                            op.sample_code,
                            op.project,
                            op.test_date,
                            json.dumps(op.value, ensure_ascii=False),
                        ),
                    )
                else:  # delete
                    cur = conn.execute(
                        "UPDATE result_versions SET valid_to = ? "
                        "WHERE id = ? AND valid_to IS NULL AND deleted = 0",
                        (revision, op.id),
                    )
                    if cur.rowcount:
                        # 墓碑版本：自本修订起该 ID 不可见。
                        conn.execute(
                            "INSERT INTO result_versions (id, valid_from, valid_to, deleted) "
                            "VALUES (?, ?, NULL, 1)",
                            (op.id, revision),
                        )
                    # 删除不存在/已删除的 ID 为幂等空操作，批次仍然成功。
            conn.execute("COMMIT")
            return {
                "revision": revision,
                "applied": len(operations),
                "puts": puts,
                "deletes": deletes,
            }
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 快照与翻页
    # ------------------------------------------------------------------
    def create_snapshot(
        self, project: str, date_from: str, date_to: str, page_size: int
    ) -> dict[str, Any]:
        """固定当前已提交修订，登记快照并返回其元数据。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            revision = conn.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM revisions"
            ).fetchone()[0]
            snapshot = {
                "snapshot_id": uuid.uuid4().hex,
                "project": project,
                "date_from": date_from,
                "date_to": date_to,
                "page_size": page_size,
                "revision": revision,
                "created_at": _utcnow(),
            }
            conn.execute(
                "INSERT INTO snapshots (snapshot_id, project, date_from, date_to, "
                "page_size, revision, created_at) "
                "VALUES (:snapshot_id, :project, :date_from, :date_to, "
                ":page_size, :revision, :created_at)",
                snapshot,
            )
            conn.execute("COMMIT")
            return snapshot
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_snapshot(self, snapshot_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT snapshot_id, project, date_from, date_to, page_size, "
                "revision, created_at FROM snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def fetch_page(
        self,
        snapshot: dict[str, Any],
        cursor_date: Optional[str],
        cursor_id: Optional[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        """按键集游标取一页；只依赖快照固定的修订号，结果确定且可重试。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                PAGE_SQL,
                {
                    "revision": snapshot["revision"],
                    "project": snapshot["project"],
                    "date_from": snapshot["date_from"],
                    "date_to": snapshot["date_to"],
                    "cursor_date": cursor_date,
                    "cursor_id": cursor_id,
                    "limit": limit,
                },
            ).fetchall()
            return [_item_from_row(row) for row in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    def list_revisions(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT revision, created_at, puts, deletes "
                "FROM revisions ORDER BY revision"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_current(self, result_id: str) -> Optional[dict[str, Any]]:
        """某 ID 在最新修订下的可见状态（不存在或已删除返回 None）。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, sample_code, project, test_date, value_json "
                "FROM result_versions "
                "WHERE id = ? AND valid_to IS NULL AND deleted = 0",
                (result_id,),
            ).fetchone()
            return _item_from_row(row) if row is not None else None
        finally:
            conn.close()
