import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError
from src.rules import BERTH_PENDING, DomainRules, TRANSIT_RELEASED


def plan(**overrides):
    data = {
        'vessel': 'HaiYun', 'berth': 'B12',
        'vessel_length_m': 180, 'berth_length_m': 220,
        'draft_m': 9.0, 'berth_depth_m': 12.0,
        'eta_hour': 8, 'etd_hour': 18,
        'risk_level': 'low', 'dangerous_goods': False, 'dangerous_class': '',
        'transit_start_hour': 6, 'transit_end_hour': 8,
    }
    data.update(overrides)
    return data


class ChannelRuleTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_deep_draft_ebb_tide_rejected(self):
        # 深吃水船：10.2米，通航窗6-8只有7是涨潮小时
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(plan(draft_m=10.2, transit_start_hour=6, transit_end_hour=8))

    def test_deep_draft_flood_tide_registered(self):
        prepared = self.rules.prepare_create(plan(draft_m=10.2, transit_start_hour=2, transit_end_hour=6, eta_hour=6))
        self.assertTrue(prepared["flood_tide_required"])
        self.assertFalse(prepared["transit_exclusive"])

    def test_dangerous_vessel_is_exclusive(self):
        prepared = self.rules.prepare_create(plan(dangerous_goods=True, dangerous_class='3'))
        self.assertTrue(prepared["transit_exclusive"])

    def test_overlength_vessel_is_exclusive(self):
        prepared = self.rules.prepare_create(plan(vessel_length_m=240, berth_length_m=260))
        self.assertTrue(prepared["transit_exclusive"])

    def test_normal_vessel_shall_not_block_parallel(self):
        records = [
            {"id": 2, "state": "confirmed", "payload": self.rules.prepare_create(plan(berth='B13'))},
        ]
        incoming = self.rules.prepare_create(plan(berth='B14'))
        self.rules.check_channel_conflicts(incoming, records)  # 普通船可并排，不抛异常

    def test_exclusive_window_rejects_overlap(self):
        dangerous = self.rules.prepare_create(
            plan(berth='B13', dangerous_goods=True, dangerous_class='3')
        )
        records = [{"id": 2, "state": "confirmed", "payload": dangerous}]
        incoming = self.rules.prepare_create(plan(berth='B14'))
        with self.assertRaises(Conflict):
            self.rules.check_channel_conflicts(incoming, records)

    def test_non_overlapping_exclusive_windows_ok(self):
        dangerous = self.rules.prepare_create(
            plan(berth='B13', dangerous_goods=True, dangerous_class='3',
                 transit_start_hour=6, transit_end_hour=8)
        )
        records = [{"id": 2, "state": "confirmed", "payload": dangerous}]
        incoming = self.rules.prepare_create(plan(berth='B14', transit_start_hour=8, transit_end_hour=10))
        self.rules.check_channel_conflicts(incoming, records)

    def test_late_transit_marks_both_pending(self):
        prepared = self.rules.prepare_create(
            plan(draft_m=10.2, transit_start_hour=14, transit_end_hour=18, eta_hour=12, etd_hour=20)
        )
        self.assertTrue(prepared["reschedule_required"])
        self.assertEqual(prepared["transit_status"], "pending_reschedule")
        self.assertEqual(prepared["berth_status"], BERTH_PENDING)

    def test_pending_plan_does_not_consume_channel(self):
        late = self.rules.prepare_create(
            plan(berth='B13', draft_m=10.2, transit_start_hour=14,
                 transit_end_hour=18, eta_hour=12, etd_hour=20)
        )
        records = [{"id": 2, "state": "draft", "payload": late}]
        incoming = self.rules.prepare_create(
            plan(berth='B14', transit_start_hour=15, transit_end_hour=17)
        )
        self.rules.check_channel_conflicts(incoming, records)  # 待重排记录不占航道


class ChannelWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.actor = Actor("creator", "port_controller")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, ref, **overrides):
        return self.service.create(self.actor, ref, plan(**overrides))

    def test_parallel_normal_vessels_allowed(self):
        first = self._create("VOY-C1", berth='B12')
        second = self._create("VOY-C2", berth='B13')
        self.assertEqual(second["payload"]["transit_status"], "registered")

    def test_dangerous_exclusive_slot_conflict(self):
        self._create("VOY-C3", berth='B12', dangerous_goods=True, dangerous_class='3')
        with self.assertRaises(Conflict):
            self._create("VOY-C4", berth='B13')

    def test_overlength_exclusive_slot_conflict(self):
        self._create("VOY-C5", berth='B12', vessel_length_m=230, berth_length_m=260)
        with self.assertRaises(Conflict):
            self._create("VOY-C6", berth='B13')

    def test_departed_vessel_releases_slot(self):
        dangerous = plan(berth='B12', dangerous_goods=True, dangerous_class='3')
        first = self._create("VOY-C7", **dangerous)
        first = self.service.act(self.actor, first["id"], first["version"], 'confirm', {'pilot_id': 'P-9'})
        first = self.service.act(self.actor, first["id"], first["version"], 'berth', {'actual_draft_m': 9.0})
        first = self.service.act(
            self.actor, first["id"], first["version"], 'depart', {'cargo_operation_complete': True}
        )
        self.assertEqual(first["payload"]["transit_status"], TRANSIT_RELEASED)
        # 让出后，后船可使用同一通航时段
        second = self._create("VOY-C8", berth='B13')
        self.assertEqual(second["payload"]["transit_status"], "registered")

    def test_late_plan_blocked_until_rescheduled(self):
        record = self._create(
            "VOY-C9", draft_m=10.2, transit_start_hour=14,
            transit_end_hour=18, eta_hour=12, etd_hour=20,
        )
        self.assertEqual(record["payload"]["berth_status"], BERTH_PENDING)
        with self.assertRaises(Conflict):
            self.service.act(self.actor, record["id"], record["version"], 'confirm', {'pilot_id': 'P-1'})
        # 重排到涨潮窗2-4，早于eta=12，恢复编排
        record = self.service.act(
            self.actor, record["id"], record["version"], 'reschedule',
            {'transit_start_hour': 2, 'transit_end_hour': 4},
        )
        self.assertEqual(record["payload"]["transit_status"], "registered")
        self.assertEqual(record["payload"]["berth_status"], "scheduled")
        record = self.service.act(self.actor, record["id"], record["version"], 'confirm', {'pilot_id': 'P-1'})
        self.assertEqual(record["state"], "confirmed")

    def test_reschedule_into_exclusive_slot_conflict(self):
        self._create(
            "VOY-C10", berth='B12', dangerous_goods=True, dangerous_class='3',
            transit_start_hour=6, transit_end_hour=8,
        )
        record = self._create(
            "VOY-C11", berth='B13', eta_hour=12, etd_hour=20,
            transit_start_hour=9, transit_end_hour=11,
        )
        with self.assertRaises(Conflict):
            self.service.act(
                self.actor, record["id"], record["version"], 'reschedule',
                {'transit_start_hour': 7, 'transit_end_hour': 9},
            )
        untouched = self.service.get_record(self.actor, record["id"])
        self.assertEqual(untouched["payload"]["transit_start_hour"], 9)
        self.assertEqual(untouched["version"], record["version"])

    def test_reschedule_still_validates_tide(self):
        record = self._create(
            "VOY-C12", draft_m=10.2, transit_start_hour=2,
            transit_end_hour=6, eta_hour=8, etd_hour=20,
        )
        with self.assertRaises(ValidationError):
            self.service.act(
                self.actor, record["id"], record["version"], 'reschedule',
                {'transit_start_hour': 9, 'transit_end_hour': 11},
            )


if __name__ == "__main__":
    unittest.main()
