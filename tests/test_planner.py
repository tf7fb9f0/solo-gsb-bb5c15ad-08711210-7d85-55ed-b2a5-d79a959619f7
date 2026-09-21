"""规划核心与 API 的测试。"""
from __future__ import annotations

import pytest

from app.planner import InfeasibleError, plan_pages


def test_single_page_when_everything_fits():
    res = plan_pages([100.0, 200.0, 150.0], [0.0, 0.0], 1000.0, 2.0)
    assert res["page_count"] == 1
    assert res["min_turn_rest"] is None  # 无需翻页
    assert res["pages"][0]["start_system"] == 0
    assert res["pages"][0]["end_system"] == 2
    assert res["pages"][0]["turn_rest_after"] is None
    assert res["total_slack_squared"] == pytest.approx((1000.0 - 450.0) ** 2)


def test_turn_only_at_safe_boundary():
    # 边界 0 休止不足，只能在边界 1 翻页
    res = plan_pages([5.0, 5.0, 5.0], [0.5, 5.0], 10.0, 1.0)
    assert res["page_count"] == 2
    assert (res["pages"][0]["start_system"], res["pages"][0]["end_system"]) == (0, 1)
    assert (res["pages"][1]["start_system"], res["pages"][1]["end_system"]) == (2, 2)
    assert res["pages"][0]["turn_rest_after"] == 5.0
    assert res["min_turn_rest"] == 5.0


def test_bottleneck_is_maximized():
    # 两种可行切分：[0,1]|[2]（最短休止 3）与 [0]|[1,2]（最短休止 5）→ 选后者
    res = plan_pages([5.0, 5.0, 5.0], [5.0, 3.0], 10.0, 1.0)
    assert res["min_turn_rest"] == 5.0
    assert res["pages"][0]["end_system"] == 0


def test_bottleneck_beats_slack_and_page_count():
    # 方案 A：[6]|[4,6]|[4] 最短休止 9，留白平方和 52，3 页
    # 方案 B：[6,4]|[6,4] 最短休止 4，留白平方和 0，2 页
    # 第一目标优先 → 选 A
    res = plan_pages([6.0, 4.0, 6.0, 4.0], [9.0, 4.0, 9.0], 10.0, 1.0)
    assert res["min_turn_rest"] == 9.0
    assert res["page_count"] == 3
    assert res["total_slack_squared"] == pytest.approx(52.0)


def test_slack_squares_minimized_within_bottleneck():
    # 所有边界休止均为 9，瓶颈不约束切分；[5,5]|[6,4] 留白平方和 1+1=2 最小
    res = plan_pages([5.0, 5.0, 6.0, 4.0], [9.0, 9.0, 9.0], 11.0, 1.0)
    assert res["min_turn_rest"] == 9.0
    assert res["page_count"] == 2
    assert res["total_slack_squared"] == pytest.approx(2.0)
    assert (res["pages"][0]["start_system"], res["pages"][0]["end_system"]) == (0, 1)


def test_page_count_minimized_last():
    # 同样最短休止 5、同样留白平方和时，页数更少者胜：
    # [5,5]|[5,5]（2 页，留白 0）vs [5]|[5,5]|[5]（3 页，留白 25+0+25）
    res = plan_pages([5.0, 5.0, 5.0, 5.0], [5.0, 5.0, 5.0], 10.0, 1.0)
    assert res["page_count"] == 2
    assert res["total_slack_squared"] == pytest.approx(0.0)


def test_slack_tiebreak_picks_balanced_pages():
    # 最短休止均为 5 时，[6,6]|[6,6]（留白平方和 32）优于 [6,6,6]|[6]（超高不可行）
    # 与 [6]|[6,6,6]（不可行），再对比 [6,6,6]… 均超高，故唯一可行均分。
    res = plan_pages([6.0, 6.0, 6.0, 6.0], [5.0, 5.0, 5.0], 13.0, 1.0)
    assert res["page_count"] == 2
    assert res["total_slack_squared"] == pytest.approx(2 * (13.0 - 12.0) ** 2)


def test_first_oversized_system_reported():
    with pytest.raises(InfeasibleError) as exc:
        plan_pages([5.0, 12.0, 20.0], [3.0, 3.0], 10.0, 1.0)
    assert exc.value.reason == "system_exceeds_page_height"
    assert exc.value.detail["system_index"] == 1  # 首个无法容纳的系统


def test_first_unsafe_boundary_reported():
    # 系统 1、2 无法同页（12 > 10），必须在边界 1 断开，但休止 0.2 < 1.0
    with pytest.raises(InfeasibleError) as exc:
        plan_pages([6.0, 6.0, 6.0], [8.0, 0.2], 10.0, 1.0)
    assert exc.value.reason == "unsafe_boundary"
    assert exc.value.detail["boundary_index"] == 1
    assert exc.value.detail["between_systems"] == [1, 2]
    assert exc.value.detail["rest_seconds"] == 0.2


def test_later_safe_boundary_does_not_hide_earlier_failure():
    # 边界 0 休止不足且系统 0、1 无法同页 → 首个失败在边界 0
    with pytest.raises(InfeasibleError) as exc:
        plan_pages([6.0, 6.0, 6.0], [0.2, 8.0], 10.0, 1.0)
    assert exc.value.reason == "unsafe_boundary"
    assert exc.value.detail["boundary_index"] == 0


def test_single_system_no_boundaries():
    res = plan_pages([300.0], [], 1000.0, 2.0)
    assert res["page_count"] == 1
    assert res["min_turn_rest"] is None


def test_exact_height_fit_allowed():
    res = plan_pages([10.0, 10.0], [2.0], 10.0, 2.0)
    assert res["page_count"] == 2
    assert res["min_turn_rest"] == 2.0
    assert res["total_slack_squared"] == pytest.approx(0.0)


def test_zero_min_safe_rest_allows_any_boundary():
    res = plan_pages([6.0, 6.0], [0.0], 10.0, 0.0)
    assert res["page_count"] == 2
    assert res["min_turn_rest"] == 0.0


# ---- spread 双页谱架模式 ----


def test_single_mode_page_annotations():
    # single 模式保持兼容：不分左右页，每次翻页都是实体翻页
    res = plan_pages([5.0, 5.0, 5.0], [5.0, 5.0], 10.0, 1.0)
    assert [p["side"] for p in res["pages"]] == [None, None]
    assert [p["physical_turn_after"] for p in res["pages"]] == [True, False]


def test_spread_within_group_boundary_needs_no_rest():
    # 组内（左页→右页）边界休止为 0 也不检查；整曲一个组即可放下时没有任何实体翻页
    res = plan_pages([6.0, 6.0], [0.0], 10.0, 5.0, layout_mode="spread")
    assert res["page_count"] == 2
    assert [p["side"] for p in res["pages"]] == ["left", "right"]
    assert [p["physical_turn_after"] for p in res["pages"]] == [False, False]
    assert res["pages"][0]["turn_rest_after"] is None
    assert res["min_turn_rest"] is None


def test_spread_succeeds_where_single_fails():
    # 同一数据 single 无解（边界 0 休止不足），spread 下边界 0 在组内不检查
    kwargs = dict(
        system_heights=[6.0, 6.0, 6.0],
        boundary_rests=[0.2, 8.0],
        page_height=10.0,
        min_safe_rest=1.0,
    )
    with pytest.raises(InfeasibleError):
        plan_pages(**kwargs)
    res = plan_pages(**kwargs, layout_mode="spread")
    assert [p["side"] for p in res["pages"]] == ["left", "right", "left"]
    assert [p["physical_turn_after"] for p in res["pages"]] == [False, True, False]
    assert res["min_turn_rest"] == 8.0
    assert res["pages"][1]["turn_rest_after"] == 8.0


def test_spread_bottleneck_only_counts_physical_turns():
    # 每页只能容纳一个系统：组内边界休止 0.1 不参与瓶颈，
    # 实体翻页休止为 4.0 与 9.0 → 瓶颈 4.0
    res = plan_pages(
        [6.0, 6.0, 6.0, 6.0, 6.0],
        [0.1, 4.0, 0.1, 9.0],
        10.0,
        1.0,
        layout_mode="spread",
    )
    assert res["page_count"] == 5
    assert res["min_turn_rest"] == 4.0
    assert [p["physical_turn_after"] for p in res["pages"]] == [False, True, False, True, False]


def test_spread_start_side_right_turns_after_odd_pages():
    # 起始页在右侧：第 1 页即右页，页 1→2 边界就需要实体翻页
    res = plan_pages(
        [6.0, 6.0, 6.0],
        [5.0, 0.1],
        10.0,
        1.0,
        layout_mode="spread",
        spread_start_side="right",
    )
    assert [p["side"] for p in res["pages"]] == ["right", "left", "right"]
    assert [p["physical_turn_after"] for p in res["pages"]] == [True, False, False]
    assert res["min_turn_rest"] == 5.0


def test_spread_start_side_changes_feasibility():
    # 同一输入下起始侧决定实体翻页落在哪个边界（页码奇偶进入状态）
    kwargs = dict(
        system_heights=[6.0, 6.0, 6.0],
        boundary_rests=[0.2, 8.0],
        page_height=10.0,
        min_safe_rest=1.0,
    )
    res = plan_pages(**kwargs, layout_mode="spread", spread_start_side="left")
    assert res["min_turn_rest"] == 8.0  # 实体翻页在边界 1
    with pytest.raises(InfeasibleError) as exc:
        plan_pages(**kwargs, layout_mode="spread", spread_start_side="right")
    assert exc.value.detail["boundary_index"] == 0  # 实体翻页在边界 0，休止不足


def test_spread_slack_minimized_within_single_group():
    # 全曲可放入一个组（两页），无实体翻页；留白平方和最小化切为 [5,5]|[6,4]
    res = plan_pages([5.0, 5.0, 6.0, 4.0], [0.0, 0.0, 0.0], 11.0, 1.0, layout_mode="spread")
    assert res["page_count"] == 2
    assert res["min_turn_rest"] is None
    assert res["total_slack_squared"] == pytest.approx(2.0)
    assert (res["pages"][0]["start_system"], res["pages"][0]["end_system"]) == (0, 1)


def test_spread_page_count_tiebreak():
    # 无实体翻页的 2 页方案（瓶颈视为无穷大）优于任何需要实体翻页的多页方案
    res = plan_pages([5.0, 5.0, 5.0, 5.0], [5.0, 5.0, 5.0], 10.0, 1.0, layout_mode="spread")
    assert res["page_count"] == 2
    assert res["total_slack_squared"] == pytest.approx(0.0)


def test_spread_infeasible_reports_first_physical_turn_boundary():
    # 页 2 是右页，其后的实体翻页边界 1 休止不足
    with pytest.raises(InfeasibleError) as exc:
        plan_pages([6.0, 6.0, 6.0], [8.0, 0.2], 10.0, 1.0, layout_mode="spread")
    assert exc.value.reason == "unsafe_boundary"
    assert exc.value.detail["boundary_index"] == 1
    assert exc.value.detail["between_systems"] == [1, 2]
    assert exc.value.detail["physical_turn"] is True


def test_spread_diagnosis_skips_safe_physical_turns():
    # 第一个实体翻页（边界 1，休止 8）安全，第二个（边界 3，休止 0.2）才是首个失败点
    with pytest.raises(InfeasibleError) as exc:
        plan_pages(
            [6.0, 6.0, 6.0, 6.0, 6.0],
            [0.0, 8.0, 0.0, 0.2],
            10.0,
            1.0,
            layout_mode="spread",
        )
    assert exc.value.detail["boundary_index"] == 3
    assert exc.value.detail["rest_seconds"] == 0.2


def test_spread_oversized_system_still_reported_first():
    with pytest.raises(InfeasibleError) as exc:
        plan_pages([5.0, 12.0], [9.0], 10.0, 1.0, layout_mode="spread")
    assert exc.value.reason == "system_exceeds_page_height"
    assert exc.value.detail["system_index"] == 1


def test_spread_single_system():
    res = plan_pages([300.0], [], 1000.0, 2.0, layout_mode="spread", spread_start_side="right")
    assert res["page_count"] == 1
    assert res["pages"][0]["side"] == "right"
    assert res["pages"][0]["physical_turn_after"] is False
    assert res["min_turn_rest"] is None


# ---- 人工断点约束（forced_break_after / forbidden_break_after）----


def test_forced_break_after_splits_pages():
    # 无约束时整曲一页即可放下；强制在系统 1 后分页 → [0,1]|[2,3]
    res = plan_pages([5.0, 5.0, 5.0, 5.0], [9.0, 9.0, 9.0], 20.0, 1.0,
                     forced_break_after=[1])
    assert [(p["start_system"], p["end_system"]) for p in res["pages"]] == [(0, 1), (2, 3)]
    assert res["pages"][0]["turn_rest_after"] == 9.0


def test_forced_break_participates_in_bottleneck_single():
    # single 模式强制断点处休止 3.0 成为全曲瓶颈（无约束时瓶颈为 9.0）
    res = plan_pages([5.0, 5.0, 5.0], [3.0, 9.0], 10.0, 1.0, forced_break_after=[0])
    assert res["min_turn_rest"] == 3.0
    assert (res["pages"][0]["start_system"], res["pages"][0]["end_system"]) == (0, 0)


def test_forced_break_after_requires_safe_rest_single():
    # single 模式强制断点处休止不足 → 无解，诊断定位该边界并标注 forced_break
    with pytest.raises(InfeasibleError) as exc:
        plan_pages([5.0, 5.0, 5.0], [0.5, 9.0], 10.0, 1.0, forced_break_after=[0])
    assert exc.value.reason == "unsafe_boundary"
    assert exc.value.detail["boundary_index"] == 0
    assert exc.value.detail["forced_break"] is True


def test_forbidden_break_after_merges_pages():
    # 禁止在系统 0 后分页 → 系统 0、1 必须同页
    res = plan_pages([5.0, 5.0, 5.0], [9.0, 9.0], 10.0, 1.0,
                     forbidden_break_after=[0])
    assert [(p["start_system"], p["end_system"]) for p in res["pages"]] == [(0, 1), (2, 2)]


def test_forbidden_break_after_infeasible_reported():
    # 禁止在系统 0 后分页，但系统 0、1 无法同页 → 无解，诊断标注 forbidden_break
    with pytest.raises(InfeasibleError) as exc:
        plan_pages([6.0, 6.0, 6.0], [9.0, 9.0], 10.0, 1.0,
                   forbidden_break_after=[0])
    assert exc.value.reason == "unsafe_boundary"
    assert exc.value.detail["boundary_index"] == 0
    assert exc.value.detail["forbidden_break"] is True


def test_break_constraints_conflict_rejected():
    with pytest.raises(ValueError, match="冲突"):
        plan_pages([5.0, 5.0], [9.0], 10.0, 1.0,
                   forced_break_after=[0], forbidden_break_after=[0])


def test_break_constraint_on_last_system_rejected():
    with pytest.raises(ValueError):
        plan_pages([5.0, 5.0], [9.0], 10.0, 1.0, forced_break_after=[1])
    with pytest.raises(ValueError):
        plan_pages([5.0, 5.0], [9.0], 10.0, 1.0, forbidden_break_after=[1])
    with pytest.raises(ValueError):
        plan_pages([5.0, 5.0], [9.0], 10.0, 1.0, forced_break_after=[5])


def test_spread_forced_break_within_group_is_not_physical():
    # 强制在系统 0 后分页：页 1（左页）组内断开，休止 0 也不检查、不参与瓶颈
    res = plan_pages([6.0, 4.0], [0.0], 10.0, 5.0, layout_mode="spread",
                     forced_break_after=[0])
    assert res["page_count"] == 2
    assert [p["side"] for p in res["pages"]] == ["left", "right"]
    assert [p["physical_turn_after"] for p in res["pages"]] == [False, False]
    assert res["pages"][0]["turn_rest_after"] is None
    assert res["min_turn_rest"] is None


def test_spread_forced_break_after_right_page_checks_rest():
    # 强制在系统 0 后分页（组内，不检查）；此后唯一可行的实体翻页在右页之后，
    # 休止达标则参与瓶颈，不足则无解
    base = dict(
        system_heights=[5.0, 5.0, 5.0, 5.0],
        page_height=10.0,
        min_safe_rest=1.0,
        layout_mode="spread",
        forced_break_after=[0],
    )
    res = plan_pages(**base, boundary_rests=[0.0, 0.5, 9.0])
    assert [p["physical_turn_after"] for p in res["pages"]] == [False, True, False]
    assert res["min_turn_rest"] == 9.0
    with pytest.raises(InfeasibleError) as exc:
        plan_pages(**base, boundary_rests=[0.0, 0.5, 0.5])
    assert exc.value.detail["boundary_index"] == 1
    assert exc.value.detail["physical_turn"] is True


def test_spread_forbidden_break_diagnosis():
    # 禁止分页的边界导致不可达时，诊断指出该约束而非休止
    with pytest.raises(InfeasibleError) as exc:
        plan_pages([6.0, 6.0], [9.0], 10.0, 1.0, layout_mode="spread",
                   forbidden_break_after=[0])
    assert exc.value.reason == "unsafe_boundary"
    assert exc.value.detail["boundary_index"] == 0
    assert exc.value.detail["forbidden_break"] is True
