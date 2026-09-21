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
- 实体翻页恢复约束（可选）：boundary_times 给出各边界的演奏秒数（长度 =
  系统数 - 1，严格递增），min_turn_gap_seconds（默认 0）要求任意两次相邻
  实体翻页之间至少间隔该秒数；启用（gap > 0）时必须提供 boundary_times。
  single 的每次翻页都计入；spread 仅右页之后的实体翻页计入，组内（左页→
  右页）换页不消耗恢复时间；第一次实体翻页不检查间隔。规划状态因此携带
  “上一次实体翻页”所在边界。

实现：两阶段精确 DP。
- 阶段一：widest-path DP 求最大可行瓶颈 B*，同时得到各位置可达性；
- 阶段二：仅保留休止 >= B* 的实体翻页边，做 (留白平方和, 页数) 字典序最小的 DP。
spread 模式下 DP 状态额外携带已用页数奇偶：页码奇偶决定该页在左还是右，
从而决定该页之后是否为实体翻页；启用恢复约束时再额外携带上一次实体翻页
所在边界（-1 表示尚无实体翻页）。

无解诊断：
- 若某系统自身高度超过页面可用高度，报告首个这样的系统（无法容纳）；
- 否则取首个不可达位置 u（spread 模式指两种页数奇偶均不可达）。可以证明：
  在所有系统都能单独容纳时，u 必然 < n，且单系统页 [u-1, u) 不跨过任何
  强制断点、从可达的 u-1 出发只差一条分页边，因此边界 u-1 要么被
  forbidden_break_after 禁止分页，要么休止不足（spread 模式下后一种情形
  该边界必为实体翻页边界——若可达状态使该页落在左页，组内边界不检查休止，
  u 反而可达），要么是实体翻页恢复间隔不可达（距上一次实体翻页太近），
  据此报告首个无法安全完成的边界及其前次翻页边界。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import inf
from typing import Any, Iterable, Optional

_EPS = 1e-9  # 浮点比较容差


@dataclass
class InfeasibleError(Exception):
    """无解诊断：首个无法容纳的系统，或首个无法安全完成的（实体）翻页边界。"""

    reason: str  # "system_exceeds_page_height" | "unsafe_boundary" | "turn_gap_unreachable"
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
    boundary_times: Optional[Iterable[float]] = None,
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
    if min_turn_gap_seconds != min_turn_gap_seconds or min_turn_gap_seconds in (
        inf,
        -inf,
    ) or min_turn_gap_seconds < 0:
        raise ValueError("min_turn_gap_seconds 必须是非负的有限数值")
    if boundary_times is not None:
        times = list(boundary_times)
        if len(times) != n - 1:
            raise ValueError(
                "boundary_times 的长度必须等于 len(system_heights) - 1"
            )
        for t in times:
            if t != t or t in (inf, -inf) or t < 0:
                raise ValueError("boundary_times 必须全部为非负的有限数值")
        if any(times[k] <= times[k - 1] for k in range(1, len(times))):
            raise ValueError("boundary_times 必须严格递增")
    else:
        if min_turn_gap_seconds > _EPS:
            raise ValueError("启用恢复约束（min_turn_gap_seconds > 0）时必须提供 boundary_times")
        times = None
    recovery_enabled = times is not None and min_turn_gap_seconds > _EPS

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

    if layout_mode == "spread":
        return _plan_spread(
            system_heights,
            boundary_rests,
            page_height,
            min_safe_rest,
            spread_start_side,
            forced,
            forbidden,
            times,
            min_turn_gap_seconds,
            recovery_enabled,
        )
    return _plan_single(
        system_heights,
        boundary_rests,
        page_height,
        min_safe_rest,
        forced,
        forbidden,
        times,
        min_turn_gap_seconds,
        recovery_enabled,
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
    times: Optional[list[float]],
    min_gap: float,
    recovery_enabled: bool,
) -> dict[str, Any]:
    """single 模式：每次翻页都是实体翻页。"""
    n = len(system_heights)
    prefix = _prefix_sums(system_heights)
    earliest = _earliest_starts(n, forced)

    def height(i: int, j: int) -> float:
        """系统 [i, j) 的总高度。"""
        return prefix[j] - prefix[i]

    def gap_ok(prev_boundary: int, boundary: int) -> bool:
        """边界 boundary 处的实体翻页与上一次实体翻页（边界 prev_boundary）间隔是否达标。

        prev_boundary == -1 表示此前尚无实体翻页，第一次翻页不检查间隔。
        """
        if not recovery_enabled or prev_boundary < 0:
            return True
        return times[boundary] - times[prev_boundary] >= min_gap - _EPS

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
            if j < n and recovery_enabled and i > 0 and not gap_ok(i - 1, j - 1):
                continue  # 与上一次实体翻页（边界 i-1）间隔不足
            w = widest[i] if j == n else min(widest[i], boundary_rests[j - 1])
            if w > best:
                best = w
        widest[j] = best

    if widest[n] == -inf:
        # 首个不可达位置 u：所有系统均可单独容纳，且单系统页不跨强制断点，
        # 故边界 u-1 要么被禁止分页，要么休止不足，要么恢复间隔不可达
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
        elif boundary_rests[b] < min_safe_rest - _EPS:
            if b in forced:
                detail["forced_break"] = True
            detail["message"] = (
                f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）的休止 "
                f"{boundary_rests[b]}s 低于最低安全休止 {min_safe_rest}s，"
                "此前区间无法安全断开"
            )
        else:
            # 恢复间隔不可达：取能到达 b 的最早页起点 k（其前次实体翻页边界
            # k-1 最早、间隔最大）；连最大可达间隔都不足，才在此处无解
            prev = next(
                k - 1
                for k in range(max(1, earliest[u]), u)
                if height(k, u) <= page_height + _EPS
                and widest[k] != -inf
                and not gap_ok(k - 1, b)
            )
            _fill_gap_detail(detail, times, b, prev, min_gap, b in forced)
            raise InfeasibleError(reason="turn_gap_unreachable", detail=detail)
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
            if j < n and recovery_enabled and i > 0 and not gap_ok(i - 1, j - 1):
                continue  # 恢复间隔不足的实体翻页边禁用
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

    pages = _build_pages(
        cuts,
        height,
        page_height,
        boundary_rests,
        sides=[None] * len(cuts),
    )

    return {
        "page_count": len(pages),
        "min_turn_rest": None if bottleneck == inf else bottleneck,
        "total_slack_squared": round(dp[n][0], 6),
        "pages": pages,
    }


def _fill_gap_detail(
    detail: dict[str, Any],
    times: list[float],
    boundary: int,
    prev_boundary: int,
    min_gap: float,
    forced: bool,
) -> None:
    """填充恢复间隔不可达诊断：首个不可达边界及其前次实体翻页边界。"""
    gap = times[boundary] - times[prev_boundary]
    detail.update(
        {
            "prev_turn_boundary_index": prev_boundary,
            "prev_turn_between_systems": [prev_boundary, prev_boundary + 1],
            "turn_time_seconds": times[boundary],
            "prev_turn_time_seconds": times[prev_boundary],
            "gap_seconds": gap,
            "min_turn_gap_seconds": min_gap,
            "physical_turn": True,
            "message": (
                f"边界 {boundary} 是首个实体翻页恢复间隔不可达的边界："
                f"距上一次实体翻页（边界 {prev_boundary}，{times[prev_boundary]}s）"
                f"仅 {gap}s，低于要求的最小恢复间隔 {min_gap}s"
            ),
        }
    )
    if forced:
        detail["forced_break"] = True


def _build_pages(
    cuts: list[tuple[int, int]],
    height,
    page_height: float,
    boundary_rests: list[float],
    sides: list[Optional[str]],
) -> list[dict[str, Any]]:
    """按回溯得到的切分点组装逐页结果。

    sides 给出每页的左右侧（spread："left"/"right"；single：全为 None）。
    spread 仅右页之后为实体翻页，single 每次翻页都是实体翻页。
    """
    n = len(boundary_rests) + 1
    pages = []
    for idx, (i, j) in enumerate(cuts):
        h = height(i, j)
        side = sides[idx]
        last = j == n
        physical = (side != "left") and not last
        pages.append(
            {
                "page": idx + 1,
                "start_system": i,
                "end_system": j - 1,
                "system_count": j - i,
                "used_height": round(h, 6),
                "slack": round(page_height - h, 6),
                "turn_rest_after": boundary_rests[j - 1] if physical else None,
                "side": side,
                "physical_turn_after": physical,
            }
        )
    return pages


def _plan_spread(
    system_heights: list[float],
    boundary_rests: list[float],
    page_height: float,
    min_safe_rest: float,
    spread_start_side: str,
    forced: set[int],
    forbidden: set[int],
    times: Optional[list[float]],
    min_gap: float,
    recovery_enabled: bool,
) -> dict[str, Any]:
    """spread 模式：相邻左右页同组可见，仅翻过右页（实体翻页）才检查休止。

    DP 状态携带已用页数奇偶：第 k 页（0 起）的左右由 k 与 spread_start_side
    共同决定，页码奇偶因此直接决定该页之后是否为实体翻页。人工断点只约束
    页边界位置：强制断点落在左页之后时仍是组内翻页，不检查休止、不参与瓶颈。
    启用恢复约束时，状态再携带上一次实体翻页所在边界（-1 表示尚无实体翻页）：
    组内换页不改变它，仅右页后的实体翻页更新它并检查恢复间隔。
    """
    n = len(system_heights)
    if recovery_enabled:
        return _plan_spread_recovery(
            system_heights,
            boundary_rests,
            page_height,
            min_safe_rest,
            spread_start_side,
            forced,
            forbidden,
            times,
            min_gap,
        )
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

    sides = ["right" if is_right_page(idx) else "left" for idx in range(len(cuts))]
    pages = _build_pages(cuts, height, page_height, boundary_rests, sides)

    return {
        "page_count": len(pages),
        "min_turn_rest": None if bottleneck == inf else bottleneck,
        "total_slack_squared": round(best_cost[0], 6),
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
    times: list[float],
    min_gap: float,
) -> dict[str, Any]:
    """spread 模式并启用实体翻页恢复约束。

    状态 = (位置, 已用页数奇偶, 上一次实体翻页边界)；t = -1 表示尚无实体翻页。
    组内（左页之后）换页不检查休止、不改变 t；右页之后的实体翻页要求休止达标、
    且距上一次实体翻页至少 min_gap 秒，随后把 t 更新为当前边界。
    """
    n = len(system_heights)
    prefix = _prefix_sums(system_heights)
    earliest = _earliest_starts(n, forced)

    def height(i: int, j: int) -> float:
        return prefix[j] - prefix[i]

    right_parity = 1 if spread_start_side == "left" else 0

    def is_right_page(page_index: int) -> bool:
        return page_index % 2 == right_parity

    def gap_ok(t: int, boundary: int) -> bool:
        if t < 0:
            return True
        return times[boundary] - times[t] >= min_gap - _EPS

    # 阶段一：widest-path。widest[j][q] = {上一次实体翻页边界: 瓶颈值}
    widest: list[list[dict[int, float]]] = [[{}, {}] for _ in range(n + 1)]
    widest[0][0] = {-1: inf}
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue
        for i in range(j - 1, earliest[j] - 1, -1):
            if height(i, j) > page_height + _EPS:
                break
            for q in (0, 1):
                src = widest[i][q]
                if not src:
                    continue
                dst = widest[j][1 - q]
                if j < n and q == right_parity:
                    # 结束于 j 的这一页是右页：边界 j-1 是实体翻页
                    rest = boundary_rests[j - 1]
                    if rest < min_safe_rest - _EPS:
                        continue
                    for t, wv in src.items():
                        if not gap_ok(t, j - 1):
                            continue
                        w = min(wv, rest)
                        cur = dst.get(j - 1, -inf)
                        if w > cur:
                            dst[j - 1] = w
                else:
                    # 末页或组内（左页之后）：不检查休止、不改变上一次实体翻页
                    for t, wv in src.items():
                        cur = dst.get(t, -inf)
                        if wv > cur:
                            dst[t] = wv

    if not widest[n][0] and not widest[n][1]:
        # 首个两种页数奇偶均不可达的位置 u
        u = next(k for k in range(1, n + 1) if not widest[k][0] and not widest[k][1])
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
            raise InfeasibleError(reason="unsafe_boundary", detail=detail)
        if boundary_rests[b] < min_safe_rest - _EPS and widest[u - 1][right_parity]:
            detail["physical_turn"] = True
            if b in forced:
                detail["forced_break"] = True
            detail["message"] = (
                f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）是首个无法安全完成的实体翻页边界："
                f"此处必然结束一页右页、需要实体翻页，但休止 {boundary_rests[b]}s "
                f"低于最低安全休止 {min_safe_rest}s"
            )
            raise InfeasibleError(reason="unsafe_boundary", detail=detail)
        # 恢复间隔不可达：找一个右页状态落在 b 的可达来源，取其中上一次实体
        # 翻页最早（间隔最大仍不足）的状态
        prev_t = None
        for i in range(u - 1, earliest[u] - 1, -1):
            if height(i, u) > page_height + _EPS:
                break
            src = widest[i][right_parity]
            if not src:
                continue
            reachable = (t for t in src if gap_ok(t, b))
            if next(reachable, None) is not None:
                break  # 存在间隔达标的来源，u 理应可达（不应发生）
            ts = [t for t in src if t >= 0]
            if ts and (prev_t is None or min(ts) < prev_t):
                prev_t = min(ts)
        assert prev_t is not None
        _fill_gap_detail(detail, times, b, prev_t, min_gap, b in forced)
        raise InfeasibleError(reason="turn_gap_unreachable", detail=detail)

    bottleneck = max(
        max(widest[n][0].values(), default=-inf),
        max(widest[n][1].values(), default=-inf),
    )

    # 阶段二：只保留休止 >= 瓶颈且恢复间隔达标的实体翻页边，
    # 最小化 (留白平方和, 页数)。dp[j][q] = {t: (留白平方和, 页数)}
    dp: list[list[dict[int, tuple[float, int]]]] = [[{}, {}] for _ in range(n + 1)]
    parent: list[list[dict[int, tuple[int, int, int]]]] = [[{}, {}] for _ in range(n + 1)]
    dp[0][0] = {-1: (0.0, 0)}
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue
        for i in range(j - 1, earliest[j] - 1, -1):
            h = height(i, j)
            if h > page_height + _EPS:
                break
            for q in (0, 1):
                src = dp[i][q]
                if not src:
                    continue
                dst = dp[j][1 - q]
                pdst = parent[j][1 - q]
                physical = j < n and q == right_parity
                if physical and boundary_rests[j - 1] < bottleneck - _EPS:
                    continue
                for t, (slack_sum, pages_used) in src.items():
                    nt = t
                    if physical:
                        if not gap_ok(t, j - 1):
                            continue
                        nt = j - 1
                    cand = (slack_sum + (page_height - h) ** 2, pages_used + 1)
                    if nt not in dst or cand < dst[nt]:
                        dst[nt] = cand
                        pdst[nt] = (i, q, t)

    # 终点取 (留白平方和, 页数) 最优的 (奇偶, 上一次实体翻页边界)
    best_cost: Optional[tuple[float, int]] = None
    final_q = 0
    final_t = -1
    for q in (0, 1):
        for t, cost in dp[n][q].items():
            if best_cost is None or cost < best_cost:
                best_cost, final_q, final_t = cost, q, t

    # 回溯切分点
    cuts: list[tuple[int, int]] = []
    j, q, t = n, final_q, final_t
    while j > 0:
        prev = parent[j][q][t]
        i, pq, pt = prev
        cuts.append((i, j))
        j, q, t = i, pq, pt
    cuts.reverse()

    sides = ["right" if is_right_page(idx) else "left" for idx in range(len(cuts))]
    pages = _build_pages(cuts, height, page_height, boundary_rests, sides)

    return {
        "page_count": len(pages),
        "min_turn_rest": None if bottleneck == inf else bottleneck,
        "total_slack_squared": round(best_cost[0], 6),
        "pages": pages,
    }
