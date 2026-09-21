"""SQLite 持久化层（仅保存完整方案，不保存部分方案）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);
"""


class PlanStore:
    def __init__(self, path: str) -> None:
        self.path = path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        """建库建表，随应用启动完成。"""
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def insert(self, request: dict[str, Any], result: dict[str, Any]) -> int:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO plans (created_at, request_json, result_json) VALUES (?, ?, ?)",
                (created_at, json.dumps(request), json.dumps(result)),
            )
            return int(cur.lastrowid)

    def get(self, plan_id: int) -> Optional[dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
        return self._to_dict(row) if row else None

    def list(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM plans ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._to_dict(r) for r in rows]

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "request": json.loads(row["request_json"]),
            "result": json.loads(row["result_json"]),
        }
