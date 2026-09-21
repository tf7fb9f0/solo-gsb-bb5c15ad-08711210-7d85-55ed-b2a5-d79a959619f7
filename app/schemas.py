"""请求 / 响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class PlanRequest(BaseModel):
    """按顺序提交的谱面数据与约束。"""

    system_heights: list[float] = Field(
        ..., min_length=1, description="按演奏顺序排列的各谱面系统高度"
    )
    boundary_rests: list[float] = Field(
        default_factory=list,
        description="相邻系统之间边界的休止秒数，长度 = 系统数 - 1",
    )
    page_height: float = Field(..., gt=0, allow_inf_nan=False, description="页面可用高度")
    min_safe_rest: float = Field(..., ge=0, allow_inf_nan=False, description="最低安全休止秒数")
    layout_mode: Literal["single", "spread"] = Field(
        default="single",
        description="排版模式：single 单页逐页翻（默认）；spread 双页谱架，相邻左右页同组可见",
    )
    spread_start_side: Literal["left", "right"] = Field(
        default="left",
        description="spread 模式下第 1 页所在的一侧（left/right，默认 left）；single 模式下忽略",
    )
    forced_break_after: list[int] = Field(
        default_factory=list,
        description="人工强制分页点（系统下标数组，默认空）：对应系统之后必须分页",
    )
    forbidden_break_after: list[int] = Field(
        default_factory=list,
        description="人工禁止分页点（系统下标数组，默认空）：对应系统之后不得分页",
    )
    boundary_times: Optional[list[float]] = Field(
        default=None,
        description=(
            "各相邻系统边界的演奏秒数（时间轴），长度 = 系统数 - 1，严格递增、非负。"
            "启用实体翻页恢复约束（min_turn_gap_seconds > 0）时必须提供；"
            "默认 null 表示不启用恢复约束"
        ),
    )
    min_turn_gap_seconds: float = Field(
        default=0.0,
        ge=0,
        allow_inf_nan=False,
        description=(
            "实体翻页最小恢复间隔（秒，默认 0 即不启用）。启用后任意两次相邻"
            "实体翻页的演奏时间差必须不小于该值：single 每次翻页计入，"
            "spread 仅右页之后的翻页计入，组内换页不消耗恢复时间"
        ),
    )

    @model_validator(mode="after")
    def _check_values(self) -> "PlanRequest":
        for h in self.system_heights:
            if h != h or h in (float("inf"), float("-inf")) or h <= 0:
                raise ValueError("system_heights 必须全部为正的有限数值")
        for r in self.boundary_rests:
            if r != r or r in (float("inf"), float("-inf")) or r < 0:
                raise ValueError("boundary_rests 必须全部为非负的有限数值")
        if len(self.boundary_rests) != len(self.system_heights) - 1:
            raise ValueError(
                "boundary_rests 的长度必须等于 len(system_heights) - 1"
            )
        if self.min_turn_gap_seconds > 0 and self.boundary_times is None:
            raise ValueError(
                "启用实体翻页恢复约束（min_turn_gap_seconds > 0）时必须提供 boundary_times"
            )
        if self.boundary_times is not None:
            times = self.boundary_times
            if len(times) != len(self.system_heights) - 1:
                raise ValueError(
                    "boundary_times 的长度必须等于 len(system_heights) - 1"
                )
            for t in times:
                if t != t or t in (float("inf"), float("-inf")) or t < 0:
                    raise ValueError("boundary_times 必须全部为非负的有限数值")
            if any(times[k + 1] <= times[k] for k in range(len(times) - 1)):
                raise ValueError("boundary_times 必须严格递增")
        return self

    @model_validator(mode="after")
    def _check_break_constraints(self) -> "PlanRequest":
        n = len(self.system_heights)
        for field_name, values in (
            ("forced_break_after", self.forced_break_after),
            ("forbidden_break_after", self.forbidden_break_after),
        ):
            for idx in values:
                if idx < 0 or idx >= n:
                    raise ValueError(
                        f"{field_name} 包含越界下标 {idx}（系统下标范围为 0..{n - 1}）"
                    )
                if idx == n - 1:
                    raise ValueError(
                        f"{field_name} 指向末系统 {idx}：末系统之后无需分页，不允许设置断点"
                    )
        conflict = sorted(set(self.forced_break_after) & set(self.forbidden_break_after))
        if conflict:
            raise ValueError(
                f"forced_break_after 与 forbidden_break_after 存在冲突下标: {conflict}"
            )
        return self


class PageOut(BaseModel):
    page: int
    start_system: int
    end_system: int
    system_count: int
    used_height: float
    slack: float
    turn_rest_after: Optional[float] = None
    # 实体翻页处的演奏秒数（来自 boundary_times）；未启用恢复约束、最后一页、
    # spread 组内边界为 null
    turn_time_after: Optional[float] = None
    side: Optional[str] = None  # spread 模式为 "left"/"right"；single 模式为 null
    physical_turn_after: Optional[bool] = None  # 该页之后是否需要实体翻页


class PlanOut(BaseModel):
    id: int
    created_at: str
    system_heights: list[float]
    boundary_rests: list[float]
    page_height: float
    min_safe_rest: float
    layout_mode: str = "single"
    spread_start_side: Optional[str] = None
    forced_break_after: list[int] = Field(default_factory=list)
    forbidden_break_after: list[int] = Field(default_factory=list)
    # 未启用恢复约束 / 旧记录回放时为 null 与 0.0
    boundary_times: Optional[list[float]] = None
    min_turn_gap_seconds: float = 0.0
    system_count: int
    page_count: int
    min_turn_rest: Optional[float] = None
    total_slack_squared: float
    pages: list[PageOut]


class PlanSummary(BaseModel):
    id: int
    created_at: str
    system_count: int
    page_count: int
    min_turn_rest: Optional[float] = None
    total_slack_squared: float
    layout_mode: str = "single"
