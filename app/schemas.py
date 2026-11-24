"""请求/响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _non_blank(value: str) -> str:
    if value is None or value.strip() == "":
        raise ValueError("字符串字段不能为空")
    return value.strip()


class WorkHourIn(BaseModel):
    row_id: int = Field(ge=1, description="工时表行号，同月唯一")
    work_order: str = Field(description="工单号")
    minutes: int = Field(ge=0, description="工时（分钟）")

    _v = field_validator("work_order")(_non_blank)


class DispatchIn(BaseModel):
    row_id: int = Field(ge=1, description="分派表行号，同月唯一")
    work_order: str = Field(description="工单号")
    project: str = Field(description="项目号/项目标识")

    _v_wo = field_validator("work_order")(_non_blank)
    _v_pr = field_validator("project")(_non_blank)


class AttributionIn(BaseModel):
    row_id: int = Field(ge=1, description="归科表行号，同月唯一")
    project: str = Field(description="项目号/项目标识")
    budget_subject: str = Field(description="预算科目")

    _v_pr = field_validator("project")(_non_blank)
    _v_bs = field_validator("budget_subject")(_non_blank)


class BulkWorkHour(BaseModel):
    rows: list[WorkHourIn] = Field(min_length=1)
    replace: bool = Field(default=True, description="true=整月覆盖写入，false=追加（行号冲突即失败）")


class BulkDispatch(BaseModel):
    rows: list[DispatchIn] = Field(min_length=1)
    replace: bool = True


class BulkAttribution(BaseModel):
    rows: list[AttributionIn] = Field(min_length=1)
    replace: bool = True


class CorrectRequest(BaseModel):
    work_row_id: int = Field(ge=1, description="要纠正的工时行号")
    remove: Literal["dispatch", "attribution"] = Field(description="要撤除的记录类型")
    remove_row_id: int = Field(ge=1, description="要撤除的分派/归科记录行号")


class SuppliableItem(BaseModel):
    work_row_id: int
    work_order: str
    minutes: int
    budget_subject: str
    path_count: int = Field(description="完整路径数，可供数时恒为 1")


class WithheldItem(BaseModel):
    work_row_id: int
    work_order: str
    minutes: int
    path_count: int = Field(description="完整路径数：0=无路径，>=2=重复")
    reason: Literal["no_path", "duplicate"]


class SupplyResponse(BaseModel):
    month: str
    suppliable: list[SuppliableItem]
    withheld: list[WithheldItem]


class CorrectResponse(BaseModel):
    month: str
    work_row_id: int
    removed: dict
    restored_work_row_ids: list[int] = Field(description="本次撤除后由扣留恢复为可供数的工时行号")
    suppliable: list[SuppliableItem]
    withheld: list[WithheldItem]


class ErrorResponse(BaseModel):
    detail: str
