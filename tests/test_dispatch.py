import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "山火事件", "description": "火场撤离调度", "severity": "high",
             "quantity": 12, "threshold": 6, "external_ref": "WF-D1"},
            "creator", "field_commander")
        self.zone = self.service.create_zone(
            self.item["id"], {"name": "东坡任务区"}, "commander", "incident_commander")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _dispatch(self, order_no, members, wind=5):
        return self.service.create_dispatch(
            self.zone["id"],
            {"order_no": order_no,
             "breakpoints": [[31.2, 121.5], [31.3, 121.6]],
             "wind_level": wind, "members": members},
            "dispatcher", "field_commander")

    def _wind(self, report_no, level):
        return self.service.submit_report(
            self.zone["id"], {"kind": "wind", "report_no": report_no,
                              "wind_level": level}, "uav", "field_commander")

    def test_dispatch_records_breakpoints_wind_members_and_zone(self):
        result = self._dispatch("D-1", ["张三", "李四"])
        self.assertFalse(result["merged"])
        dispatch = result["dispatch"]
        self.assertEqual(dispatch["zone_id"], self.zone["id"])
        self.assertEqual(dispatch["status"], "planned")
        self.assertEqual(dispatch["wind_level"], 5)
        self.assertEqual(dispatch["breakpoints"],
                         [{"lat": 31.2, "lng": 121.5}, {"lat": 31.3, "lng": 121.6}])
        self.assertEqual(result["members"], [
            {"member_name": "张三", "status": "occupied"},
            {"member_name": "李四", "status": "occupied"}])
        self.assertEqual(dispatch["plan"]["wind_level"], 5)
        self.assertEqual(dispatch["plan"]["breakpoint_count"], 2)
        detail = self.service.get_dispatch(dispatch["id"], "viewer")
        self.assertEqual(len(detail["plans"]), 1)
        self.assertEqual(detail["plans"][0]["reason"], "initial")

    def test_first_arrival_occupies_and_later_goes_pending(self):
        first = self._dispatch("D-1", ["张三"])
        second = self._dispatch("D-2", ["张三", "王五"])
        self.assertEqual(first["members"][0]["status"], "occupied")
        statuses = {m["member_name"]: m["status"] for m in second["members"]}
        self.assertEqual(statuses["张三"], "pending_coordination")
        self.assertEqual(statuses["王五"], "occupied")

    def test_concurrent_entries_keep_only_first_occupation(self):
        results = []
        barrier = threading.Barrier(2)

        def submit(order_no):
            barrier.wait()
            results.append(self._dispatch(order_no, ["张三"]))

        threads = [threading.Thread(target=submit, args=(f"D-{i}",))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        statuses = sorted(r["members"][0]["status"] for r in results)
        self.assertEqual(statuses, ["occupied", "pending_coordination"])

    def test_offline_retry_merged_by_order_no_counts_once(self):
        first = self._dispatch("D-1", ["张三"])
        retry = self._dispatch("D-1", ["李四", "王五"])
        self.assertTrue(retry["merged"])
        self.assertEqual(retry["dispatch"]["id"], first["dispatch"]["id"])
        dispatches = self.service.list_dispatches(self.zone["id"], "viewer")
        self.assertEqual(len(dispatches), 1)
        members = self.service.list_dispatch_members(first["dispatch"]["id"])
        self.assertEqual([m["member_name"] for m in members], ["张三"])

    def test_report_and_delivery_retry_count_once(self):
        self._dispatch("D-1", ["张三"], wind=5)
        wind = self._wind("W-1", 9)
        self.assertTrue(wind["changed"])
        retry = self._wind("W-1", 9)
        self.assertTrue(retry["merged"])
        self.assertEqual(retry["recalculated"], [])
        material = self.service.upsert_material(
            self.zone["id"], {"name": "水带", "required_qty": 10},
            "logi", "logistics")
        delivered = self.service.deliver_material(
            self.zone["id"], material["id"], {"qty": 6, "report_no": "M-1"},
            "logi", "logistics")
        self.assertFalse(delivered["merged"])
        again = self.service.deliver_material(
            self.zone["id"], material["id"], {"qty": 6, "report_no": "M-1"},
            "logi", "logistics")
        self.assertTrue(again["merged"])
        self.assertEqual(again["material"]["delivered_qty"], 6)

    def test_wind_change_recalculates_planned_dispatches(self):
        created = self._dispatch("D-1", ["张三", "李四"], wind=5)
        dispatch_id = created["dispatch"]["id"]
        old_plan = created["dispatch"]["plan"]
        result = self._wind("W-1", 9)
        self.assertEqual(result["recalculated"], [dispatch_id])
        updated = self.service.get_dispatch(dispatch_id, "viewer")
        self.assertEqual(updated["wind_level"], 9)
        self.assertLess(updated["plan"]["egress_window_minutes"],
                        old_plan["egress_window_minutes"])
        self.assertEqual([p["reason"] for p in updated["plans"]],
                         ["initial", "wind_change"])
        same = self._wind("W-2", 9)
        self.assertFalse(same["changed"])
        self.assertEqual(same["recalculated"], [])

    def test_wind_change_reconfirms_closed_and_keeps_completed(self):
        closed = self._dispatch("D-1", ["张三"], wind=5)["dispatch"]
        completed = self._dispatch("D-2", ["李四"], wind=5)["dispatch"]
        for dispatch, target in [(closed, "closed"), (completed, "completed")]:
            current = dispatch
            steps = {"closed": ["in_progress", "completed", "closed"],
                     "completed": ["in_progress", "completed"]}[target]
            for step in steps:
                role = "incident_commander" if step == "closed" else "field_commander"
                current = self.service.transition_dispatch(
                    current["id"], {"target": step,
                                    "expected_version": current["version"]},
                    "actor", role)
        completed_plan = self.service.get_dispatch(completed["id"], "viewer")["plan"]
        result = self._wind("W-1", 10)
        self.assertEqual(sorted(result["reconfirm"]), [closed["id"]])
        reopened = self.service.get_dispatch(closed["id"], "viewer")
        self.assertEqual(reopened["status"], "completed")
        kept = self.service.get_dispatch(completed["id"], "viewer")
        self.assertEqual(kept["status"], "completed")
        self.assertEqual(kept["plan"], completed_plan)
        self.assertEqual([p["reason"] for p in kept["plans"]], ["initial"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_closure_checklist_lists_blockers_then_close(self):
        self._dispatch("D-1", ["张三", "李四"])
        self._dispatch("D-2", ["张三"])
        material = self.service.upsert_material(
            self.zone["id"], {"name": "水带", "required_qty": 10},
            "logi", "logistics")
        self.service.deliver_material(
            self.zone["id"], material["id"], {"qty": 6, "report_no": "M-1"},
            "logi", "logistics")
        checklist = self.service.closure_checklist(self.zone["id"], "viewer")
        self.assertFalse(checklist["clear"])
        self.assertEqual(sorted(m["member_name"]
                                for m in checklist["unreturned_members"]),
                         ["张三", "李四"])
        self.assertEqual(checklist["incomplete_materials"][0]["missing_qty"], 4)
        self.assertEqual([m["member_name"]
                          for m in checklist["pending_coordinations"]], ["张三"])
        zone = self.service.get_zone(self.zone["id"], "viewer")
        with self.assertRaises(ConflictError) as ctx:
            self.service.close_zone(
                self.zone["id"], {"expected_version": zone["version"]},
                "commander", "incident_commander")
        message = str(ctx.exception)
        self.assertIn("未归队队员", message)
        self.assertIn("未齐物资", message)
        self.assertIn("待协调占用", message)
        for name in ("张三", "李四"):
            self.service.submit_report(
                self.zone["id"],
                {"kind": "location", "report_no": f"L-{name}",
                 "member_name": name, "returned": True, "position": "集结点"},
                "patrol", "field_commander")
        self.service.deliver_material(
            self.zone["id"], material["id"], {"qty": 4, "report_no": "M-2"},
            "logi", "logistics")
        pending_id = checklist["pending_coordinations"][0]["member_id"]
        dispatch_id = checklist["pending_coordinations"][0]["dispatch_id"]
        self.service.resolve_member(
            dispatch_id, pending_id, {"action": "release"},
            "commander", "incident_commander")
        checklist = self.service.closure_checklist(self.zone["id"], "viewer")
        self.assertTrue(checklist["clear"])
        zone = self.service.get_zone(self.zone["id"], "viewer")
        closed = self.service.close_zone(
            self.zone["id"], {"expected_version": zone["version"]},
            "commander", "incident_commander")
        self.assertEqual(closed["status"], "closed")
        with self.assertRaises(ConflictError):
            self._dispatch("D-3", ["王五"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_resolve_promote_after_release(self):
        first = self._dispatch("D-1", ["张三"])
        second = self._dispatch("D-2", ["张三"])
        occupied_id = self.service.list_dispatch_members(
            first["dispatch"]["id"])[0]["id"]
        pending_id = self.service.list_dispatch_members(
            second["dispatch"]["id"])[0]["id"]
        with self.assertRaises(ConflictError):
            self.service.resolve_member(
                second["dispatch"]["id"], pending_id, {"action": "promote"},
                "commander", "incident_commander")
        self.service.resolve_member(
            first["dispatch"]["id"], occupied_id, {"action": "release"},
            "commander", "incident_commander")
        promoted = self.service.resolve_member(
            second["dispatch"]["id"], pending_id, {"action": "promote"},
            "commander", "incident_commander")
        self.assertEqual(promoted["status"], "occupied")

    def test_permissions_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_dispatch(
                self.zone["id"], {"order_no": "D-9", "breakpoints": [[1, 1]],
                                  "members": ["张三"]}, "spy", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.create_zone(self.item["id"], {"name": "西区"},
                                     "spy", "viewer")
        with self.assertRaises(ValidationError):
            self.service.create_dispatch(
                self.zone["id"], {"order_no": "D-9", "breakpoints": [[1, 1]],
                                  "wind_level": 99, "members": ["张三"]},
                "dispatcher", "field_commander")
        with self.assertRaises(ValidationError):
            self.service.submit_report(
                self.zone["id"], {"kind": "wind", "report_no": "W-9"},
                "uav", "field_commander")
        dispatch = self._dispatch("D-1", ["张三"])["dispatch"]
        with self.assertRaises(PermissionDenied):
            self.service.transition_dispatch(
                dispatch["id"], {"target": "in_progress", "expected_version": 1},
                "spy", "viewer")
        with self.assertRaises(ConflictError):
            self.service.transition_dispatch(
                dispatch["id"], {"target": "in_progress", "expected_version": 99},
                "dispatcher", "field_commander")
        with self.assertRaises(ConflictError):
            self.service.transition_dispatch(
                dispatch["id"], {"target": "closed", "expected_version": 1},
                "commander", "incident_commander")


if __name__ == "__main__":
    unittest.main()
