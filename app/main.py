"""FastAPI 应用入口：乐谱安全翻页规划 API。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from .planner import InfeasibleError, plan_pages
from .schemas import PlanOut, PlanRequest, PlanSummary
from .storage import PlanStore

DATABASE_PATH = os.getenv("DATABASE_PATH", "plans.db")

store = PlanStore(DATABASE_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时完成建库建表
    store.init()
    yield


app = FastAPI(
    title="乐谱安全翻页规划 API",
    version="1.3.0",
    description=(
        "提交按顺序排列的谱面系统高度、边界休止秒数、页面可用高度与最低安全休止，"
        "动态规划生成完整分页方案：页面不超高、仅在达标边界翻页，"
        "依次最大化最短翻页休止、最小化各页留白平方和、减少页数。"
        "支持 single 单页模式与 spread 双页谱架模式（相邻左右页同组可见，"
        "组内边界不检查休止，仅翻过右页的实体翻页要求达标休止）。"
        "可用 forced_break_after / forbidden_break_after 指定人工强制 / 禁止分页点。"
        "提供 boundary_times（边界演奏秒数）与 min_turn_gap_seconds 可启用"
        "实体翻页恢复约束：相邻两次实体翻页（spread 仅右页后的翻页）至少间隔指定秒数。"
    ),
    lifespan=lifespan,
)


def _to_plan_out(record: dict) -> PlanOut:
    req, res = record["request"], record["result"]
    return PlanOut(
        id=record["id"],
        created_at=record["created_at"],
        system_heights=req["system_heights"],
        boundary_rests=req["boundary_rests"],
        page_height=req["page_height"],
        min_safe_rest=req["min_safe_rest"],
        # 旧记录没有新增字段，按 single 模式默认值回放
        layout_mode=req.get("layout_mode", "single"),
        spread_start_side=req.get("spread_start_side"),
        # 旧记录没有人工断点字段，按空数组回放
        forced_break_after=req.get("forced_break_after", []),
        forbidden_break_after=req.get("forbidden_break_after", []),
        # 旧记录没有恢复约束字段，按未启用回放
        boundary_times=req.get("boundary_times"),
        min_turn_gap_seconds=req.get("min_turn_gap_seconds", 0.0),
        system_count=len(req["system_heights"]),
        page_count=res["page_count"],
        min_turn_rest=res["min_turn_rest"],
        total_slack_squared=res["total_slack_squared"],
        pages=res["pages"],
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/plans", status_code=201, response_model=PlanOut)
def create_plan(req: PlanRequest) -> PlanOut:
    """生成并保存完整分页方案；无解时返回 422 及首个问题区间，不保存任何部分方案。"""
    try:
        result = plan_pages(
            req.system_heights,
            req.boundary_rests,
            req.page_height,
            req.min_safe_rest,
            layout_mode=req.layout_mode,
            spread_start_side=req.spread_start_side,
            forced_break_after=req.forced_break_after,
            forbidden_break_after=req.forbidden_break_after,
            boundary_times=req.boundary_times,
            min_turn_gap_seconds=req.min_turn_gap_seconds,
        )
    except InfeasibleError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "no_feasible_pagination", "reason": exc.reason, **exc.detail},
        )
    plan_id = store.insert(req.model_dump(), result)
    return _to_plan_out(store.get(plan_id))


@app.get("/plans", response_model=list[PlanSummary])
def list_plans(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[PlanSummary]:
    """列出已保存的方案摘要。"""
    return [
        PlanSummary(
            id=r["id"],
            created_at=r["created_at"],
            system_count=len(r["request"]["system_heights"]),
            page_count=r["result"]["page_count"],
            min_turn_rest=r["result"]["min_turn_rest"],
            total_slack_squared=r["result"]["total_slack_squared"],
            layout_mode=r["request"].get("layout_mode", "single"),
        )
        for r in store.list(limit=limit, offset=offset)
    ]


@app.get("/plans/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: int) -> PlanOut:
    """按 ID 查询完整方案。"""
    record = store.get(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"plan {plan_id} not found")
    return _to_plan_out(record)
