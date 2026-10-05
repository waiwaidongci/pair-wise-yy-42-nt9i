from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    team TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'on_duty'
                        CHECK(status IN ('on_duty','returned')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_areas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','signed','closed')),
                    signoff_wind_level INTEGER,
                    signoff_by TEXT,
                    signoff_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supplies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_area_id INTEGER NOT NULL
                        REFERENCES task_areas(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    required_qty INTEGER NOT NULL DEFAULT 1,
                    fulfilled_qty INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wind_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_no TEXT NOT NULL UNIQUE,
                    member_id INTEGER NOT NULL REFERENCES members(id),
                    task_area_id INTEGER NOT NULL REFERENCES task_areas(id),
                    breakpoint TEXT NOT NULL,
                    wind_level INTEGER NOT NULL,
                    priority INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','in_progress','completed','cancelled','pending_coordination')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dispatches_member_status
                    ON dispatches(member_id, status);
                CREATE INDEX IF NOT EXISTS idx_dispatches_task_area
                    ON dispatches(task_area_id);
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ---- 队员 ----
    def create_member(self, name: str, team: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO members(name, team, status, created_by, created_at) "
                "VALUES(?,?,?,?,?)",
                (name, team, "on_duty", actor, now),
            )
            member_id = int(cur.lastrowid)
        return self.get_member(member_id)

    def get_member(self, member_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM members WHERE id=?", (member_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("队员不存在")
        return dict(row)

    def list_members(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM members"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def mark_member_returned(self, member_id: int) -> Dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE members SET status='returned' WHERE id=?", (member_id,)
            )
        return self.get_member(member_id)

    # ---- 任务区 ----
    def create_task_area(self, name: str, description: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO task_areas(name, description, status, created_by, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (name, description, "open", actor, now, now),
            )
            area_id = int(cur.lastrowid)
        return self.get_task_area(area_id)

    def get_task_area(self, area_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM task_areas WHERE id=?", (area_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("任务区不存在")
        return dict(row)

    def list_task_areas(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM task_areas"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def set_task_area_status(self, area_id: int, status: str,
                             signoff_wind_level: Optional[int], signoff_by: Optional[str],
                             signoff_at: Optional[str]) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE task_areas SET status=?, signoff_wind_level=?, signoff_by=?, "
                "signoff_at=?, updated_at=? WHERE id=?",
                (status, signoff_wind_level, signoff_by, signoff_at, now, area_id),
            )
        return self.get_task_area(area_id)

    def revert_signed_task_areas(self) -> int:
        """风级变化后，已签认待确认的任务区签认作废，需重新签认。返回受影响数量。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE task_areas SET status='open', signoff_wind_level=NULL, "
                "signoff_by=NULL, signoff_at=NULL, updated_at=? WHERE status='signed'",
                (now,),
            )
            return int(cur.rowcount)

    # ---- 物资 ----
    def add_supply(self, task_area_id: int, name: str, required_qty: int) -> Dict[str, Any]:
        now = utc_now()
        self.get_task_area(task_area_id)
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO supplies(task_area_id, name, required_qty, fulfilled_qty, "
                "created_at) VALUES(?,?,?,?,?)",
                (task_area_id, name, required_qty, 0, now),
            )
            supply_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM supplies WHERE id=?", (supply_id,)
            ).fetchone()
        return dict(row)

    def list_supplies(self, task_area_id: int) -> List[Dict[str, Any]]:
        self.get_task_area(task_area_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM supplies WHERE task_area_id=? ORDER BY id",
                (task_area_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def fulfill_supply(self, supply_id: int, qty: int) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM supplies WHERE id=?", (supply_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("物资不存在")
            new_qty = min(int(row["required_qty"]), int(row["fulfilled_qty"]) + qty)
            self.conn.execute(
                "UPDATE supplies SET fulfilled_qty=? WHERE id=?", (new_qty, supply_id)
            )
            updated = self.conn.execute(
                "SELECT * FROM supplies WHERE id=?", (supply_id,)
            ).fetchone()
        return dict(updated)

    def incomplete_supplies(self, task_area_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM supplies WHERE task_area_id=? AND fulfilled_qty<required_qty "
                "ORDER BY id",
                (task_area_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 风级 ----
    def add_wind_report(self, level: int, source: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO wind_reports(level, source, created_by, created_at) "
                "VALUES(?,?,?,?)",
                (level, source, actor, now),
            )
            report_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM wind_reports WHERE id=?", (report_id,)
            ).fetchone()
        return dict(row)

    def latest_wind_level(self) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT level FROM wind_reports ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return int(row["level"]) if row else 0

    # ---- 派工 ----
    def get_dispatch(self, dispatch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("派工不存在")
        return dict(row)

    def get_dispatch_by_order_no(self, order_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatches WHERE order_no=?", (order_no,)
            ).fetchone()
        return dict(row) if row else None

    def list_dispatches(self, status: Optional[str] = None,
                        task_area_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM dispatches"
        clauses: list = []
        params: list = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if task_area_id is not None:
            clauses.append("task_area_id=?")
            params.append(task_area_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def member_has_active_dispatch(self, member_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM dispatches WHERE member_id=? AND status IN ('pending','in_progress') "
                "LIMIT 1",
                (member_id,),
            ).fetchone()
        return row is not None

    def create_dispatch(self, order_no: str, member_id: int, task_area_id: int,
                        breakpoint: str, wind_level: int, priority: int,
                        status: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO dispatches(order_no, member_id, task_area_id, breakpoint,
                       wind_level, priority, status, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (order_no, member_id, task_area_id, breakpoint, wind_level,
                     priority, status, actor, now, now),
                )
                dispatch_id = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            # 单号唯一：断网补报重试时按单号合并，返回已有派工，重试只算一次
            existing = self.get_dispatch_by_order_no(order_no)
            if existing is not None:
                return existing
            raise ConflictError("派工单号已存在")
        return self.get_dispatch(dispatch_id)

    def update_dispatch_status(self, dispatch_id: int, status: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE dispatches SET status=?, updated_at=? WHERE id=?",
                (status, now, dispatch_id),
            )
        return self.get_dispatch(dispatch_id)

    def activate_pending_coordination(self, dispatch_id: int, wind_level: int,
                                      priority: int) -> Dict[str, Any]:
        """待协调派工激活为正式派工：更新为未开工，并按当前风级重算起点条件。"""
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE dispatches SET status='pending', wind_level=?, priority=?, "
                "updated_at=? WHERE id=?",
                (wind_level, priority, now, dispatch_id),
            )
        return self.get_dispatch(dispatch_id)

    def recalculate_pending_dispatches(self, wind_level: int, priority: int) -> int:
        """风级变化后，未开工（pending）派工随新条件重算风级与优先级。
        进行中、已完成与待协调派工保留各自过程快照，不动。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE dispatches SET wind_level=?, priority=?, updated_at=? "
                "WHERE status='pending'",
                (wind_level, priority, now),
            )
            return int(cur.rowcount)

    def pending_coordination_for_area(self, task_area_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dispatches WHERE task_area_id=? AND status='pending_coordination' "
                "ORDER BY id",
                (task_area_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def unreturned_members_for_area(self, task_area_id: int) -> List[Dict[str, Any]]:
        """仍在该任务区活动派工上、且未归队的队员。"""
        with self._lock:
            rows = self.conn.execute(
                """SELECT DISTINCT m.* FROM members m
                   JOIN dispatches d ON d.member_id=m.id
                   WHERE d.task_area_id=? AND d.status IN ('pending','in_progress')
                     AND m.status='on_duty'
                   ORDER BY m.id""",
                (task_area_id,),
            ).fetchall()
        return [dict(row) for row in rows]
