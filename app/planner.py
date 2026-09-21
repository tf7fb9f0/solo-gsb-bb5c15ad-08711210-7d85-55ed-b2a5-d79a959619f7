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

实现：两阶段精确 DP。
- 阶段一：widest-path DP 求最大可行瓶颈 B*，同时得到各位置可达性；
- 阶段二：仅保留休止 >= B* 的实体翻页边，做 (留白平方和, 页数) 字典序最小的 DP。
spread 模式下 DP 状态额外携带已用页数奇偶：页码奇偶决定该页在左还是右，
从而决定该页之后是否为实体翻页。

无解诊断：
- 若某系统自身高度超过页面可用高度，报告首个这样的系统（无法容纳）；
- 否则取首个不可达位置 u（spread 模式指两种页数奇偶均不可达）。可以证明：
  在所有系统都能单独容纳时，u 必然 < n，且单系统页 [u-1, u) 不跨过任何
  强制断点、从可达的 u-1 出发只差一条分页边，因此边界 u-1 要么被
  forbidden_break_after 禁止分页，要么休止不足（spread 模式下后一种情形
  该边界必为实体翻页边界——若可达状态使该页落在左页，组内边界不检查休止，
  u 反而可达），据此报告首个无法安全完成的边界。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import inf
from typing import Any, Iterable, Optional

_EPS = 1e-9  # 浮点比较容差


@dataclass
class InfeasibleError(Exception):
    """无解诊断：首个无法容纳的系统，或首个无法安全完成的（实体）翻页边界。"""

    reason: str  # "system_exceeds_page_height" | "unsafe_boundary"
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
