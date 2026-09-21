"""乐谱安全翻页动态规划核心（single 单页 / spread 双页谱架）。

目标（按字典序依次优化）：
1. 最大化全曲最短的一次实体翻页休止（瓶颈值，越大越安全）；
2. 在达到该瓶颈的方案中，最小化各页留白高度的平方和；
3. 仍并列时，取页数更少的方案。

约束：
- 每页容纳连续若干系统，总高度不得超过页面可用高度；
- single：每次翻页都是实体翻页，只允许在休止秒数 >= 最低安全休止的边界翻页
  （最后一页之后无需翻页）；
- spread：相邻左、右页组成一组同时可见，组内（左页→右页）边界不检查休止；
  只有翻过右页进入下一组才是实体翻页，要求休止 >= 最低安全休止，
  且只有实体翻页参与最短翻页休止最大化；
- 人工断点：forced_break_after 中的系统之后必须分页，forbidden_break_after
  中的系统之后禁止分页，两者在 DP 转移阶段直接裁剪分页边。强制分页只约束
  页边界位置，不改变实体翻页判定：spread 组内（左页之后）的强制断点仍不
  检查休止、不参与瓶颈，只有右页之后的断点才检查 min_safe_rest 并参与瓶颈。
- 实体翻页恢复约束：min_turn_gap_seconds > 0 时启用（此时必须提供长度为
  系统数-1、严格递增的 boundary_times 演奏秒数），任意两次相邻实体翻页的
  时间差必须 >= 该值。single 的每次翻页计入；spread 仅右页之后的翻页计入；
  组内换页不消耗恢复时间。DP 状态因此额外携带"上一次实体翻页边界"。

实现：两阶段精确 DP。
- 阶段一：widest-path DP 求最大可行瓶颈 B*，同时得到各位置可达性；
- 阶段二：仅保留休止 >= B* 的实体翻页边，做 (留白平方和, 页数) 字典序最小的 DP。
spread 模式下 DP 状态额外携带已用页数奇偶：页码奇偶决定该页在左还是右，
从而决定该页之后是否为实体翻页。启用恢复约束时再携带上一次实体翻页边界
（对其做前缀聚合，整体仍为 O(n²)）。

无解诊断：
- 若某系统自身高度超过页面可用高度，报告首个这样的系统（无法容纳）；
- 否则取首个不可达位置 u（spread 模式指所有页数奇偶均不可达）。可以证明：
  在所有系统都能单独容纳时，u 必然 < n，且单系统页 [u-1, u) 不跨过任何
  强制断点、从可达的 u-1 出发只差一条分页边，因此边界 u-1 要么被
  forbidden_break_after 禁止分页，要么休止不足（spread 模式下后一种情形
  该边界必为实体翻页边界——若可达状态使该页落在左页，组内边界不检查休止，
  u 反而可达），要么（启用恢复约束时）距上一次实体翻页太近，据此报告首个
  无法安全完成的边界。
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from math import inf
from typing import Any, Iterable, Optional

_EPS = 1e-9  # 浮点比较容差


@dataclass
class InfeasibleError(Exception):
    """无解诊断：首个无法容纳的系统，或首个无法安全完成的（实体）翻页边界。"""

    reason: str  # "system_exceeds_page_height" | "unsafe_boundary" | "recovery_gap_violation"
    detail: dict[str, Any] = field(default_factory=dict)


def plan_pages(
    system_heights: list[float],
    boundary_rests: list[float],
    page_height: float,
    min_safe_rest: float,
    layout_mode: str = "single",
    spread_start_side: str = "left",
    forced_break_after: Optional[Iterable[int]] = None,
    forbidden_break_after: Optional[Iterable[int]] = None,
    boundary_times: Optional[list[float]] = None,
    min_turn_gap_seconds: float = 0.0,
) -> dict[str, Any]:
    """生成完整分页方案；无解时抛出 InfeasibleError，不产生任何部分方案。"""
    n = len(system_heights)
    if n == 0:
        raise ValueError("system_heights must not be empty")
    if len(boundary_rests) != n - 1:
        raise ValueError("boundary_rests must have exactly len(system_heights) - 1 entries")
    if layout_mode not in ("single", "spread"):
        raise ValueError("layout_mode must be 'single' or 'spread'")
    if spread_start_side not in ("left", "right"):
        raise ValueError("spread_start_side must be 'left' or 'right'")
    if min_turn_gap_seconds < 0:
        raise ValueError("min_turn_gap_seconds 必须非负")
    if min_turn_gap_seconds > 0 and boundary_times is None:
        raise ValueError(
            "启用实体翻页恢复约束（min_turn_gap_seconds > 0）时必须提供 boundary_times"
        )
    if boundary_times is not None:
        if len(boundary_times) != n - 1:
            raise ValueError("boundary_times 的长度必须等于 len(system_heights) - 1")
        if any(t != t or t in (inf, -inf) for t in boundary_times):
            raise ValueError("boundary_times 必须全部为有限数值")
        if any(t < 0 for t in boundary_times):
            raise ValueError("boundary_times 必须全部为非负演奏秒数")
        if any(boundary_times[k + 1] <= boundary_times[k] + _EPS for k in range(n - 2)):
            raise ValueError("boundary_times 必须严格递增")

    forced = set(forced_break_after or ())
    forbidden = set(forbidden_break_after or ())
    for b in sorted(forced | forbidden):
        if not 0 <= b < n - 1:
            raise ValueError(
                f"断点下标 {b} 无效：只能在系统 0..{n - 2} 之后分页（末系统之后无需分页）"
            )
    conflict = sorted(forced & forbidden)
    if conflict:
        raise ValueError(
            f"forced_break_after 与 forbidden_break_after 存在冲突下标: {conflict}"
        )

    # 首个无法容纳的系统
    for i, h in enumerate(system_heights):
        if h > page_height + _EPS:
            raise InfeasibleError(
                reason="system_exceeds_page_height",
                detail={
                    "system_index": i,
                    "system_height": h,
                    "page_height": page_height,
                    "message": (
                        f"系统 {i} 的高度 {h} 超过页面可用高度 {page_height}，"
                        "任何页面都无法容纳该系统"
                    ),
                },
            )

    if min_turn_gap_seconds > 0:
        assert boundary_times is not None  # 上面已校验
        if layout_mode == "spread":
            return _plan_spread_recovery(
                system_heights,
                boundary_rests,
                page_height,
                min_safe_rest,
                spread_start_side,
                forced,
                forbidden,
                boundary_times,
                min_turn_gap_seconds,
            )
        return _plan_single_recovery(
            system_heights,
            boundary_rests,
            page_height,
            min_safe_rest,
            forced,
            forbidden,
            boundary_times,
            min_turn_gap_seconds,
        )

    if layout_mode == "spread":
        return _plan_spread(
            system_heights,
            boundary_rests,
            page_height,
            min_safe_rest,
            spread_start_side,
            forced,
            forbidden,
        )
    return _plan_single(
        system_heights, boundary_rests, page_height, min_safe_rest, forced, forbidden
    )


def _prefix_sums(system_heights: list[float]) -> list[float]:
    prefix = [0.0] * (len(system_heights) + 1)
    for i, h in enumerate(system_heights):
        prefix[i + 1] = prefix[i] + h
    return prefix


def _earliest_starts(n: int, forced: set[int]) -> list[int]:
    """earliest[j] = 页 [i, j) 不跨过任何强制断点的最小起始下标 i。

    页 [i, j) 的内部边界为 i..j-2；强制断点 b 要求页边界必须落在系统 b 之后，
    即任何页面不得同时包含系统 b 与 b+1，故 i 必须大于所有 <= j-2 的强制断点。
    """
    earliest = [0] * (n + 1)
    max_forced = -1
    for j in range(1, n + 1):
        if j >= 2 and (j - 2) in forced:
            max_forced = j - 2
        earliest[j] = max_forced + 1
    return earliest


def _plan_single(
    system_heights: list[float],
    boundary_rests: list[float],
    page_height: float,
    min_safe_rest: float,
    forced: set[int],
    forbidden: set[int],
) -> dict[str, Any]:
    """single 模式：每次翻页都是实体翻页。"""
    n = len(system_heights)
    prefix = _prefix_sums(system_heights)
    earliest = _earliest_starts(n, forced)

    def height(i: int, j: int) -> float:
        """系统 [i, j) 的总高度。"""
        return prefix[j] - prefix[i]

    # 阶段一：widest-path，最大化最短翻页休止；同时记录可达性
    widest = [-inf] * (n + 1)
    widest[0] = inf
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue  # 禁止在边界 j-1 分页，位置 j 不可作为页尾
        best = -inf
        for i in range(j - 1, earliest[j] - 1, -1):
            if height(i, j) > page_height + _EPS:
                break  # 高度为正，i 再小只会更高
            if widest[i] == -inf:
                continue
            if j < n and boundary_rests[j - 1] < min_safe_rest - _EPS:
                break  # 该边界休止不足，对所有 i 都不可断开
            w = widest[i] if j == n else min(widest[i], boundary_rests[j - 1])
            if w > best:
                best = w
        widest[j] = best

    if widest[n] == -inf:
        # 首个不可达位置 u：所有系统均可单独容纳，且单系统页不跨强制断点，
        # 故边界 u-1 要么被禁止分页，要么休止不足
        u = next(k for k in range(1, n + 1) if widest[k] == -inf)
        b = u - 1
        detail: dict[str, Any] = {
            "boundary_index": b,
            "between_systems": [b, b + 1],
            "rest_seconds": boundary_rests[b],
            "min_safe_rest": min_safe_rest,
        }
        if b in forbidden:
            detail["forbidden_break"] = True
            detail["message"] = (
                f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）被 forbidden_break_after "
                "禁止分页，此前区间无法安全断开"
            )
        else:
            if b in forced:
                detail["forced_break"] = True
            detail["message"] = (
                f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）的休止 "
                f"{boundary_rests[b]}s 低于最低安全休止 {min_safe_rest}s，"
                "此前区间无法安全断开"
            )
        raise InfeasibleError(reason="unsafe_boundary", detail=detail)

    bottleneck = widest[n]

    # 阶段二：只用休止 >= 瓶颈的翻页边，最小化 (留白平方和, 页数)
    dp: list[Optional[tuple[float, int]]] = [None] * (n + 1)
    parent = [-1] * (n + 1)
    dp[0] = (0.0, 0)
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue  # 禁止在边界 j-1 分页
        for i in range(j - 1, earliest[j] - 1, -1):
            h = height(i, j)
            if h > page_height + _EPS:
                break
            if dp[i] is None:
                continue
            if j < n and boundary_rests[j - 1] < bottleneck - _EPS:
                break  # 低于瓶颈的翻页边会破坏第一目标，禁用
            cand = (dp[i][0] + (page_height - h) ** 2, dp[i][1] + 1)
            if dp[j] is None or cand < dp[j]:
                dp[j] = cand
                parent[j] = i

    # 回溯切分点
    cuts: list[tuple[int, int]] = []
    j = n
    while j > 0:
        i = parent[j]
        cuts.append((i, j))
        j = i
    cuts.reverse()

    pages = []
    for idx, (i, j) in enumerate(cuts):
        h = height(i, j)
        last = j == n
        pages.append(
            {
                "page": idx + 1,
                "start_system": i,
                "end_system": j - 1,
                "system_count": j - i,
                "used_height": round(h, 6),
                "slack": round(page_height - h, 6),
                "turn_rest_after": None if last else boundary_rests[j - 1],
                "turn_time_after": None,  # 未启用恢复约束时无演奏时间
                "side": None,  # single 模式不分左右页
                "physical_turn_after": not last,  # single 模式每次翻页都是实体翻页
            }
        )

    return {
        "page_count": len(pages),
        "min_turn_rest": None if bottleneck == inf else bottleneck,
        "total_slack_squared": round(dp[n][0], 6),
        "pages": pages,
    }


def _plan_spread(
    system_heights: list[float],
    boundary_rests: list[float],
    page_height: float,
    min_safe_rest: float,
    spread_start_side: str,
    forced: set[int],
    forbidden: set[int],
) -> dict[str, Any]:
    """spread 模式：相邻左右页同组可见，仅翻过右页（实体翻页）才检查休止。

    DP 状态携带已用页数奇偶：第 k 页（0 起）的左右由 k 与 spread_start_side
    共同决定，页码奇偶因此直接决定该页之后是否为实体翻页。人工断点只约束
    页边界位置：强制断点落在左页之后时仍是组内翻页，不检查休止、不参与瓶颈。
    """
    n = len(system_heights)
    prefix = _prefix_sums(system_heights)
    earliest = _earliest_starts(n, forced)

    def height(i: int, j: int) -> float:
        """系统 [i, j) 的总高度。"""
        return prefix[j] - prefix[i]

    # 第 1 页（下标 0）位于 spread_start_side，之后左右交替
    right_parity = 1 if spread_start_side == "left" else 0

    def is_right_page(page_index: int) -> bool:
        return page_index % 2 == right_parity

    # 阶段一：widest-path，状态 = (位置, 已用页数奇偶)，最大化最短实体翻页休止
    widest = [[-inf, -inf] for _ in range(n + 1)]
    widest[0][0] = inf
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue  # 禁止在边界 j-1 分页，位置 j 不可作为页尾
        for i in range(j - 1, earliest[j] - 1, -1):
            if height(i, j) > page_height + _EPS:
                break  # 高度为正，i 再小只会更高
            for q in (0, 1):
                if widest[i][q] == -inf:
                    continue
                # 结束于 j 的这一页下标奇偶为 q；右页之后（非末页）才是实体翻页
                physical = j < n and is_right_page(q)
                if physical:
                    rest = boundary_rests[j - 1]
                    if rest < min_safe_rest - _EPS:
                        continue  # 该次实体翻页休止不足
                    w = min(widest[i][q], rest)
                else:
                    w = widest[i][q]  # 组内边界不检查休止，也不影响瓶颈
                if w > widest[j][1 - q]:
                    widest[j][1 - q] = w

    if widest[n][0] == -inf and widest[n][1] == -inf:
        # 首个两种页数奇偶均不可达的位置 u：边界 u-1 要么被禁止分页，
        # 要么是休止不足的实体翻页边界
        u = next(
            k for k in range(1, n + 1) if widest[k][0] == -inf and widest[k][1] == -inf
        )
        b = u - 1
        detail: dict[str, Any] = {
            "boundary_index": b,
            "between_systems": [b, b + 1],
            "rest_seconds": boundary_rests[b],
            "min_safe_rest": min_safe_rest,
        }
        if b in forbidden:
            detail["forbidden_break"] = True
            detail["message"] = (
                f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）被 forbidden_break_after "
                "禁止分页，此前区间无法安全断开"
            )
        else:
            if b in forced:
                detail["forced_break"] = True
            detail["physical_turn"] = True
            detail["message"] = (
                f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）是首个无法安全完成的实体翻页边界："
                f"此处必然结束一页右页、需要实体翻页，但休止 {boundary_rests[b]}s "
                f"低于最低安全休止 {min_safe_rest}s"
            )
        raise InfeasibleError(reason="unsafe_boundary", detail=detail)

    bottleneck = max(widest[n][0], widest[n][1])

    # 阶段二：只用休止 >= 瓶颈的实体翻页边，最小化 (留白平方和, 页数)
    dp: list[list[Optional[tuple[float, int]]]] = [[None, None] for _ in range(n + 1)]
    parent: list[list[Optional[tuple[int, int]]]] = [[None, None] for _ in range(n + 1)]
    dp[0][0] = (0.0, 0)
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue  # 禁止在边界 j-1 分页
        for i in range(j - 1, earliest[j] - 1, -1):
            h = height(i, j)
            if h > page_height + _EPS:
                break
            for q in (0, 1):
                if dp[i][q] is None:
                    continue
                physical = j < n and is_right_page(q)
                if physical and boundary_rests[j - 1] < bottleneck - _EPS:
                    continue  # 低于瓶颈的实体翻页边会破坏第一目标，禁用
                cand = (dp[i][q][0] + (page_height - h) ** 2, dp[i][q][1] + 1)
                if dp[j][1 - q] is None or cand < dp[j][1 - q]:
                    dp[j][1 - q] = cand
                    parent[j][1 - q] = (i, q)

    # 终点两种页数奇偶取 (留白平方和, 页数) 更优者
    best_cost, final_q = min((dp[n][q], q) for q in (0, 1) if dp[n][q] is not None)

    # 回溯切分点
    cuts: list[tuple[int, int]] = []
    j, q = n, final_q
    while j > 0:
        prev = parent[j][q]
        assert prev is not None
        i, q = prev
        cuts.append((i, j))
        j = i
    cuts.reverse()

    pages = []
    for idx, (i, j) in enumerate(cuts):
        h = height(i, j)
        right = is_right_page(idx)
        last = j == n
        physical = right and not last
        pages.append(
            {
                "page": idx + 1,
                "start_system": i,
                "end_system": j - 1,
                "system_count": j - i,
                "used_height": round(h, 6),
                "slack": round(page_height - h, 6),
                "turn_rest_after": boundary_rests[j - 1] if physical else None,
                "turn_time_after": None,  # 未启用恢复约束时无演奏时间
                "side": "right" if right else "left",
                "physical_turn_after": physical,
            }
        )

    return {
        "page_count": len(pages),
        "min_turn_rest": None if bottleneck == inf else bottleneck,
        "total_slack_squared": round(best_cost[0], 6),
        "pages": pages,
    }



# ---- 实体翻页恢复约束（min_turn_gap_seconds > 0）----


def _gap_violation_detail(
    b: int,
    prev_b: int,
    boundary_rests: list[float],
    boundary_times: list[float],
    min_safe_rest: float,
    min_gap: float,
    forced: set[int],
    spread: bool,
) -> dict[str, Any]:
    """构造 recovery_gap_violation 无解诊断。

    b 为首个因恢复间隔不可达的实体翻页边界，prev_b 为对应的上一次实体翻页
    边界（取可达前缀中最早的一次，给出最大可能间隔的取证方案）。
    """
    turn_time = boundary_times[b]
    prev_time = boundary_times[prev_b]
    detail: dict[str, Any] = {
        "boundary_index": b,
        "between_systems": [b, b + 1],
        "rest_seconds": boundary_rests[b],
        "min_safe_rest": min_safe_rest,
        "previous_turn_boundary_index": prev_b,
        "previous_turn_between_systems": [prev_b, prev_b + 1],
        "previous_turn_time": prev_time,
        "turn_time": turn_time,
        "actual_gap_seconds": turn_time - prev_time,
        "min_turn_gap_seconds": min_gap,
    }
    if spread:
        detail["physical_turn"] = True
    if b in forced:
        detail["forced_break"] = True
    kind = "（右页之后的）实体翻页" if spread else "实体翻页"
    detail["message"] = (
        f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）是首个因实体翻页恢复间隔不足而"
        f"不可达的{kind}边界：上一次实体翻页发生在边界 {prev_b}"
        f"（系统 {prev_b} 与系统 {prev_b + 1} 之间，{prev_time}s），"
        f"两次翻页仅相隔 {turn_time - prev_time}s，小于要求的最小恢复间隔 {min_gap}s"
    )
    return detail


def _plan_single_recovery(
    system_heights: list[float],
    boundary_rests: list[float],
    page_height: float,
    min_safe_rest: float,
    forced: set[int],
    forbidden: set[int],
    boundary_times: list[float],
    min_gap: float,
) -> dict[str, Any]:
    """single 模式 + 恢复约束。

    每次翻页都是实体翻页：页尾位置 i（i<n）处"上一次实体翻页边界"必然是
    i-1（本页起点处的那次翻页），故分页边 (i, j)（j<n）只需检查
    t[j-1] - t[i-1] >= min_gap；i=0 为全曲首次翻页，无恢复间隔要求。
    状态无需额外维度，整体保持 O(n²)。
    """
    n = len(system_heights)
    prefix = _prefix_sums(system_heights)
    earliest = _earliest_starts(n, forced)

    def height(i: int, j: int) -> float:
        return prefix[j] - prefix[i]

    def fits_from(j: int) -> int:
        """满足 height(i, j) <= page_height 的最小 i（总 O(n²)）。"""
        lo = earliest[j]
        while lo < j and height(lo, j) > page_height + _EPS:
            lo += 1
        return lo

    # 阶段一：widest-path，物理分页边同时检查休止与恢复间隔
    widest = [-inf] * (n + 1)
    widest[0] = inf
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue  # 禁止在边界 j-1 分页，位置 j 不可作为页尾
        lo = fits_from(j)
        best = -inf
        for i in range(j - 1, lo - 1, -1):
            if widest[i] == -inf:
                continue
            if j < n:
                if boundary_rests[j - 1] < min_safe_rest - _EPS:
                    break  # 该边界休止不足，对所有 i 都不可断开
                # i=0 是全曲首次实体翻页，无"上一次翻页"，不检查恢复间隔
                if i > 0 and boundary_times[j - 1] - boundary_times[i - 1] < min_gap - _EPS:
                    continue
                w = min(widest[i], boundary_rests[j - 1])
            else:
                w = widest[i]  # 最后一页之后无需翻页
            if w > best:
                best = w
        widest[j] = best

    if widest[n] == -inf:
        u = next(k for k in range(1, n + 1) if widest[k] == -inf)
        b = u - 1
        if b in forbidden:
            raise InfeasibleError(
                reason="unsafe_boundary",
                detail={
                    "boundary_index": b,
                    "between_systems": [b, b + 1],
                    "rest_seconds": boundary_rests[b],
                    "min_safe_rest": min_safe_rest,
                    "forbidden_break": True,
                    "message": (
                        f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）被 "
                        "forbidden_break_after 禁止分页，此前区间无法安全断开"
                    ),
                },
            )
        if boundary_rests[b] < min_safe_rest - _EPS:
            detail: dict[str, Any] = {
                "boundary_index": b,
                "between_systems": [b, b + 1],
                "rest_seconds": boundary_rests[b],
                "min_safe_rest": min_safe_rest,
            }
            if b in forced:
                detail["forced_break"] = True
            detail["message"] = (
                f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）的休止 "
                f"{boundary_rests[b]}s 低于最低安全休止 {min_safe_rest}s，"
                "此前区间无法安全断开"
            )
            raise InfeasibleError(reason="unsafe_boundary", detail=detail)
        # 单系统页 [u-1, u) 的来源位置 u-1 可达：其上一次实体翻页必在边界 u-2；
        # 首次翻页（b=0）不存在恢复间隔问题，故此处 u >= 2
        prev_b = u - 2
        raise InfeasibleError(
            reason="recovery_gap_violation",
            detail=_gap_violation_detail(
                b, prev_b, boundary_rests, boundary_times,
                min_safe_rest, min_gap, forced, spread=False,
            ),
        )

    bottleneck = widest[n]

    # 阶段二：只保留休止 >= 瓶颈、且满足恢复间隔的分页边，
    # 最小化 (留白平方和, 页数)
    dp: list[Optional[tuple[float, int]]] = [None] * (n + 1)
    parent = [-1] * (n + 1)
    dp[0] = (0.0, 0)
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue
        lo = fits_from(j)
        for i in range(j - 1, lo - 1, -1):
            if dp[i] is None:
                continue
            if j < n:
                if boundary_rests[j - 1] < bottleneck - _EPS:
                    break
                if i > 0 and boundary_times[j - 1] - boundary_times[i - 1] < min_gap - _EPS:
                    continue
            h = height(i, j)
            cand = (dp[i][0] + (page_height - h) ** 2, dp[i][1] + 1)
            if dp[j] is None or cand < dp[j]:
                dp[j] = cand
                parent[j] = i

    cuts: list[tuple[int, int]] = []
    j = n
    while j > 0:
        i = parent[j]
        cuts.append((i, j))
        j = i
    cuts.reverse()

    pages = []
    for idx, (i, j) in enumerate(cuts):
        h = height(i, j)
        last = j == n
        pages.append(
            {
                "page": idx + 1,
                "start_system": i,
                "end_system": j - 1,
                "system_count": j - i,
                "used_height": round(h, 6),
                "slack": round(page_height - h, 6),
                "turn_rest_after": None if last else boundary_rests[j - 1],
                "turn_time_after": None if last else boundary_times[j - 1],
                "side": None,  # single 模式不分左右页
                "physical_turn_after": not last,
            }
        )

    return {
        "page_count": len(pages),
        "min_turn_rest": None if bottleneck == inf else bottleneck,
        "total_slack_squared": round(dp[n][0], 6),
        "pages": pages,
    }


def _plan_spread_recovery(
    system_heights: list[float],
    boundary_rests: list[float],
    page_height: float,
    min_safe_rest: float,
    spread_start_side: str,
    forced: set[int],
    forbidden: set[int],
    boundary_times: list[float],
    min_gap: float,
) -> dict[str, Any]:
    """spread 模式 + 恢复约束。

    第 1 页（下标 0）在 spread_start_side，之后左右交替；下标为
    right_parity 的页是右页。q = 已结束页数的奇偶。处理到位置 i 时的状态：
    - q == left_parity：最后结束的是右页（右页之后即实体翻页），位置 i 处
      "上一次实体翻页边界"必为 i-1，只有唯一状态 s=i，记为 L[i]
      （i=0 且 start=right 时初始状态在另一奇偶）；
    - q == right_parity：最后结束的是左页或初始状态，上一次实体翻页继承自
      更早的右页之后，0 <= s < i（s=0 表示尚无实体翻页），记为 R[i][s]。
    新增一页 [i,j)（q 翻转）：
    - 来源 left 奇偶（L[i]）→ 新增页是左页：j<n 时为组内换页边，不检查
      休止与间隔，进入 R[j][s=i]；
    - 来源 right 奇偶（R[i][s]）→ 新增页是右页：j<n 时为实体翻页边，
      要求休止达标且（s=0 首次翻页，或 t[j-1] - t[s-1] >= min_gap），
      进入 L[j]；
    - j=n（最后一页）：之后无需翻页，两种奇偶均可到达。
    阶段一对每行 R[i] 维护瓶颈值的 s 前缀最大值，阶段二维护
    (留白平方和, 页数) 的 s 前缀最小值，整体 O(n²)。
    """
    n = len(system_heights)
    prefix = _prefix_sums(system_heights)
    earliest = _earliest_starts(n, forced)

    def height(i: int, j: int) -> float:
        return prefix[j] - prefix[i]

    def fits_from(j: int) -> int:
        lo = earliest[j]
        while lo < j and height(lo, j) > page_height + _EPS:
            lo += 1
        return lo

    right_parity = 1 if spread_start_side == "left" else 0
    left_parity = 1 - right_parity

    def is_right_page(page_index: int) -> bool:
        return page_index % 2 == right_parity

    # ---------------- 阶段一：widest-path ----------------
    # Lw[i]：位置 i、left_parity 的瓶颈值（该奇偶 s 恒为 i）。
    # rpref[i][s]：位置 i、right_parity 上继承状态 s'<=s 的瓶颈最大值。
    Lw = [-inf] * (n + 1)
    rpref = [[-inf] * (n + 1) for _ in range(n)]
    if left_parity == 0:
        Lw[0] = inf  # 初始：0 页，q=0=left
    else:
        for s in range(n + 1):
            rpref[0][s] = inf  # 初始：0 页，q=0=right，s=0

    end_w = [-inf, -inf]
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue  # 禁止在边界 j-1 分页，位置 j 两种奇偶均不可达
        lo = fits_from(j)
        if j < n:
            rest = boundary_rests[j - 1]
            # 组内换页边：Lw[i] → R[j][s=i]，瓶颈值原样继承
            # 实体翻页边：rpref[i][m]（允许的前次翻页 s 前缀最大）→ L[j]
            if rest >= min_safe_rest - _EPS:
                # 允许的来源状态：s=0（首次）或 t[s-1] <= t[j-1] - min_gap
                m = bisect_right(
                    boundary_times, boundary_times[j - 1] - min_gap + _EPS, 0, j - 1
                )
                # s 范围 0..m-1 对应边界下标，s=0 额外表示首次翻页；
                # 索引 m 表示允许 s in {0} ∪ {s>=1: s-1<=m-1}，即 s<=m
                cap = m
                best = -inf
                for i in range(lo, j):
                    v = rpref[i][cap]
                    if v > best:
                        best = v
                Lj = min(best, rest) if best != -inf else -inf
            else:
                Lj = -inf
            Lw[j] = Lj
            # R[j] 只有组内边产生的状态 s=i（i in [lo,j)），构造前缀最大
            row = rpref[j]
            run = -inf
            for s in range(n + 1):
                i = s
                if lo <= i < j and Lw[i] > run:
                    run = Lw[i]
                row[s] = run
        else:
            # 最后一页：Lw[i] → 终点 left_parity；rpref[i] → 终点 right_parity
            for i in range(lo, n):
                if Lw[i] > end_w[left_parity]:
                    end_w[left_parity] = Lw[i]
                v = rpref[i][n]
                if v > end_w[right_parity]:
                    end_w[right_parity] = v

    if end_w[0] == -inf and end_w[1] == -inf:
        def _dead(k: int) -> bool:
            return Lw[k] == -inf and rpref[k][n] == -inf

        u = next(k for k in range(1, n + 1) if _dead(k))
        b = u - 1
        if b in forbidden:
            raise InfeasibleError(
                reason="unsafe_boundary",
                detail={
                    "boundary_index": b,
                    "between_systems": [b, b + 1],
                    "rest_seconds": boundary_rests[b],
                    "min_safe_rest": min_safe_rest,
                    "forbidden_break": True,
                    "message": (
                        f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）被 "
                        "forbidden_break_after 禁止分页，此前区间无法安全断开"
                    ),
                },
            )
        # 单系统组内边 [u-1,u)（新增左页）恒可行：Lw[u-1] 可达则 R[u] 可达，
        # 故 u 全死只能是新增右页的实体翻页边不可达
        if boundary_rests[b] < min_safe_rest - _EPS:
            detail: dict[str, Any] = {
                "boundary_index": b,
                "between_systems": [b, b + 1],
                "rest_seconds": boundary_rests[b],
                "min_safe_rest": min_safe_rest,
                "physical_turn": True,
            }
            if b in forced:
                detail["forced_break"] = True
            detail["message"] = (
                f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）是首个无法安全完成的实体翻页边界："
                f"此处必然结束一页右页、需要实体翻页，但休止 {boundary_rests[b]}s "
                f"低于最低安全休止 {min_safe_rest}s"
            )
            raise InfeasibleError(reason="unsafe_boundary", detail=detail)
        # 恢复间隔：取所有可作 [i,u) 右页来源的 R[i] 中最早的前次实体翻页 s
        # （s=0 为首次翻页、不受限；若可达则此处本可行，故 s>=1）
        lo = fits_from(u)
        witness = None
        for i in range(lo, u):
            row = rpref[i]
            if row[n] == -inf:
                continue
            if row[0] != -inf:
                witness = 0
                break
            prev = -inf
            for s in range(1, i):
                if row[s] != prev:
                    if witness is None or s < witness:
                        witness = s
                    prev = row[s]
        assert witness is not None and witness >= 1
        raise InfeasibleError(
            reason="recovery_gap_violation",
            detail=_gap_violation_detail(
                b, witness - 1, boundary_rests, boundary_times,
                min_safe_rest, min_gap, forced, spread=True,
            ),
        )

    bottleneck = max(end_w)

    # ---------------- 阶段二：(留白平方和, 页数) 字典序最小 ----------------
    Cost = Optional[tuple[float, int]]
    Lc: list[Cost] = [None] * (n + 1)
    # rc_pref[i][s]：R[i] 上继承状态 s'<=s 的最小 (代价, 取得位置 s')
    rc_pref: list[list[Optional[tuple[tuple[float, int], int]]]] = [
        [None] * (n + 1) for _ in range(n)
    ]
    # 实体边 L[j] 的来源 (i, s)；组内边 R[j][s=i] 无需另存（s 即来源 i）
    phys_parent: list[Optional[tuple[int, int]]] = [None] * (n + 1)
    end_parent: Optional[tuple[int, int, int]] = None

    if left_parity == 0:
        Lc[0] = (0.0, 0)
    else:
        for s in range(n + 1):
            rc_pref[0][s] = ((0.0, 0), 0)

    best_cost: Cost = None
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue
        lo = fits_from(j)
        if j < n:
            rest = boundary_rests[j - 1]
            # 实体翻页边：rc_pref[i][cap] → Lc[j]
            lbest: Optional[tuple[tuple[float, int], tuple[int, int]]] = None
            if rest >= bottleneck - _EPS:
                cap = bisect_right(
                    boundary_times, boundary_times[j - 1] - min_gap + _EPS, 0, j - 1
                )
                for i in range(lo, j):
                    mv = rc_pref[i][cap]
                    if mv is None:
                        continue
                    cand = (mv[0][0] + (page_height - height(i, j)) ** 2, mv[0][1] + 1)
                    if lbest is None or cand < lbest[0]:
                        lbest = (cand, (i, mv[1]))
            if lbest is not None:
                Lc[j] = lbest[0]
                phys_parent[j] = lbest[1]
            # 组内换页边：Lc[i] → Rc[j][s=i]，新增左页 [i,j) 的留白与页数
            # 在此计入；构造该行 s 前缀最小
            row = rc_pref[j]
            best_entry: Optional[tuple[tuple[float, int], int]] = None
            for s in range(n + 1):
                i = s
                if lo <= i < j and Lc[i] is not None:
                    entry = (
                        (Lc[i][0] + (page_height - height(i, j)) ** 2, Lc[i][1] + 1),
                        i,
                    )
                    if best_entry is None or entry[0] < best_entry[0]:
                        best_entry = entry
                row[s] = best_entry
        else:
            # 最后一页
            for i in range(lo, n):
                if Lc[i] is not None:
                    cand = (Lc[i][0] + (page_height - height(i, n)) ** 2, Lc[i][1] + 1)
                    if best_cost is None or cand < best_cost:
                        best_cost, end_parent = cand, (i, left_parity, i)
                mv = rc_pref[i][n]
                if mv is not None:
                    cand = (mv[0][0] + (page_height - height(i, n)) ** 2, mv[0][1] + 1)
                    if best_cost is None or cand < best_cost:
                        best_cost, end_parent = cand, (i, right_parity, mv[1])

    # ---------------- 回溯 ----------------
    assert end_parent is not None
    i, q, s = end_parent
    cuts: list[tuple[int, int]] = [(i, n)]
    j, q, s = i, q, s
    while j > 0:
        if q == left_parity:
            # L[j] 由实体边到达：来源 R[i][s]
            src = phys_parent[j]
            assert src is not None
            pi, ps = src
            cuts.append((pi, j))
            j, q, s = pi, right_parity, ps
        else:
            # R[j][s] 由组内边到达：来源 L[s]（组内边 s 即来源位置）
            assert 0 <= s < j  # s=0：首左页继承初始状态
            cuts.append((s, j))
            j, q, s = s, left_parity, s
    cuts.reverse()

    pages = []
    for idx, (pi, pj) in enumerate(cuts):
        h = height(pi, pj)
        right = is_right_page(idx)
        last = pj == n
        physical = right and not last
        pages.append(
            {
                "page": idx + 1,
                "start_system": pi,
                "end_system": pj - 1,
                "system_count": pj - pi,
                "used_height": round(h, 6),
                "slack": round(page_height - h, 6),
                "turn_rest_after": boundary_rests[pj - 1] if physical else None,
                "turn_time_after": boundary_times[pj - 1] if physical else None,
                "side": "right" if right else "left",
                "physical_turn_after": physical,
            }
        )

    return {
        "page_count": len(pages),
        "min_turn_rest": None if bottleneck == inf else bottleneck,
        "total_slack_squared": round(best_cost[0], 6),
        "pages": pages,
    }
