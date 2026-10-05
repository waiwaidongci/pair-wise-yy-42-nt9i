from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ensure_role, normalize_severity, require_number, require_text
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DISPATCH_ROLES, ENTITY, MEMBER_ROLES,
                    RECORD_ROLES, SIGN_OFF_ROLES, SUPPLY_ROLES, TASK_AREA_CREATE_ROLES,
                    TITLE, VIEW_ROLES, WIND_ROLES, CLOSE_CONFIRM_ROLES,
                    completion_blockers, dispatch_priority, escalation_required,
                    normalize_wind_level, priority_score, recalculated_dispatch,
                    response_deadline_hours, role_for_transition,
                    validate_transition, close_checklist_blockers)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 队员 ----
    def create_member(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, MEMBER_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        team = require_text(payload.get("team", ""), "team", 100) if payload.get("team") is not None else ""
        member = self.repository.create_member(name, team, actor)
        self.repository.append_audit("member", "member", member["id"], actor, {
            "name": name, "team": team,
        })
        return member

    def list_members(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_members(status)

    def return_member(self, member_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, MEMBER_ROLES)
        actor = require_text(actor, "actor", 100)
        member = self.repository.get_member(member_id)
        if member["status"] == "returned":
            from .domain import ConflictError
            raise ConflictError("队员已归队")
        updated = self.repository.mark_member_returned(member_id)
        self.repository.append_audit("member_return", "member", member_id, actor, {
            "name": member["name"],
        })
        return updated

    # ---- 任务区 ----
    def create_task_area(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_AREA_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        description = require_text(payload.get("description", ""), "description") if payload.get("description") else ""
        area = self.repository.create_task_area(name, description, actor)
        self.repository.append_audit("task_area", "task_area", area["id"], actor, {
            "name": name,
        })
        return area

    def list_task_areas(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_task_areas(status)

    def get_task_area(self, area_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_task_area(area_id)

    def add_supply(self, area_id: int, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SUPPLY_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        required_qty = int(require_number(payload.get("required_qty", 1), "required_qty", 1))
        supply = self.repository.add_supply(area_id, name, required_qty)
        self.repository.append_audit("supply_add", "task_area", area_id, actor, {
            "supply_id": supply["id"], "name": name, "required_qty": required_qty,
        })
        return supply

    def fulfill_supply(self, supply_id: int, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SUPPLY_ROLES)
        actor = require_text(actor, "actor", 100)
        qty = int(require_number(payload.get("qty", 1), "qty", 1))
        supply = self.repository.fulfill_supply(supply_id, qty)
        self.repository.append_audit("supply_fulfill", "task_area", supply["task_area_id"], actor, {
            "supply_id": supply["id"], "fulfilled_qty": supply["fulfilled_qty"],
        })
        return supply

    def task_area_checklist(self, area_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        self.repository.get_task_area(area_id)
        return self._build_checklist(area_id)

    def _build_checklist(self, area_id: int) -> Dict[str, Any]:
        unreturned = self.repository.unreturned_members_for_area(area_id)
        incomplete = self.repository.incomplete_supplies(area_id)
        pending_coord = self.repository.pending_coordination_for_area(area_id)
        return {
            "unreturned_members": unreturned,
            "incomplete_supplies": incomplete,
            "pending_coordination": pending_coord,
        }

    def sign_off_task_area(self, area_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SIGN_OFF_ROLES)
        actor = require_text(actor, "actor", 100)
        area = self.repository.get_task_area(area_id)
        if area["status"] == "closed":
            from .domain import ConflictError
            raise ConflictError("任务区已关闭，不能重复签认")
        checklist = self._build_checklist(area_id)
        blockers = close_checklist_blockers(
            checklist["unreturned_members"],
            checklist["incomplete_supplies"],
            checklist["pending_coordination"],
        )
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        wind = self.repository.latest_wind_level()
        from .audit import utc_now
        updated = self.repository.set_task_area_status(
            area_id, "signed", wind, actor, utc_now())
        self.repository.append_audit("task_area_signoff", "task_area", area_id, actor, {
            "wind_level": wind,
        })
        return updated

    def confirm_close_task_area(self, area_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CLOSE_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        area = self.repository.get_task_area(area_id)
        if area["status"] != "signed":
            from .domain import ConflictError
            raise ConflictError("请先签认后再确认关闭")
        wind = self.repository.latest_wind_level()
        if area["signoff_wind_level"] != wind:
            from .domain import ConflictError
            raise ConflictError("风级已变化，请重新签认后再确认关闭")
        checklist = self._build_checklist(area_id)
        blockers = close_checklist_blockers(
            checklist["unreturned_members"],
            checklist["incomplete_supplies"],
            checklist["pending_coordination"],
        )
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        from .audit import utc_now
        updated = self.repository.set_task_area_status(
            area_id, "closed", area["signoff_wind_level"], area["signoff_by"], area["signoff_at"])
        self.repository.append_audit("task_area_closed", "task_area", area_id, actor, {
            "wind_level": wind,
        })
        return updated

    # ---- 风级 ----
    def report_wind(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WIND_ROLES)
        actor = require_text(actor, "actor", 100)
        level = normalize_wind_level(payload.get("level"))
        source = require_text(payload.get("source", ""), "source", 100) if payload.get("source") else ""
        previous = self.repository.latest_wind_level()
        report = self.repository.add_wind_report(level, source, actor)
        result: Dict[str, Any] = {"report": report, "recalculated": 0, "reverted_signoffs": 0}
        if level != previous:
            # 风级跃变：未开工派工随新条件重算；已签认的关闭签认作废需重新签认；
            # 进行中/已完成派工保留各自过程，不动。
            recalc = recalculated_dispatch(level)
            recalculated = self.repository.recalculate_pending_dispatches(
                recalc["wind_level"], recalc["priority"])
            reverted = self.repository.revert_signed_task_areas()
            result["recalculated"] = recalculated
            result["reverted_signoffs"] = reverted
            self.repository.append_audit("wind_change", "wind", report["id"], actor, {
                "from": previous, "to": level,
                "recalculated": recalculated, "reverted_signoffs": reverted,
            })
        else:
            self.repository.append_audit("wind_report", "wind", report["id"], actor, {
                "level": level,
            })
        return result

    # ---- 派工 ----
    def create_dispatch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        order_no = require_text(payload.get("order_no"), "order_no", 100)
        member_id = int(require_number(payload.get("member_id"), "member_id", 1))
        task_area_id = int(require_number(payload.get("task_area_id"), "task_area_id", 1))
        breakpoint = require_text(payload.get("breakpoint"), "breakpoint", 200)
        # 校验队员与任务区存在
        self.repository.get_member(member_id)
        self.repository.get_task_area(task_area_id)
        wind = self.repository.latest_wind_level()
        priority = dispatch_priority(wind)
        # 断网补报：按单号合并，重试一次只算一次
        existing = self.repository.get_dispatch_by_order_no(order_no)
        if existing is not None:
            result = dict(existing)
            result["idempotent"] = True
            return result
        # 两个入口同时提交同一队员：只留先到占用，后到转待协调
        if self.repository.member_has_active_dispatch(member_id):
            status = "pending_coordination"
            occupied = False
        else:
            status = "pending"
            occupied = True
        dispatch = self.repository.create_dispatch(
            order_no, member_id, task_area_id, breakpoint, wind, priority, status, actor)
        self.repository.append_audit("dispatch", "dispatch", dispatch["id"], actor, {
            "order_no": order_no, "member_id": member_id, "task_area_id": task_area_id,
            "breakpoint": breakpoint, "wind_level": wind, "status": status,
            "occupied": occupied,
        })
        result = dict(dispatch)
        result["idempotent"] = False
        return result

    def list_dispatches(self, role: str, status: Optional[str] = None,
                        task_area_id: Optional[int] = None) -> list:
        self._view(role)
        return self.repository.list_dispatches(status, task_area_id)

    def get_dispatch(self, dispatch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_dispatch(dispatch_id)

    def start_dispatch(self, dispatch_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        dispatch = self.repository.get_dispatch(dispatch_id)
        if dispatch["status"] != "pending":
            from .domain import ConflictError
            raise ConflictError("只有未开工的派工可以开工")
        updated = self.repository.update_dispatch_status(dispatch_id, "in_progress")
        self.repository.append_audit("dispatch_start", "dispatch", dispatch_id, actor, {
            "order_no": dispatch["order_no"],
        })
        return updated

    def complete_dispatch(self, dispatch_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        dispatch = self.repository.get_dispatch(dispatch_id)
        if dispatch["status"] not in ("pending", "in_progress"):
            from .domain import ConflictError
            raise ConflictError("只有未开工或进行中的派工可以完工")
        updated = self.repository.update_dispatch_status(dispatch_id, "completed")
        self.repository.append_audit("dispatch_complete", "dispatch", dispatch_id, actor, {
            "order_no": dispatch["order_no"],
        })
        return updated

    def resolve_dispatch(self, dispatch_id: int, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        """处理待协调派工：cancel 撤回（释放待协调占用），activate 转为正式派工。"""
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        decision = require_text(payload.get("decision"), "decision", 20)
        dispatch = self.repository.get_dispatch(dispatch_id)
        if dispatch["status"] != "pending_coordination":
            from .domain import ConflictError
            raise ConflictError("只有待协调的派工需要处理")
        if decision == "cancel":
            updated = self.repository.update_dispatch_status(dispatch_id, "cancelled")
            self.repository.append_audit("dispatch_cancel", "dispatch", dispatch_id, actor, {
                "order_no": dispatch["order_no"],
            })
            return updated
        if decision == "activate":
            if self.repository.member_has_active_dispatch(dispatch["member_id"]):
                from .domain import ConflictError
                raise ConflictError("队员仍被其他派工占用，无法激活")
            wind = self.repository.latest_wind_level()
            updated = self.repository.activate_pending_coordination(
                dispatch_id, wind, dispatch_priority(wind))
            self.repository.append_audit("dispatch_activate", "dispatch", dispatch_id, actor, {
                "order_no": dispatch["order_no"], "wind_level": wind,
            })
            return updated
        from .domain import ValidationError
        raise ValidationError("decision必须是cancel或activate")

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
