from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_breakpoints, normalize_severity, require_name_list,
                     require_number, require_text, require_wind_level)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DISPATCH_CREATE_ROLES,
                    DISPATCH_ENTITY, ENTITY, MATERIAL_ROLES, RECORD_ROLES,
                    REPORT_KINDS, REPORT_ROLES, RESOLVE_ROLES, TITLE,
                    VIEW_ROLES, ZONE_CLOSE_ROLES, ZONE_CREATE_ROLES, ZONE_ENTITY,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_dispatch_transition,
                    role_for_transition, validate_dispatch_transition,
                    validate_transition)


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

    # ---- 火场撤离调度 ----
    def create_zone(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, ZONE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        zone = self.repository.create_zone(item_id, name, external_ref, actor)
        self.repository.append_audit("zone_create", ZONE_ENTITY, zone["id"], actor, {
            "item_id": item_id, "name": name,
        })
        return zone

    def list_zones(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_zones(item_id)

    def get_zone(self, zone_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        zone = self.repository.get_zone(zone_id)
        zone["closure"] = self.repository.closure_checklist(zone_id)
        return zone

    def submit_report(self, zone_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, REPORT_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = payload.get("kind")
        if kind not in REPORT_KINDS:
            raise ValidationError("kind必须是wind、breakpoints或location")
        report_no = require_text(payload.get("report_no"), "report_no", 100)
        if kind == "wind":
            wind_level = require_wind_level(payload.get("wind_level"))
            result = self.repository.submit_wind_report(
                zone_id, report_no, wind_level, actor)
            if not result["merged"]:
                self.repository.append_audit("report_wind", ZONE_ENTITY, zone_id,
                                             actor, {
                    "report_no": report_no, "wind_level": wind_level,
                    "changed": result["changed"],
                    "recalculated": result["recalculated"],
                    "reconfirm": result["reconfirm"],
                })
                for dispatch_id in result["recalculated"]:
                    self.repository.append_audit(
                        "dispatch_recalc", DISPATCH_ENTITY, dispatch_id, actor,
                        {"reason": "wind_change", "wind_level": wind_level})
                for dispatch_id in result["reconfirm"]:
                    self.repository.append_audit(
                        "dispatch_reconfirm", DISPATCH_ENTITY, dispatch_id, actor,
                        {"reason": "wind_change", "wind_level": wind_level,
                         "status": "completed"})
            return result
        if kind == "breakpoints":
            points = normalize_breakpoints(payload.get("breakpoints"))
            result = self.repository.submit_breakpoints_report(
                zone_id, report_no, points, actor)
            if not result["merged"]:
                self.repository.append_audit("report_breakpoints", ZONE_ENTITY,
                                             zone_id, actor, {
                    "report_no": report_no, "point_count": len(points),
                })
            return result
        member_name = require_text(payload.get("member_name"), "member_name", 100)
        returned = payload.get("returned", False)
        if not isinstance(returned, bool):
            raise ValidationError("returned必须是布尔值")
        position = payload.get("position", "")
        if position is None:
            position = ""
        position = str(position).strip()
        if len(position) > 500:
            raise ValidationError("position不能超过500个字符")
        result = self.repository.submit_location_report(
            zone_id, report_no, member_name, returned, position, actor)
        if not result["merged"]:
            self.repository.append_audit("report_location", ZONE_ENTITY, zone_id,
                                         actor, {
                "report_no": report_no, "member_name": member_name,
                "returned": returned, "returned_count": result["returned_count"],
            })
        return result

    def create_dispatch(self, zone_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        order_no = require_text(payload.get("order_no"), "order_no", 100)
        breakpoints = normalize_breakpoints(payload.get("breakpoints"))
        wind_raw = payload.get("wind_level")
        wind_level = None if wind_raw is None else require_wind_level(wind_raw)
        note = payload.get("note", "")
        if note is None:
            note = ""
        note = str(note).strip()
        if len(note) > 2000:
            raise ValidationError("note不能超过2000个字符")
        members = require_name_list(payload.get("members"))
        result = self.repository.create_dispatch(
            zone_id, order_no, breakpoints, wind_level, note, members, actor)
        dispatch = result["dispatch"]
        if result["merged"]:
            result["members"] = self.repository.list_dispatch_members(dispatch["id"])
            return result
        self.repository.append_audit("dispatch_create", DISPATCH_ENTITY,
                                     dispatch["id"], actor, {
            "zone_id": zone_id, "order_no": order_no,
            "wind_level": dispatch["wind_level"],
            "breakpoint_count": len(breakpoints),
            "members": result["members"],
        })
        return result

    def get_dispatch(self, dispatch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        dispatch = self.repository.get_dispatch(dispatch_id)
        dispatch["members"] = self.repository.list_dispatch_members(dispatch_id)
        dispatch["plans"] = self.repository.list_dispatch_plans(dispatch_id)
        return dispatch

    def list_dispatches(self, zone_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_dispatches(zone_id)

    def list_dispatch_members(self, dispatch_id: int, role: str = "viewer") -> list:
        self._view(role)
        return self.repository.list_dispatch_members(dispatch_id)

    def transition_dispatch(self, dispatch_id: int, payload: Dict[str, Any],
                            actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        target = payload.get("target")
        expected = payload.get("expected_version")
        dispatch = self.repository.get_dispatch(dispatch_id)
        validate_dispatch_transition(dispatch["status"], target)
        ensure_role(role, role_for_dispatch_transition(target))
        if not isinstance(expected, int) or expected < 1:
            raise ValidationError("expected_version必须是正整数")
        updated = self.repository.transition_dispatch(
            dispatch_id, target, expected, actor)
        self.repository.append_audit("dispatch_transition", DISPATCH_ENTITY,
                                     dispatch_id, actor, {
            "from": dispatch["status"], "to": target,
        })
        return updated

    def resolve_member(self, dispatch_id: int, member_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RESOLVE_ROLES)
        actor = require_text(actor, "actor", 100)
        action = payload.get("action")
        if action not in ("release", "promote"):
            raise ValidationError("action必须是release或promote")
        member = self.repository.resolve_member(dispatch_id, member_id, action, actor)
        self.repository.append_audit("member_resolve", DISPATCH_ENTITY, dispatch_id,
                                     actor, {
            "member_id": member_id, "member_name": member["member_name"],
            "action": action, "status": member["status"],
        })
        return member

    def upsert_material(self, zone_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, MATERIAL_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        required_qty = require_number(payload.get("required_qty", 0), "required_qty")
        material = self.repository.upsert_material(zone_id, name, required_qty, actor)
        self.repository.append_audit("material_upsert", ZONE_ENTITY, zone_id, actor, {
            "material_id": material["id"], "name": name,
            "required_qty": required_qty,
        })
        return material

    def list_materials(self, zone_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_materials(zone_id)

    def deliver_material(self, zone_id: int, material_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, MATERIAL_ROLES)
        actor = require_text(actor, "actor", 100)
        qty = require_number(payload.get("qty"), "qty", 0.000001)
        report_no = require_text(payload.get("report_no"), "report_no", 100)
        result = self.repository.deliver_material(
            zone_id, material_id, qty, report_no, actor)
        if not result["merged"]:
            self.repository.append_audit("material_deliver", ZONE_ENTITY, zone_id,
                                         actor, {
                "material_id": material_id, "qty": qty, "report_no": report_no,
            })
        return result

    def closure_checklist(self, zone_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.closure_checklist(zone_id)

    def close_zone(self, zone_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, ZONE_CLOSE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or expected < 1:
            raise ValidationError("expected_version必须是正整数")
        zone = self.repository.close_zone(zone_id, expected, actor)
        self.repository.append_audit("zone_close", ZONE_ENTITY, zone_id, actor, {
            "item_id": zone["item_id"], "name": zone["name"],
        })
        return zone
