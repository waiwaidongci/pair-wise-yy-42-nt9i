from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (ID_PREFIX, STATES, closure_clear, dispatch_plan,
                    format_closure_blockers)


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
                CREATE TABLE IF NOT EXISTS task_zones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
                    current_wind REAL NOT NULL DEFAULT 0,
                    latest_breakpoints TEXT NOT NULL DEFAULT '[]',
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_zones_external_ref
                    ON task_zones(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS dispatches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_id INTEGER NOT NULL REFERENCES task_zones(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL,
                    order_no TEXT NOT NULL,
                    breakpoints TEXT NOT NULL DEFAULT '[]',
                    wind_level REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'planned'
                        CHECK(status IN ('planned','in_progress','completed','closed')),
                    plan TEXT NOT NULL DEFAULT '{{}}',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, order_no)
                );
                CREATE TABLE IF NOT EXISTS dispatch_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
                    zone_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    member_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'occupied'
                        CHECK(status IN ('occupied','pending_coordination','returned','released')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_member_occupied
                    ON dispatch_members(item_id, member_name) WHERE status='occupied';
                CREATE INDEX IF NOT EXISTS ix_dispatch_members_zone
                    ON dispatch_members(zone_id);
                CREATE TABLE IF NOT EXISTS dispatch_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
                    reason TEXT NOT NULL,
                    wind_level REAL NOT NULL,
                    plan TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS field_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_id INTEGER NOT NULL REFERENCES task_zones(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('wind','breakpoints','location')),
                    report_no TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{{}}',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(zone_id, report_no)
                );
                CREATE TABLE IF NOT EXISTS zone_materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_id INTEGER NOT NULL REFERENCES task_zones(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    required_qty REAL NOT NULL DEFAULT 0,
                    delivered_qty REAL NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(zone_id, name)
                );
                CREATE TABLE IF NOT EXISTS material_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    material_id INTEGER NOT NULL REFERENCES zone_materials(id) ON DELETE CASCADE,
                    zone_id INTEGER NOT NULL,
                    qty REAL NOT NULL,
                    report_no TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(zone_id, report_no)
                );
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

    # ---- 火场撤离调度 ----
    def create_zone(self, item_id: int, name: str, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        self.get_item(item_id)
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO task_zones(item_id, name, status, current_wind,
                       latest_breakpoints, version, external_ref, created_by,
                       created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, name, "open", 0.0, "[]", 1, external_ref, actor, now, now),
                )
                zone_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("任务区名称或external_ref已存在") from exc
        return self.get_zone(zone_id)

    def get_zone(self, zone_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM task_zones WHERE id=?", (zone_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务区不存在")
        result = dict(row)
        result["latest_breakpoints"] = json.loads(result["latest_breakpoints"])
        return result

    def list_zones(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task_zones WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        result = []
        for row in rows:
            zone = dict(row)
            zone["latest_breakpoints"] = json.loads(zone["latest_breakpoints"])
            result.append(zone)
        return result

    def _zone_locked(self, zone_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM task_zones WHERE id=?", (zone_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务区不存在")
        return row

    def create_dispatch(self, zone_id: int, order_no: str, breakpoints: list,
                        wind_level: Optional[float], note: str, members: List[str],
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            zone = self._zone_locked(zone_id)
            if zone["status"] != "open":
                raise ConflictError("任务区已关闭，无法派工")
            item_id = int(zone["item_id"])
            existing = self.conn.execute(
                "SELECT id FROM dispatches WHERE item_id=? AND order_no=?",
                (item_id, order_no),
            ).fetchone()
            if existing is not None:
                return {"merged": True, "dispatch": self.get_dispatch(existing["id"])}
            if wind_level is None:
                wind_level = float(zone["current_wind"])
            plan = dispatch_plan(wind_level, breakpoints, len(members))
            cur = self.conn.execute(
                """INSERT INTO dispatches(zone_id, item_id, order_no, breakpoints,
                   wind_level, note, status, plan, version, created_by, created_at,
                   updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (zone_id, item_id, order_no, json.dumps(breakpoints, ensure_ascii=False),
                 wind_level, note, "planned",
                 json.dumps(plan, ensure_ascii=False, sort_keys=True), 1, actor, now, now),
            )
            dispatch_id = int(cur.lastrowid)
            member_rows = []
            for name in members:
                status = "occupied"
                try:
                    self.conn.execute(
                        """INSERT INTO dispatch_members(dispatch_id, zone_id, item_id,
                           member_name, status, created_at, updated_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (dispatch_id, zone_id, item_id, name, "occupied", now, now),
                    )
                except sqlite3.IntegrityError:
                    status = "pending_coordination"
                    self.conn.execute(
                        """INSERT INTO dispatch_members(dispatch_id, zone_id, item_id,
                           member_name, status, created_at, updated_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (dispatch_id, zone_id, item_id, name, "pending_coordination",
                         now, now),
                    )
                member_rows.append({"member_name": name, "status": status})
            self.conn.execute(
                """INSERT INTO dispatch_plans(dispatch_id, reason, wind_level, plan,
                   created_at) VALUES(?,?,?,?,?)""",
                (dispatch_id, "initial", wind_level,
                 json.dumps(plan, ensure_ascii=False, sort_keys=True), now),
            )
        return {"merged": False, "dispatch": self.get_dispatch(dispatch_id),
                "members": member_rows, "plan": plan}

    def get_dispatch(self, dispatch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
        if row is None:
            raise NotFoundError("派工单不存在")
        result = dict(row)
        result["breakpoints"] = json.loads(result["breakpoints"])
        result["plan"] = json.loads(result["plan"])
        return result

    def list_dispatches(self, zone_id: int) -> List[Dict[str, Any]]:
        self.get_zone(zone_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dispatches WHERE zone_id=? ORDER BY id", (zone_id,)
            ).fetchall()
        result = []
        for row in rows:
            dispatch = dict(row)
            dispatch["breakpoints"] = json.loads(dispatch["breakpoints"])
            dispatch["plan"] = json.loads(dispatch["plan"])
            result.append(dispatch)
        return result

    def list_dispatch_members(self, dispatch_id: int) -> List[Dict[str, Any]]:
        self.get_dispatch(dispatch_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dispatch_members WHERE dispatch_id=? ORDER BY id",
                (dispatch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_dispatch_plans(self, dispatch_id: int) -> List[Dict[str, Any]]:
        self.get_dispatch(dispatch_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dispatch_plans WHERE dispatch_id=? ORDER BY id",
                (dispatch_id,),
            ).fetchall()
        result = []
        for row in rows:
            plan = dict(row)
            plan["plan"] = json.loads(plan["plan"])
            result.append(plan)
        return result

    def transition_dispatch(self, dispatch_id: int, target: str,
                            expected_version: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE dispatches SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, dispatch_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("派工单不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_dispatch(dispatch_id)

    def resolve_member(self, dispatch_id: int, member_id: int, action: str,
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM dispatch_members WHERE id=? AND dispatch_id=?",
                (member_id, dispatch_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("队员占用不存在")
            member = dict(row)
            if action == "release":
                if member["status"] not in ("occupied", "pending_coordination"):
                    raise ConflictError("当前占用状态不可释放")
                self.conn.execute(
                    "UPDATE dispatch_members SET status='released', updated_at=? WHERE id=?",
                    (now, member_id),
                )
                member["status"] = "released"
            elif action == "promote":
                if member["status"] != "pending_coordination":
                    raise ConflictError("仅待协调占用可转为占用")
                try:
                    self.conn.execute(
                        """UPDATE dispatch_members SET status='occupied', updated_at=?
                           WHERE id=?""",
                        (now, member_id),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("该队员已有先到占用，不能重复占用") from exc
                member["status"] = "occupied"
            else:
                raise ConflictError("未知协调动作")
        member["updated_at"] = now
        return member

    def _find_report_locked(self, zone_id: int, report_no: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM field_reports WHERE zone_id=? AND report_no=?",
            (zone_id, report_no),
        ).fetchone()
        if row is None:
            return None
        report = dict(row)
        report["payload"] = json.loads(report["payload"])
        return report

    def _insert_report_locked(self, zone_id: int, kind: str, report_no: str,
                              payload: dict, actor: str, now: str) -> None:
        self.conn.execute(
            """INSERT INTO field_reports(zone_id, kind, report_no, payload, created_by,
               created_at) VALUES(?,?,?,?,?,?)""",
            (zone_id, kind, report_no,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), actor, now),
        )

    def submit_wind_report(self, zone_id: int, report_no: str, wind_level: float,
                           actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            zone = self._zone_locked(zone_id)
            if zone["status"] != "open":
                raise ConflictError("任务区已关闭，无法回传")
            existing = self._find_report_locked(zone_id, report_no)
            if existing is not None:
                return {"merged": True, "report": existing, "changed": False,
                        "recalculated": [], "reconfirm": []}
            self._insert_report_locked(zone_id, "wind", report_no,
                                       {"wind_level": wind_level}, actor, now)
            changed = float(zone["current_wind"]) != float(wind_level)
            self.conn.execute(
                """UPDATE task_zones SET current_wind=?, version=version+1, updated_at=?
                   WHERE id=?""",
                (wind_level, now, zone_id),
            )
            recalculated: List[int] = []
            reconfirm: List[int] = []
            if changed:
                rows = self.conn.execute(
                    "SELECT * FROM dispatches WHERE zone_id=? AND status='planned'",
                    (zone_id,),
                ).fetchall()
                for row in rows:
                    dispatch = dict(row)
                    breakpoints = json.loads(dispatch["breakpoints"])
                    crew = self.conn.execute(
                        """SELECT COUNT(*) AS n FROM dispatch_members
                           WHERE dispatch_id=? AND status<>'released'""",
                        (dispatch["id"],),
                    ).fetchone()
                    plan = dispatch_plan(wind_level, breakpoints, int(crew["n"]))
                    self.conn.execute(
                        """UPDATE dispatches SET wind_level=?, plan=?, version=version+1,
                           updated_at=? WHERE id=?""",
                        (wind_level,
                         json.dumps(plan, ensure_ascii=False, sort_keys=True),
                         now, dispatch["id"]),
                    )
                    self.conn.execute(
                        """INSERT INTO dispatch_plans(dispatch_id, reason, wind_level,
                           plan, created_at) VALUES(?,?,?,?,?)""",
                        (dispatch["id"], "wind_change", wind_level,
                         json.dumps(plan, ensure_ascii=False, sort_keys=True), now),
                    )
                    recalculated.append(int(dispatch["id"]))
                rows = self.conn.execute(
                    "SELECT id FROM dispatches WHERE zone_id=? AND status='closed'",
                    (zone_id,),
                ).fetchall()
                for row in rows:
                    self.conn.execute(
                        """UPDATE dispatches SET status='completed', version=version+1,
                           updated_at=? WHERE id=?""",
                        (now, int(row["id"])),
                    )
                    reconfirm.append(int(row["id"]))
            saved = self._find_report_locked(zone_id, report_no)
        return {"merged": False, "report": saved, "changed": changed,
                "recalculated": recalculated, "reconfirm": reconfirm}

    def submit_breakpoints_report(self, zone_id: int, report_no: str,
                                  points: list, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            zone = self._zone_locked(zone_id)
            if zone["status"] != "open":
                raise ConflictError("任务区已关闭，无法回传")
            existing = self._find_report_locked(zone_id, report_no)
            if existing is not None:
                return {"merged": True, "report": existing}
            self._insert_report_locked(zone_id, "breakpoints", report_no,
                                       {"breakpoints": points}, actor, now)
            self.conn.execute(
                """UPDATE task_zones SET latest_breakpoints=?, version=version+1,
                   updated_at=? WHERE id=?""",
                (json.dumps(points, ensure_ascii=False), now, zone_id),
            )
            saved = self._find_report_locked(zone_id, report_no)
        return {"merged": False, "report": saved}

    def submit_location_report(self, zone_id: int, report_no: str, member_name: str,
                               returned: bool, position: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            zone = self._zone_locked(zone_id)
            if zone["status"] != "open":
                raise ConflictError("任务区已关闭，无法回传")
            payload = {"member_name": member_name, "returned": returned,
                       "position": position}
            existing = self._find_report_locked(zone_id, report_no)
            if existing is not None:
                return {"merged": True, "report": existing, "returned_count": 0}
            self._insert_report_locked(zone_id, "location", report_no, payload,
                                       actor, now)
            returned_count = 0
            if returned:
                cur = self.conn.execute(
                    """UPDATE dispatch_members SET status='returned', updated_at=?
                       WHERE zone_id=? AND member_name=? AND status='occupied'""",
                    (now, zone_id, member_name),
                )
                returned_count = int(cur.rowcount)
            saved = self._find_report_locked(zone_id, report_no)
        return {"merged": False, "report": saved, "returned_count": returned_count}

    def upsert_material(self, zone_id: int, name: str, required_qty: float,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self._zone_locked(zone_id)
            self.conn.execute(
                """INSERT INTO zone_materials(zone_id, name, required_qty, delivered_qty,
                   created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(zone_id, name) DO UPDATE SET
                   required_qty=excluded.required_qty, updated_at=excluded.updated_at""",
                (zone_id, name, required_qty, 0.0, actor, now, now),
            )
            row = self.conn.execute(
                "SELECT * FROM zone_materials WHERE zone_id=? AND name=?",
                (zone_id, name),
            ).fetchone()
        return dict(row)

    def list_materials(self, zone_id: int) -> List[Dict[str, Any]]:
        self.get_zone(zone_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM zone_materials WHERE zone_id=? ORDER BY id", (zone_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def deliver_material(self, zone_id: int, material_id: int, qty: float,
                         report_no: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self._zone_locked(zone_id)
            material = self.conn.execute(
                "SELECT * FROM zone_materials WHERE id=? AND zone_id=?",
                (material_id, zone_id),
            ).fetchone()
            if material is None:
                raise NotFoundError("物资不存在")
            existing = self.conn.execute(
                "SELECT * FROM material_deliveries WHERE zone_id=? AND report_no=?",
                (zone_id, report_no),
            ).fetchone()
            if existing is not None:
                return {"merged": True, "delivery": dict(existing),
                        "material": self.get_material(material_id)}
            cur = self.conn.execute(
                """INSERT INTO material_deliveries(material_id, zone_id, qty, report_no,
                   created_by, created_at) VALUES(?,?,?,?,?,?)""",
                (material_id, zone_id, qty, report_no, actor, now),
            )
            delivery_id = int(cur.lastrowid)
            self.conn.execute(
                """UPDATE zone_materials SET delivered_qty=delivered_qty+?, updated_at=?
                   WHERE id=?""",
                (qty, now, material_id),
            )
            row = self.conn.execute(
                "SELECT * FROM material_deliveries WHERE id=?", (delivery_id,)).fetchone()
        return {"merged": False, "delivery": dict(row),
                "material": self.get_material(material_id)}

    def get_material(self, material_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM zone_materials WHERE id=?", (material_id,)).fetchone()
        if row is None:
            raise NotFoundError("物资不存在")
        return dict(row)

    def _closure_checklist_locked(self, zone_id: int) -> Dict[str, Any]:
        unreturned = self.conn.execute(
            """SELECT m.id AS member_id, m.member_name, m.dispatch_id, d.order_no
               FROM dispatch_members m JOIN dispatches d ON d.id=m.dispatch_id
               WHERE m.zone_id=? AND m.status='occupied' ORDER BY m.id""",
            (zone_id,),
        ).fetchall()
        materials = self.conn.execute(
            """SELECT id AS material_id, name, required_qty, delivered_qty
               FROM zone_materials WHERE zone_id=? AND delivered_qty<required_qty
               ORDER BY id""",
            (zone_id,),
        ).fetchall()
        pending = self.conn.execute(
            """SELECT m.id AS member_id, m.member_name, m.dispatch_id, d.order_no
               FROM dispatch_members m JOIN dispatches d ON d.id=m.dispatch_id
               WHERE m.zone_id=? AND m.status='pending_coordination' ORDER BY m.id""",
            (zone_id,),
        ).fetchall()
        return {
            "unreturned_members": [dict(row) for row in unreturned],
            "incomplete_materials": [
                dict(row, missing_qty=row["required_qty"] - row["delivered_qty"])
                for row in materials
            ],
            "pending_coordinations": [dict(row) for row in pending],
        }

    def closure_checklist(self, zone_id: int) -> Dict[str, Any]:
        with self._lock:
            self._zone_locked(zone_id)
            checklist = self._closure_checklist_locked(zone_id)
        checklist["clear"] = closure_clear(checklist)
        return checklist

    def close_zone(self, zone_id: int, expected_version: int,
                   actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            zone = self._zone_locked(zone_id)
            if zone["status"] == "closed":
                raise ConflictError("任务区已关闭")
            checklist = self._closure_checklist_locked(zone_id)
            if not closure_clear(checklist):
                raise ConflictError(format_closure_blockers(checklist))
            cur = self.conn.execute(
                """UPDATE task_zones SET status='closed', version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (now, zone_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_zone(zone_id)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
