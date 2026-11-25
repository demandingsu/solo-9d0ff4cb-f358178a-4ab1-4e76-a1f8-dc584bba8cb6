"""请求/响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


# ---------------------------------------------------------------------------
# 班次报送
# ---------------------------------------------------------------------------
# 为兼容不同调用习惯，入参在 before 校验阶段把常见别名字段折叠到规范字段。
_SHIFT_ALIASES = {
    "shift_id": ("shift_no", "shift_number", "shift_code", "shiftId", "shiftNo", "id"),
    "revision": (
        "revision_no",
        "revision_number",
        "revisionNo",
        "rev_no",
        "revNo",
        "rev",
        "version",
    ),
    "work_order": ("work_order_no", "workOrder", "workOrderNo", "wo"),
    "start_at": (
        "start_time",
        "started_at",
        "start_datetime",
        "begin_at",
        "begin_time",
        "begin",
        "start",
        "startTime",
        "startAt",
    ),
    "end_at": (
        "end_time",
        "ended_at",
        "end_datetime",
        "finish_at",
        "finish_time",
        "finish",
        "end",
        "endTime",
        "endAt",
    ),
}


class ShiftReportIn(BaseModel):
    """班次报送入参。起止时刻为北京时间（带时区则换算到北京时间），精确到分钟。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "shift_id": "YB-1001",
                "revision": 1,
                "work_order": "WO-7788",
                "start_at": "2026-10-31T22:00",
                "end_at": "2026-11-01T06:00",
            }
        }
    )

    shift_id: int | str | None = Field(default=None, description="班次号（数字或字符串）")
    revision: int = Field(ge=1, description="正整数修订号；同班次必须单调更新")
    work_order: str = Field(description="工单号")
    start_at: Any = Field(description="开始时刻，北京时间，精确到分钟，如 2026-10-31T22:00")
    end_at: Any = Field(description="结束时刻，北京时间，精确到分钟；与开始间隔最长 24 小时")

    @model_validator(mode="before")
    @classmethod
    def _fold_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        folded = dict(data)
        for canonical, aliases in _SHIFT_ALIASES.items():
            if folded.get(canonical) is None:
                for alias in aliases:
                    if folded.get(alias) is not None:
                        folded[canonical] = folded[alias]
                        break
        # 兼容嵌套时间区间：{"time_range": {"start": ..., "end": ...}}
        if folded.get("start_at") is None or folded.get("end_at") is None:
            for key in ("time_range", "timerange", "period", "range", "time_window"):
                nested = folded.get(key)
                if isinstance(nested, dict):
                    if folded.get("start_at") is None:
                        folded["start_at"] = (
                            nested.get("start") or nested.get("start_at") or nested.get("begin")
                        )
                    if folded.get("end_at") is None:
                        folded["end_at"] = (
                            nested.get("end") or nested.get("end_at") or nested.get("finish")
                        )
                    break
                if isinstance(nested, (list, tuple)) and len(nested) == 2:
                    folded.setdefault("start_at", nested[0])
                    folded.setdefault("end_at", nested[1])
                    break
        return folded

    @field_validator("shift_id")
    @classmethod
    def _shift_id_valid(cls, v):
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValueError("班次号不能是布尔值")
        if isinstance(v, str) and not v.strip():
            raise ValueError("班次号不能为空字符串")
        return v.strip() if isinstance(v, str) else v

    _v_wo = field_validator("work_order")(_non_blank)


class ShiftMonthLine(BaseModel):
    month: str = Field(description="月份 YYYY-MM（北京时间月界）")
    row_id: int = Field(description="该班次在该月固定使用的工时行号")
    work_row_id: int = Field(description="同 row_id，便于直接对接供数/纠错接口")
    minutes: int = Field(description="拆分到该月的分钟数（>0）")


class ShiftReceiptResponse(BaseModel):
    """班次报送回执（实际响应还带 shift_no/rows 等兼容别名）。"""

    shift_id: int | str
    revision: int
    work_order: str
    start_at: str = Field(description="规范化后的北京时间开始时刻")
    end_at: str = Field(description="规范化后的北京时间结束时刻")
    total_minutes: int
    months: list[ShiftMonthLine]
    rows: list[ShiftMonthLine] = Field(description="同 months")
