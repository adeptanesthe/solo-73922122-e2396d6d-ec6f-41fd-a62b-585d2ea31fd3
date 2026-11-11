"""水样检测结果快照取数 API。

实验室数据员分页提取大批水样结果编制月报时使用：先开启快照固定修订，
再按游标逐页取数；翻页期间的更正、删除、新增都不会影响已固定的快照页。
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .db import Database
from .schemas import (
    BatchRequest,
    BatchResponse,
    Page,
    PageResponse,
    ResultItem,
    RevisionInfo,
    SnapshotCreated,
    SnapshotInfo,
    SnapshotRequest,
)

DESCRIPTION = """
## 用途

大批水样检测结果按月报口径分页提取。**先开快照、再翻页**：
快照在创建时固定当时已提交的修订号，之后任意时刻用游标翻页，
看到的都是该修订下的记录与值。

## 规则

* `POST /api/v1/batches`：每批 1～100 条「完整结果写入」或「按 ID 删除」，
  同 ID 写入替换；整批成功才产生一个递增修订号，任何一条不合法则整批不生效。
* `POST /api/v1/snapshots`：指定项目、日期闭区间、页大小（1～100），
  固定当前已提交修订，按 **检测日期、ID 升序** 返回第一页与后续游标。
* `GET /api/v1/snapshots/{id}/pages?cursor=...`：翻页。翻页保留该修订下的
  记录和值——包括后来被更正、被移出日期范围或被删除的记录；
  之后新增的记录不会出现。同一游标重试返回相同页。
* 修订历史与游标在服务重启后仍然可用；两个服务进程共享同一
  SQLite 数据库文件并发写入、开快照、翻页时上述规则同样成立。

## 跨页更正示例

1. 批次 1 写入 12 条 10 月 `COD` 结果（修订 1）。
2. 开快照（项目 `COD`，`2026-10-01`～`2026-10-31`，页大小 5）→ 固定修订 1，读第 1 页。
3. 批次 2（修订 2）：改第 1 页某条的值、删第 2 页某条、把第 3 页某条日期改到 11 月、新增 1 条。
4. 继续用旧游标读第 2、3 页：仍是原来的 12 条与原值——被改/被删/被移出的记录原样保留，新增记录不出现。
5. 重新开快照才能看到修订 2 的状态（11 条）。

仓库 `demo/cross_page_correction.py` 可直接演示上述流程。
"""

_cursor_prefix = "v1"


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return _cursor_prefix + "." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(token: str) -> dict[str, Any]:
    if not token.startswith(_cursor_prefix + "."):
        raise HTTPException(status_code=400, detail="无效游标")
    body = token[len(_cursor_prefix) + 1 :]
    try:
        padding = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + padding))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="无效游标") from None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("sid"), str)
        or not isinstance(payload.get("d"), str)
        or not isinstance(payload.get("i"), str)
        or not isinstance(payload.get("p"), int)
        or isinstance(payload.get("p"), bool)
        or payload["p"] < 2
    ):
        raise HTTPException(status_code=400, detail="无效游标")
    return payload


def _json_safe(obj: Any) -> Any:
    """把错误详情中无法严格 JSON 编码的非有限浮点替换为字符串。"""
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, dict):
        return {key: _json_safe(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(value) for value in obj]
    return obj


def create_app(db_path: Optional[str] = None) -> FastAPI:
    """应用工厂；db_path 缺省取环境变量 RESULTS_DB，再缺省 ./results.db。"""
    db = Database(db_path or os.environ.get("RESULTS_DB", "./results.db"))

    app = FastAPI(
        title="水样检测结果快照取数 API",
        description=DESCRIPTION,
        version="1.0.0",
    )
    app.state.db = db

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(_request: Any, exc: RequestValidationError):
        # 默认处理器在错误输入含 NaN/Infinity 时会因 JSON 编码失败而 500；
        # 这里先清洗再序列化，保证始终返回可读的 422。
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"detail": _json_safe(exc.errors())}),
        )

    def turn_page(
        snapshot: dict[str, Any],
        cursor_date: Optional[str],
        cursor_id: Optional[str],
        page_number: int,
    ) -> Page:
        page_size = snapshot["page_size"]
        rows = db.fetch_page(snapshot, cursor_date, cursor_id, page_size + 1)
        items = rows[:page_size]
        next_cursor: Optional[str] = None
        if len(rows) > page_size:  # 多取到一条，说明还有下一页
            last = items[-1]
            next_cursor = _encode_cursor(
                {
                    "sid": snapshot["snapshot_id"],
                    "d": last["test_date"],
                    "i": last["id"],
                    "p": page_number + 1,
                }
            )
        return Page(
            number=page_number,
            items=[ResultItem(**item) for item in items],
            next_cursor=next_cursor,
        )

    @app.get("/health", tags=["运维"], summary="健康检查")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/api/v1/batches",
        response_model=BatchResponse,
        tags=["批次写入"],
        summary="提交一批写入/删除（原子生效，产生一个递增修订号）",
    )
    def submit_batch(request: BatchRequest) -> dict[str, int]:
        return db.apply_batch(request.operations)

    @app.get(
        "/api/v1/revisions",
        response_model=list[RevisionInfo],
        tags=["批次写入"],
        summary="修订历史（重启后保留）",
    )
    def list_revisions() -> list[dict[str, Any]]:
        return db.list_revisions()

    @app.get(
        "/api/v1/results/{result_id}",
        response_model=ResultItem,
        tags=["批次写入"],
        summary="查看某 ID 在最新修订下的状态",
    )
    def get_current(result_id: str = Path(description="结果唯一 ID")) -> dict[str, Any]:
        item = db.get_current(result_id)
        if item is None:
            raise HTTPException(status_code=404, detail="结果不存在或已删除")
        return item

    @app.post(
        "/api/v1/snapshots",
        response_model=SnapshotCreated,
        tags=["快照取数"],
        summary="开启快照取数：固定当前修订并返回第一页",
    )
    def open_snapshot(request: SnapshotRequest) -> SnapshotCreated:
        snapshot = db.create_snapshot(
            request.project, request.date_from, request.date_to, request.page_size
        )
        page = turn_page(snapshot, None, None, 1)
        return SnapshotCreated(**snapshot, page=page)

    @app.get(
        "/api/v1/snapshots/{snapshot_id}",
        response_model=SnapshotInfo,
        tags=["快照取数"],
        summary="快照元数据",
    )
    def get_snapshot(snapshot_id: str) -> dict[str, Any]:
        snapshot = db.get_snapshot(snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="快照不存在")
        return snapshot

    @app.get(
        "/api/v1/snapshots/{snapshot_id}/pages",
        response_model=PageResponse,
        tags=["快照取数"],
        summary="按游标翻页（同一游标重试返回相同页）",
    )
    def get_page(
        snapshot_id: str,
        cursor: str = Query(description="上一页返回的 next_cursor"),
    ) -> PageResponse:
        snapshot = db.get_snapshot(snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="快照不存在")
        cur = _decode_cursor(cursor)
        if cur["sid"] != snapshot_id:
            raise HTTPException(status_code=400, detail="游标与快照不匹配")
        page = turn_page(snapshot, cur["d"], cur["i"], cur["p"])
        return PageResponse(
            snapshot_id=snapshot_id, revision=snapshot["revision"], page=page
        )

    return app


app = create_app()
