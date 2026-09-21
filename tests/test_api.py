"""API 层测试：建库、提交、查询、无解诊断。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    # 重新加载 DB 路径后重建 app
    import importlib

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


GOOD_BODY = {
    "system_heights": [120.0, 130.0, 110.0, 125.0, 115.0],
    "boundary_rests": [1.0, 4.0, 0.5, 6.0],
    "page_height": 300.0,
    "min_safe_rest": 2.0,
}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_get_plan(client):
    r = client.post("/plans", json=GOOD_BODY)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["id"] == 1
    assert plan["page_count"] >= 1
    total = sum(p["used_height"] for p in plan["pages"])
    assert total == pytest.approx(sum(GOOD_BODY["system_heights"]))
    for p in plan["pages"]:
        assert p["used_height"] <= GOOD_BODY["page_height"] + 1e-6
    for p in plan["pages"][:-1]:
        assert p["turn_rest_after"] >= GOOD_BODY["min_safe_rest"]
    assert plan["pages"][-1]["turn_rest_after"] is None

    got = client.get(f"/plans/{plan['id']}")
    assert got.status_code == 200
    assert got.json() == plan

    listed = client.get("/plans")
    assert listed.status_code == 200
    assert any(s["id"] == plan["id"] for s in listed.json())


def test_infeasible_returns_422_and_saves_nothing(client):
    bad = dict(GOOD_BODY, boundary_rests=[0.1, 0.1, 0.1, 0.1], page_height=150.0)
    r = client.post("/plans", json=bad)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "no_feasible_pagination"
    assert detail["reason"] in ("system_exceeds_page_height", "unsafe_boundary")
    assert client.get("/plans").json() == []  # 不保存部分方案


def test_oversized_system_diagnosis(client):
    body = dict(GOOD_BODY, system_heights=[100.0, 999.0, 100.0], boundary_rests=[3.0, 3.0])
    r = client.post("/plans", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["reason"] == "system_exceeds_page_height"
    assert detail["system_index"] == 1


def test_get_missing_plan_404(client):
    assert client.get("/plans/42").status_code == 404


def test_validation_errors(client):
    # 边界数量不匹配
    r = client.post("/plans", json=dict(GOOD_BODY, boundary_rests=[1.0]))
    assert r.status_code == 422
    # 非正高度
    r = client.post("/plans", json=dict(GOOD_BODY, system_heights=[0.0, 1.0]))
    assert r.status_code == 422
    # 空系统列表
    r = client.post("/plans", json=dict(GOOD_BODY, system_heights=[], boundary_rests=[]))
    assert r.status_code == 422
    # 非法排版模式 / 起始侧
    r = client.post("/plans", json=dict(GOOD_BODY, layout_mode="book"))
    assert r.status_code == 422
    r = client.post("/plans", json=dict(GOOD_BODY, layout_mode="spread", spread_start_side="middle"))
    assert r.status_code == 422


SPREAD_BODY = {
    "system_heights": [6.0, 6.0, 6.0],
    "boundary_rests": [0.2, 8.0],
    "page_height": 10.0,
    "min_safe_rest": 1.0,
    "layout_mode": "spread",
    "spread_start_side": "left",
}


def test_layout_mode_defaults_to_single(client):
    r = client.post("/plans", json=GOOD_BODY)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["layout_mode"] == "single"
    # single 模式响应兼容：不分左右页，每次翻页都是实体翻页
    for p in plan["pages"]:
        assert p["side"] is None
        assert p["physical_turn_after"] == (p["turn_rest_after"] is not None)


def test_spread_plan_create_and_replay(client):
    r = client.post("/plans", json=SPREAD_BODY)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["layout_mode"] == "spread"
    assert plan["spread_start_side"] == "left"
    assert [p["side"] for p in plan["pages"]] == ["left", "right", "left"]
    assert [p["physical_turn_after"] for p in plan["pages"]] == [False, True, False]
    assert plan["min_turn_rest"] == 8.0
    # 组内边界（页 1→2）不检查休止，也不产生 turn_rest_after
    assert plan["pages"][0]["turn_rest_after"] is None
    assert plan["pages"][1]["turn_rest_after"] == 8.0

    # SQLite 回放：新增字段完整返回
    got = client.get(f"/plans/{plan['id']}")
    assert got.status_code == 200
    assert got.json() == plan

    listed = client.get("/plans")
    assert listed.status_code == 200
    summary = next(s for s in listed.json() if s["id"] == plan["id"])
    assert summary["layout_mode"] == "spread"


def test_spread_default_start_side_is_left(client):
    body = {k: v for k, v in SPREAD_BODY.items() if k != "spread_start_side"}
    r = client.post("/plans", json=body)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["spread_start_side"] == "left"
    assert [p["side"] for p in plan["pages"]] == ["left", "right", "left"]


def test_spread_infeasible_diagnosis(client):
    # 页 2 为右页，其后实体翻页边界 1 休止不足 → 422 且定位该实体翻页边界
    r = client.post("/plans", json=dict(SPREAD_BODY, boundary_rests=[8.0, 0.2]))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "no_feasible_pagination"
    assert detail["reason"] == "unsafe_boundary"
    assert detail["boundary_index"] == 1
    assert detail["physical_turn"] is True
    assert client.get("/plans").json() == []  # 不保存部分方案


# ---- 人工断点约束 ----

CONSTRAINED_BODY = {
    "system_heights": [100.0, 100.0, 100.0, 100.0],
    "boundary_rests": [5.0, 5.0, 5.0],
    "page_height": 200.0,
    "min_safe_rest": 1.0,
    "forced_break_after": [1],
    "forbidden_break_after": [2],
}


def test_break_constraints_default_empty(client):
    r = client.post("/plans", json=GOOD_BODY)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["forced_break_after"] == []
    assert plan["forbidden_break_after"] == []


def test_break_constraints_create_and_replay(client):
    r = client.post("/plans", json=CONSTRAINED_BODY)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["forced_break_after"] == [1]
    assert plan["forbidden_break_after"] == [2]
    # 唯一的分页点在系统 1 之后（强制），系统 2 之后不分页（禁止）
    assert [p["end_system"] for p in plan["pages"][:-1]] == [1]

    # SQLite 回放保留两组约束
    got = client.get(f"/plans/{plan['id']}")
    assert got.status_code == 200
    assert got.json() == plan


def test_break_constraints_conflict_422(client):
    body = dict(CONSTRAINED_BODY, forced_break_after=[1, 2], forbidden_break_after=[2])
    r = client.post("/plans", json=body)
    assert r.status_code == 422
    assert "2" in str(r.json()["detail"])  # 给出具体冲突下标
    assert client.get("/plans").json() == []  # 不保存任何方案


def test_break_constraint_on_last_system_422(client):
    # 指向末系统
    r = client.post("/plans", json=dict(CONSTRAINED_BODY, forced_break_after=[3],
                                        forbidden_break_after=[]))
    assert r.status_code == 422
    r = client.post("/plans", json=dict(CONSTRAINED_BODY, forbidden_break_after=[3],
                                        forced_break_after=[]))
    assert r.status_code == 422
    # 越界下标同样 422
    r = client.post("/plans", json=dict(CONSTRAINED_BODY, forced_break_after=[4],
                                        forbidden_break_after=[]))
    assert r.status_code == 422
    assert client.get("/plans").json() == []


def test_infeasible_due_to_forbidden_break_saves_nothing(client):
    # 禁止在系统 0 后分页，但系统 0、1 超高无法同页 → 422 且不保存
    body = {
        "system_heights": [6.0, 6.0, 6.0],
        "boundary_rests": [9.0, 9.0],
        "page_height": 10.0,
        "min_safe_rest": 1.0,
        "forbidden_break_after": [0],
    }
    r = client.post("/plans", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "no_feasible_pagination"
    assert detail["reason"] == "unsafe_boundary"
    assert detail["boundary_index"] == 0
    assert detail["forbidden_break"] is True
    assert client.get("/plans").json() == []


def test_forced_break_with_insufficient_rest_422(client):
    # 强制分页处休止不足 → 沿用现有无解诊断，不保存
    body = {
        "system_heights": [5.0, 5.0, 5.0],
        "boundary_rests": [0.5, 9.0],
        "page_height": 10.0,
        "min_safe_rest": 1.0,
        "forced_break_after": [0],
    }
    r = client.post("/plans", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["reason"] == "unsafe_boundary"
    assert detail["boundary_index"] == 0
    assert detail["forced_break"] is True
    assert client.get("/plans").json() == []


def test_spread_forced_break_within_group_not_physical(client):
    # spread 组内（左页之后）的强制断点不算实体翻页、不检查休止：
    # 若被当作实体翻页，边界 0 的休止 0.0 < 1.0 将无解
    body = {
        "system_heights": [6.0, 4.0, 6.0],
        "boundary_rests": [0.0, 8.0],
        "page_height": 10.0,
        "min_safe_rest": 1.0,
        "layout_mode": "spread",
        "forced_break_after": [0],
    }
    r = client.post("/plans", json=body)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["forced_break_after"] == [0]
    assert [p["side"] for p in plan["pages"]] == ["left", "right"]
    assert plan["pages"][0]["end_system"] == 0  # 强制断点生效
    assert plan["pages"][0]["physical_turn_after"] is False
    assert plan["pages"][0]["turn_rest_after"] is None
    assert plan["min_turn_rest"] is None


# ---- 实体翻页恢复约束 ----

RECOVERY_BODY = {
    "system_heights": [6.0, 6.0, 6.0, 6.0],
    "boundary_rests": [9.0, 9.0, 9.0],
    "page_height": 10.0,
    "min_safe_rest": 1.0,
    "boundary_times": [2.0, 8.0, 16.0],
    "min_turn_gap_seconds": 5.0,
}


def test_recovery_fields_default_when_absent(client):
    r = client.post("/plans", json=GOOD_BODY)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["boundary_times"] is None
    assert plan["min_turn_gap_seconds"] == 0.0
    for p in plan["pages"]:
        assert p["turn_time_after"] is None


def test_recovery_create_and_replay(client):
    r = client.post("/plans", json=RECOVERY_BODY)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["boundary_times"] == [2.0, 8.0, 16.0]
    assert plan["min_turn_gap_seconds"] == 5.0
    # 每系统一页，三次实体翻页间隔 6、8 均达标
    assert plan["page_count"] == 4
    assert [p["turn_time_after"] for p in plan["pages"]] == [2.0, 8.0, 16.0, None]
    # 完整回放
    got = client.get(f"/plans/{plan['id']}")
    assert got.status_code == 200
    assert got.json() == plan


def test_recovery_spread_create_and_replay(client):
    body = dict(
        RECOVERY_BODY,
        system_heights=[6.0, 6.0, 6.0, 6.0, 6.0],
        boundary_rests=[9.0, 9.0, 9.0, 9.0],
        boundary_times=[2.0, 2.5, 8.0, 16.0],
        layout_mode="spread",
    )
    r = client.post("/plans", json=body)
    assert r.status_code == 201, r.text
    plan = r.json()
    # 仅右页之后（边界 1、3）计入恢复间隔；组内边界 0、2 不检查
    assert [p["physical_turn_after"] for p in plan["pages"]] == [False, True, False, True, False]
    assert [p["turn_time_after"] for p in plan["pages"]] == [None, 2.5, None, 16.0, None]
    got = client.get(f"/plans/{plan['id']}")
    assert got.json() == plan


def test_recovery_gap_infeasible_422_and_saves_nothing(client):
    body = dict(RECOVERY_BODY, boundary_times=[2.0, 4.0, 16.0])  # 间隔 2 < 5
    r = client.post("/plans", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "no_feasible_pagination"
    assert detail["reason"] == "recovery_gap_violation"
    assert detail["boundary_index"] == 1
    assert detail["between_systems"] == [1, 2]
    assert detail["previous_turn_boundary_index"] == 0
    assert detail["previous_turn_between_systems"] == [0, 1]
    assert detail["actual_gap_seconds"] == 2.0
    assert detail["min_turn_gap_seconds"] == 5.0
    assert client.get("/plans").json() == []  # 不落库


def test_recovery_missing_times_422(client):
    body = {k: v for k, v in RECOVERY_BODY.items() if k != "boundary_times"}
    r = client.post("/plans", json=body)
    assert r.status_code == 422
    assert "boundary_times" in str(r.json()["detail"])
    assert client.get("/plans").json() == []


def test_recovery_times_validation_422(client):
    # 长度不符
    r = client.post("/plans", json=dict(RECOVERY_BODY, boundary_times=[1.0, 2.0]))
    assert r.status_code == 422
    # 非严格递增
    r = client.post("/plans", json=dict(RECOVERY_BODY, boundary_times=[2.0, 2.0, 9.0]))
    assert r.status_code == 422
    assert "严格递增" in str(r.json()["detail"])
    # 负数
    r = client.post("/plans", json=dict(RECOVERY_BODY, boundary_times=[-1.0, 2.0, 9.0]))
    assert r.status_code == 422
    assert client.get("/plans").json() == []


def test_boundary_times_allowed_without_gap(client):
    # 提供 boundary_times 但 gap 保持默认 0：视为普通请求，约束不启用
    body = {k: v for k, v in RECOVERY_BODY.items() if k != "min_turn_gap_seconds"}
    r = client.post("/plans", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["min_turn_gap_seconds"] == 0.0
