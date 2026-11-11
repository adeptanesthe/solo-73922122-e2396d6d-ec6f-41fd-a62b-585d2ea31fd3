"""请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 值：数字或字符串（拒绝布尔、null、非有限浮点）。
Value = Union[int, float, str]


def _validate_date(value: str) -> str:
    if not DATE_PATTERN.match(value):
        raise ValueError("日期必须为 YYYY-MM-DD 格式")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"不存在的日期: {value}")
    return value


def _validate_value(value: object) -> Value:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("值必须为数字或字符串")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("值必须为有限数字")
    return value  # type: ignore[return-value]


class PutOp(BaseModel):
    """写入一条完整结果；同 ID 已存在时替换。"""

    model_config = ConfigDict(extra="forbid")

    op: Literal["put"] = "put"
    id: str = Field(min_length=1, max_length=128, description="结果唯一 ID")
    sample_code: str = Field(min_length=1, max_length=128, description="样品码")
    project: str = Field(min_length=1, max_length=128, description="检测项目")
    test_date: str = Field(description="检测日期 YYYY-MM-DD")
    value: Value = Field(description="检测值：数字或字符串")

    @field_validator("test_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        return _validate_date(v)

    @field_validator("value", mode="before")
    @classmethod
    def _check_value(cls, v: object) -> Value:
        return _validate_value(v)


class DeleteOp(BaseModel):
    """按 ID 删除；删除不存在的 ID 为幂等空操作。"""

    model_config = ConfigDict(extra="forbid")

    op: Literal["delete"]
    id: str = Field(min_length=1, max_length=128, description="要删除的结果 ID")


Operation = Annotated[Union[PutOp, DeleteOp], Field(discriminator="op")]


class BatchRequest(BaseModel):
    """一批写入/删除：1～100 条，整体原子生效。"""

    operations: List[Operation] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _unique_ids(self) -> "BatchRequest":
        ids = [op.id for op in self.operations]
        if len(ids) != len(set(ids)):
            raise ValueError("同一批次内 ID 不能重复")
        return self


class BatchResponse(BaseModel):
    revision: int = Field(description="本批次产生的递增修订号")
    applied: int = Field(description="生效的操作条数")
    puts: int
    deletes: int


class RevisionInfo(BaseModel):
    revision: int
    created_at: str
    puts: int
    deletes: int


class SnapshotRequest(BaseModel):
    """首次取数：项目 + 日期闭区间 + 页大小。"""

    project: str = Field(min_length=1, max_length=128, description="检测项目")
    date_from: str = Field(description="起始日期 YYYY-MM-DD（含）")
    date_to: str = Field(description="截止日期 YYYY-MM-DD（含）")
    page_size: int = Field(ge=1, le=100, description="页大小 1～100")

    @field_validator("date_from", "date_to")
    @classmethod
    def _check_dates(cls, v: str) -> str:
        return _validate_date(v)

    @model_validator(mode="after")
    def _range_ok(self) -> "SnapshotRequest":
        if self.date_from > self.date_to:
            raise ValueError("date_from 不能晚于 date_to（闭区间）")
        return self


class ResultItem(BaseModel):
    id: str
    sample_code: str
    project: str
    test_date: str
    value: Value


class Page(BaseModel):
    number: int = Field(description="页码，从 1 开始")
    items: List[ResultItem]
    next_cursor: Optional[str] = Field(
        description="下一页游标；为 null 表示已是最后一页"
    )


class SnapshotInfo(BaseModel):
    snapshot_id: str
    project: str
    date_from: str
    date_to: str
    page_size: int
    revision: int = Field(description="快照固定的修订号")
    created_at: str


class SnapshotCreated(SnapshotInfo):
    page: Page = Field(description="第一页")


class PageResponse(BaseModel):
    snapshot_id: str
    revision: int
    page: Page
