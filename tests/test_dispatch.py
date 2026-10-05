import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.svc = Service(self.repo)
        self.m1 = self.svc.create_member({"name": "张三", "team": "巡火一队"}, "cmd", "field_commander")
        self.m2 = self.svc.create_member({"name": "李四", "team": "巡火二队"}, "cmd", "field_commander")
        self.area = self.svc.create_task_area({"name": "东坡火线", "description": "撤离任务区"}, "cmd", "field_commander")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _dispatch(self, order_no, member, breakpoint="折点A-7"):
        return self.svc.create_dispatch({
            "order_no": order_no, "member_id": member["id"],
            "task_area_id": self.area["id"], "breakpoint": breakpoint,
        }, "cmd", "field_commander")

    def test_dispatch_records_breakpoint_wind_member_area(self):
        self.svc.report_wind({"level": 3, "source": "无人机"}, "cmd", "field_commander")
        d = self._dispatch("DD-001", self.m1)
        self.assertEqual(d["breakpoint"], "折点A-7")
        self.assertEqual(d["wind_level"], 3)
        self.assertEqual(d["member_id"], self.m1["id"])
        self.assertEqual(d["task_area_id"], self.area["id"])
        self.assertEqual(d["status"], "pending")
        self.assertIn("priority", d)

    def test_duplicate_member_first_occupies_later_pending_coordination(self):
        d1 = self._dispatch("DD-001", self.m1)
        self.assertEqual(d1["status"], "pending")  # 先到占用
        d2 = self._dispatch("DD-002", self.m1)
        self.assertEqual(d2["status"], "pending_coordination")  # 后到转待协调
        # 待协调单不占用：再给该队员派一单，仍转待协调而非占用
        d3 = self._dispatch("DD-003", self.m1)
        self.assertEqual(d3["status"], "pending_coordination")
        # 另一队员不受影响，正常占用
        d4 = self._dispatch("DD-004", self.m2)
        self.assertEqual(d4["status"], "pending")

    def test_offline_merge_by_order_no_retry_counts_once(self):
        d1 = self._dispatch("DD-001", self.m1)
        # 断网补报：同一单号重试，按单号合并，返回已有派工，不新增
        retry = self._dispatch("DD-001", self.m1)
        self.assertTrue(retry["idempotent"])
        self.assertEqual(retry["id"], d1["id"])
        self.assertEqual(len(self.svc.list_dispatches("viewer")), 1)
        # 重试不占用新名额：换个队员用同一单号补报，仍合并到原单
        retry2 = self._dispatch("DD-001", self.m2)
        self.assertEqual(retry2["id"], d1["id"])
        self.assertEqual(len(self.svc.list_dispatches("viewer")), 1)

    def test_wind_change_recalculates_pending_keeps_completed(self):
        self.svc.report_wind({"level": 3}, "cmd", "field_commander")
        d1 = self._dispatch("DD-001", self.m1)   # 未开工
        d2 = self._dispatch("DD-002", self.m1)   # 待协调
        d3 = self._dispatch("DD-003", self.m2)   # 未开工
        # d1 开工后完工，成为已完成
        self.svc.complete_dispatch(d1["id"], "cmd", "field_commander")
        # 风级跃变 3 -> 8
        result = self.svc.report_wind({"level": 8, "source": "巡火队"}, "cmd", "field_commander")
        self.assertEqual(result["recalculated"], 1)  # 只有 d3 一张未开工
        d3b = self.svc.get_dispatch(d3["id"], "viewer")
        self.assertEqual(d3b["wind_level"], 8)
        self.assertEqual(d3b["priority"], 8)
        # 待协调单保留原风级快照，不随重算
        d2b = self.svc.get_dispatch(d2["id"], "viewer")
        self.assertEqual(d2b["wind_level"], 3)
        # 已完成行动保留过程（风级3快照）
        d1b = self.svc.get_dispatch(d1["id"], "viewer")
        self.assertEqual(d1b["wind_level"], 3)
        self.assertEqual(d1b["status"], "completed")

    def test_close_requires_resign_after_wind_change(self):
        self.svc.report_wind({"level": 3}, "cmd", "field_commander")
        d1 = self._dispatch("DD-001", self.m1)
        self.svc.complete_dispatch(d1["id"], "cmd", "field_commander")
        self.svc.return_member(self.m1["id"], "cmd", "field_commander")
        self.svc.sign_off_task_area(self.area["id"], "cmd", "incident_commander")
        # 签认后风级变化，确认关闭需重新签认
        self.svc.report_wind({"level": 9}, "cmd", "field_commander")
        with self.assertRaises(ConflictError):
            self.svc.confirm_close_task_area(self.area["id"], "cmd", "incident_commander")
        # 重新签认后可确认
        area = self.svc.get_task_area(self.area["id"], "viewer")
        self.assertEqual(area["status"], "open")  # 签认已作废
        self.svc.sign_off_task_area(self.area["id"], "cmd", "incident_commander")
        closed = self.svc.confirm_close_task_area(self.area["id"], "cmd", "incident_commander")
        self.assertEqual(closed["status"], "closed")

    def test_close_checklist_lists_and_blocks(self):
        # 未归队队员：m1 在活动派工上
        d1 = self._dispatch("DD-001", self.m1)
        # 待协调占用：m1 的第二单
        d2 = self._dispatch("DD-002", self.m1)
        # 未齐物资：required 3, fulfilled 0
        self.svc.add_supply(self.area["id"], {"name": "急救包", "required_qty": 3}, "cmd", "logistics")
        checklist = self.svc.task_area_checklist(self.area["id"], "viewer")
        self.assertEqual(len(checklist["unreturned_members"]), 1)
        self.assertEqual(checklist["unreturned_members"][0]["name"], "张三")
        self.assertEqual(len(checklist["incomplete_supplies"]), 1)
        self.assertEqual(len(checklist["pending_coordination"]), 1)
        # 三类未清项阻止签认
        with self.assertRaises(ConflictError) as ctx:
            self.svc.sign_off_task_area(self.area["id"], "cmd", "incident_commander")
        msg = str(ctx.exception)
        self.assertIn("未归队队员", msg)
        self.assertIn("未齐物资", msg)
        self.assertIn("待协调占用", msg)

    def test_resolve_pending_coordination_clears_checklist(self):
        d1 = self._dispatch("DD-001", self.m1)
        d2 = self._dispatch("DD-002", self.m1)  # 待协调
        # 取消待协调单
        self.svc.resolve_dispatch(d2["id"], {"decision": "cancel"}, "cmd", "field_commander")
        # 完工 d1、归队 m1、补齐物资
        self.svc.complete_dispatch(d1["id"], "cmd", "field_commander")
        self.svc.return_member(self.m1["id"], "cmd", "field_commander")
        s = self.svc.add_supply(self.area["id"], {"name": "对讲机", "required_qty": 1}, "cmd", "logistics")
        self.svc.fulfill_supply(s["id"], {"qty": 1}, "cmd", "logistics")
        checklist = self.svc.task_area_checklist(self.area["id"], "viewer")
        self.assertEqual(len(checklist["unreturned_members"]), 0)
        self.assertEqual(len(checklist["incomplete_supplies"]), 0)
        self.assertEqual(len(checklist["pending_coordination"]), 0)
        # 列清后可签认并关闭
        self.svc.sign_off_task_area(self.area["id"], "cmd", "incident_commander")
        closed = self.svc.confirm_close_task_area(self.area["id"], "cmd", "incident_commander")
        self.assertEqual(closed["status"], "closed")

    def test_activate_pending_coordination(self):
        d1 = self._dispatch("DD-001", self.m1)
        d2 = self._dispatch("DD-002", self.m1)  # 待协调
        # m1 仍被占用时无法激活
        with self.assertRaises(ConflictError):
            self.svc.resolve_dispatch(d2["id"], {"decision": "activate"}, "cmd", "field_commander")
        # d1 完工后 m1 空闲，激活 d2 为正式派工
        self.svc.complete_dispatch(d1["id"], "cmd", "field_commander")
        activated = self.svc.resolve_dispatch(d2["id"], {"decision": "activate"}, "cmd", "field_commander")
        self.assertEqual(activated["status"], "pending")
        # 激活后该队员又被占用
        d3 = self._dispatch("DD-003", self.m1)
        self.assertEqual(d3["status"], "pending_coordination")

    def test_viewer_cannot_create_dispatch(self):
        with self.assertRaises(PermissionDenied):
            self.svc.create_dispatch({
                "order_no": "X-1", "member_id": self.m1["id"],
                "task_area_id": self.area["id"], "breakpoint": "K",
            }, "viewer", "viewer")

    def test_audit_chain_intact(self):
        self.svc.report_wind({"level": 4}, "cmd", "field_commander")
        self._dispatch("DD-001", self.m1)
        self._dispatch("DD-002", self.m1)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
