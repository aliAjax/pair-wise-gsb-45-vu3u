import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError
from src.rules import DEFAULT_EXCLUSIVE_LENGTH_M, DEFAULT_FLOOD_WINDOWS, DEFAULT_TIDE_SAFE_DRAFT_M, DomainRules


CONTROLLER = Actor("ctrl", "port_controller")

# 普通浅吃水船，通航3-5，靠泊6-18
BASE = {'vessel': 'HaiYun', 'berth': 'B12', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 8.0, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18, 'risk_level': 'low', 'dangerous_goods': False, 'dangerous_class': '', 'transit_start_hour': 3, 'transit_end_hour': 5}


def make_data(**overrides):
    data = dict(BASE)
    data.update(overrides)
    return data


class ChannelRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def prepare(self, **overrides):
        return self.rules.prepare_create(make_data(**overrides))

    def test_registers_transit_window_and_draft_flags(self):
        prepared = self.prepare()
        self.assertEqual(prepared["transit_start_hour"], 3)
        self.assertEqual(prepared["transit_end_hour"], 5)
        self.assertFalse(prepared["deep_draft"])
        self.assertFalse(prepared["channel_exclusive"])
        self.assertTrue(prepared["nav_on_time"])
        self.assertEqual(prepared["nav_status"], "registered")

    def test_shallow_draft_allowed_in_ebb_tide(self):
        prepared = self.prepare(transit_start_hour=8, transit_end_hour=10, eta_hour=11)
        self.assertFalse(prepared["deep_draft"])
        self.assertTrue(prepared["nav_on_time"])

    def test_deep_draft_must_use_flood_window(self):
        with self.assertRaises(ValidationError):
            # 8-10处于落潮/平潮，深吃水船不得安排
            self.prepare(draft_m=DEFAULT_TIDE_SAFE_DRAFT_M + 0.1, transit_start_hour=8, transit_end_hour=10, eta_hour=11)

    def test_deep_draft_allowed_in_flood_window(self):
        prepared = self.prepare(draft_m=10.2)
        self.assertTrue(prepared["deep_draft"])
        self.assertTrue(prepared["nav_on_time"])

    def test_dangerous_goods_is_exclusive(self):
        prepared = self.prepare(dangerous_goods=True, dangerous_class="3类", transit_start_hour=4, transit_end_hour=6)
        self.assertTrue(prepared["channel_exclusive"])

    def test_overlength_vessel_is_exclusive(self):
        prepared = self.prepare(vessel_length_m=DEFAULT_EXCLUSIVE_LENGTH_M, berth_length_m=280, transit_start_hour=4, transit_end_hour=6)
        self.assertTrue(prepared["channel_exclusive"])

    def test_transit_window_inversion_rejected(self):
        with self.assertRaises(ValidationError):
            self.prepare(transit_start_hour=5, transit_end_hour=5)

    def test_late_transit_marked_not_on_time(self):
        prepared = self.prepare(transit_start_hour=5, transit_end_hour=7)
        self.assertFalse(prepared["nav_on_time"])

    def test_normal_vessels_may_share_channel(self):
        first = self.prepare()
        second = self.prepare(vessel='Other', berth='B13', transit_start_hour=4, transit_end_hour=6)
        self.rules.check_channel_conflicts(second, [{'id': 1, 'state': 'confirmed', 'payload': first}])

    def test_exclusive_blocks_everyone(self):
        exclusive = self.prepare(dangerous_goods=True, dangerous_class="3类", transit_start_hour=3, transit_end_hour=5)
        incoming = self.prepare(vessel='Other', berth='B13', transit_start_hour=4, transit_end_hour=6)
        with self.assertRaises(Conflict):
            self.rules.check_channel_conflicts(incoming, [{'id': 1, 'state': 'confirmed', 'payload': exclusive}])
        with self.assertRaises(Conflict):
            self.rules.check_channel_conflicts(exclusive, [{'id': 2, 'state': 'confirmed', 'payload': incoming}])

    def test_adjacent_windows_do_not_overlap(self):
        first = self.prepare(transit_start_hour=3, transit_end_hour=5)
        second = self.prepare(vessel='Other', berth='B13', transit_start_hour=5, transit_end_hour=6, eta_hour=7)
        self.rules.check_channel_conflicts(second, [{'id': 1, 'state': 'confirmed', 'payload': first}])

    def test_reschedule_required_and_cancelled_hold_no_window(self):
        first = self.prepare(dangerous_goods=True, dangerous_class="3类")
        incoming = self.prepare(vessel='Other', berth='B13', transit_start_hour=3, transit_end_hour=5)
        for state in ("reschedule_required", "cancelled", "departed"):
            self.rules.check_channel_conflicts(incoming, [{'id': 1, 'state': state, 'payload': first}])


class ChannelWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create(self, reference, **overrides):
        return self.service.create(CONTROLLER, reference, make_data(**overrides))

    def confirm(self, record, pilot='P-01'):
        return self.service.act(CONTROLLER, record["id"], record["version"], "confirm", {"pilot_id": pilot})

    def test_normal_vessels_pass_side_by_side(self):
        first = self.create("VOY-1")
        second = self.create("VOY-2", vessel="Other", berth="B13")
        self.assertEqual(self.confirm(first)["state"], "confirmed")
        self.assertEqual(self.confirm(second)["state"], "confirmed")

    def test_create_rejects_exclusive_overlap(self):
        self.create("VOY-1", dangerous_goods=True, dangerous_class="3类")
        with self.assertRaises(Conflict):
            self.create("VOY-2", vessel="Other", berth="B13", transit_start_hour=4, transit_end_hour=6)

    def test_deep_draft_ebb_tide_rejected_on_create(self):
        with self.assertRaises(ValidationError):
            self.create("VOY-1", draft_m=10.5, transit_start_hour=8, transit_end_hour=10, eta_hour=11)

    def test_late_transit_marks_both_nav_and_berth_for_reschedule(self):
        record = self.create("VOY-1", transit_start_hour=5, transit_end_hour=7)
        confirmed = self.confirm(record)
        self.assertEqual(confirmed["state"], "reschedule_required")
        self.assertEqual(confirmed["payload"]["nav_status"], "reschedule_required")
        self.assertIn("通航时段晚于靠泊开始", confirmed["payload"]["reschedule_reason"])

    def test_reschedule_recovers_to_confirmed(self):
        record = self.create("VOY-1", transit_start_hour=5, transit_end_hour=7)
        pending = self.confirm(record)
        self.assertEqual(pending["state"], "reschedule_required")
        # 重排到涨潮窗内且早于ETA=6
        recovered = self.service.act(CONTROLLER, pending["id"], pending["version"], "reschedule", {"transit_start_hour": 3, "transit_end_hour": 5})
        self.assertEqual(recovered["state"], "confirmed")
        self.assertTrue(recovered["payload"]["nav_on_time"])
        self.assertEqual(recovered["payload"]["nav_status"], "confirmed")
        self.assertNotIn("reschedule_reason", recovered["payload"])

    def test_reschedule_still_late_rejected(self):
        record = self.create("VOY-1", transit_start_hour=5, transit_end_hour=7)
        pending = self.confirm(record)
        with self.assertRaises(ValidationError):
            self.service.act(CONTROLLER, pending["id"], pending["version"], "reschedule", {"transit_start_hour": 6, "transit_end_hour": 7})

    def test_reschedule_into_occupied_window_rejected(self):
        # 独占危险品船占3-5，待重排船不能重排进该时段
        blocker = self.create("VOY-1", vessel="Danger", dangerous_goods=True, dangerous_class="3类", transit_start_hour=3, transit_end_hour=5)
        self.assertEqual(self.confirm(blocker)["state"], "confirmed")
        pending = self.confirm(self.create("VOY-2", vessel="Late", berth="B13", transit_start_hour=5, transit_end_hour=7))
        self.assertEqual(pending["state"], "reschedule_required")
        with self.assertRaises(Conflict):
            self.service.act(CONTROLLER, pending["id"], pending["version"], "reschedule", {"transit_start_hour": 3, "transit_end_hour": 5})

    def test_departure_releases_channel_window(self):
        first = self.create("VOY-1")
        confirmed = self.confirm(first)
        berthed = self.service.act(CONTROLLER, confirmed["id"], confirmed["version"], "berth", {"actual_draft_m": 8.1})
        departed = self.service.act(CONTROLLER, berthed["id"], berthed["version"], "depart", {"cargo_operation_complete": True})
        self.assertEqual(departed["payload"]["nav_status"], "released")
        # 时段让出后，后来的船可以登记同一时段
        follower = self.create("VOY-2", vessel="Follower", berth="B13", transit_start_hour=4, transit_end_hour=5)
        self.assertEqual(self.confirm(follower)["state"], "confirmed")

    def test_reschedule_required_holds_no_window(self):
        pending = self.confirm(self.create("VOY-1", vessel="Late", transit_start_hour=5, transit_end_hour=7))
        self.assertEqual(pending["state"], "reschedule_required")
        # 待重排记录不再占航道，与其时段重叠（5-7 vs 4-6）的新计划仍可登记确认
        follower = self.create("VOY-2", vessel="Follower", berth="B13", transit_start_hour=4, transit_end_hour=6)
        self.assertEqual(self.confirm(follower)["state"], "confirmed")

    def test_cancel_allowed_from_reschedule_required(self):
        pending = self.confirm(self.create("VOY-1", transit_start_hour=5, transit_end_hour=7))
        cancelled = self.service.act(CONTROLLER, pending["id"], pending["version"], "cancel", {"cancel_reason": "取消"})
        self.assertEqual(cancelled["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
