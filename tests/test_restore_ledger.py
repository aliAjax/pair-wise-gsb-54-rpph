import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.rules import BATCH_CONFIRMED, BATCH_PENDING, BATCH_REVERTED


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
PRE_FLOW = [('approve', 'repair_manager', {'repair_manager': 'RM-2'}), ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}), ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}), ('splice', 'cable_engineer', {'splice_loss_db': 0.12, 'spare_used_km': 16}), ('test', 'noc_operator', {'end_to_end_loss_db': 0.3})]
NOC = Actor("dispatcher", "noc_operator")


class RestoreLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _to_tested(self, reference="CABLE-40001"):
        record = self.service.create(Actor("creator", "noc_operator"), reference, CREATE_DATA)
        for action, role, data in PRE_FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        return record

    def _register(self, record, route, capacity, groups):
        return self.service.act(NOC, record["id"], record["version"], "register_batch", {"route": route, "capacity_gbps": capacity, "customer_groups": groups})

    def _confirm(self, record, seq):
        return self.service.act(NOC, record["id"], record["version"], "confirm_batch", {"seq": seq})

    def _revert(self, record, seq):
        return self.service.act(NOC, record["id"], record["version"], "revert_batch", {"seq": seq})

    def _finish(self, record):
        return self.service.act(NOC, record["id"], record["version"], "restore", {})

    def test_batches_accumulate_and_detail_shows_gap(self):
        record = self._to_tested()
        record = self._register(record, "R-NORTH", 200, ["G1", "G2"])
        self.assertEqual(record["state"], "restoring")
        ledger = record["restore_ledger"]
        self.assertEqual(ledger["registered_capacity_gbps"], 200)
        self.assertEqual(ledger["confirmed_capacity_gbps"], 0)
        self.assertEqual(ledger["remaining_gap_gbps"], 400)
        self.assertEqual(ledger["pending_count"], 1)
        self.assertEqual(ledger["batches"][0]["status"], BATCH_PENDING)
        record = self._confirm(record, 1)
        ledger = record["restore_ledger"]
        self.assertEqual(ledger["confirmed_capacity_gbps"], 200)
        self.assertEqual(ledger["remaining_gap_gbps"], 200)
        self.assertEqual(ledger["pending_count"], 0)
        record = self._register(record, "R-SOUTH", 200, ["G3"])
        record = self._confirm(record, 2)
        self.assertEqual(record["restore_ledger"]["remaining_gap_gbps"], 0)
        record = self._finish(record)
        self.assertEqual(record["state"], "restored")
        self.assertEqual(record["payload"]["restore_capacity_gbps"], 400)
        detail = self.service.get_record(NOC, record["id"])
        self.assertEqual(detail["restore_ledger"]["confirmed_capacity_gbps"], 400)
        self.assertEqual(len(detail["restore_ledger"]["batches"]), 2)

    def test_register_over_capacity_rejected(self):
        record = self._to_tested()
        record = self._register(record, "R1", 250, ["G1"])
        record = self._confirm(record, 1)
        with self.assertRaises(Conflict):
            self._register(record, "R2", 200, ["G2"])

    def test_duplicate_route_or_customer_group_rejected(self):
        record = self._to_tested()
        record = self._register(record, "R1", 100, ["G1"])
        with self.assertRaises(Conflict):
            self._register(record, "R1", 50, ["G2"])
        with self.assertRaises(Conflict):
            self._register(record, "R2", 50, ["G1"])
        with self.assertRaises(ValidationError):
            self._register(record, "R3", 50, ["G9", "G9"])

    def test_pending_blocks_finish_even_when_full(self):
        record = self._to_tested()
        record = self._register(record, "R1", 400, ["G1"])
        with self.assertRaises(Conflict):
            self._finish(record)
        record = self._confirm(record, 1)
        record = self._finish(record)
        self.assertEqual(record["state"], "restored")

    def test_finish_rejected_when_capacity_short(self):
        record = self._to_tested()
        record = self._register(record, "R1", 200, ["G1"])
        record = self._confirm(record, 1)
        with self.assertRaises(Conflict):
            self._finish(record)

    def test_revert_pending_releases_capacity_and_occupancy(self):
        record = self._to_tested()
        record = self._register(record, "R1", 250, ["G1"])
        record = self._revert(record, 1)
        ledger = record["restore_ledger"]
        self.assertEqual(ledger["registered_capacity_gbps"], 0)
        self.assertEqual(ledger["pending_count"], 0)
        self.assertEqual(ledger["batches"][0]["status"], BATCH_REVERTED)
        record = self._register(record, "R1", 400, ["G1"])
        record = self._confirm(record, 2)
        self.assertEqual(record["restore_ledger"]["remaining_gap_gbps"], 0)
        record = self._finish(record)
        self.assertEqual(record["state"], "restored")

    def test_revert_confirmed_removes_only_that_batch_from_available(self):
        record = self._to_tested()
        record = self._register(record, "R1", 250, ["G1"])
        record = self._confirm(record, 1)
        record = self._register(record, "R2", 150, ["G2"])
        record = self._confirm(record, 2)
        record = self._revert(record, 2)
        ledger = record["restore_ledger"]
        self.assertEqual(ledger["registered_capacity_gbps"], 250)
        self.assertEqual(ledger["confirmed_capacity_gbps"], 250)
        self.assertEqual(ledger["remaining_gap_gbps"], 150)
        statuses = {batch["seq"]: batch["status"] for batch in ledger["batches"]}
        self.assertEqual(statuses[1], BATCH_CONFIRMED)
        self.assertEqual(statuses[2], BATCH_REVERTED)
        with self.assertRaises(Conflict):
            self._finish(record)
        record = self._register(record, "R2", 150, ["G2"])
        self.assertEqual(record["restore_ledger"]["batches"][-1]["seq"], 3)
        record = self._confirm(record, 3)
        record = self._finish(record)
        self.assertEqual(record["state"], "restored")

    def test_duplicate_action_on_reverted_batch_rejected(self):
        record = self._to_tested()
        record = self._register(record, "R1", 100, ["G1"])
        record = self._revert(record, 1)
        with self.assertRaises(Conflict):
            self._revert(record, 1)
        with self.assertRaises(Conflict):
            self._confirm(record, 1)

    def test_unknown_batch_rejected(self):
        record = self._to_tested()
        record = self._register(record, "R1", 100, ["G1"])
        with self.assertRaises(Conflict):
            self._confirm(record, 9)

    def test_register_requires_tested_or_restoring_state(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-40002", CREATE_DATA)
        with self.assertRaises(Conflict):
            self._register(record, "R1", 100, ["G1"])

    def test_permission_and_stale_version(self):
        record = self._to_tested()
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("v1", "vessel_master"), record["id"], record["version"], "register_batch", {"route": "R1", "capacity_gbps": 100, "customer_groups": ["G1"]})
        record = self._register(record, "R1", 100, ["G1"])
        with self.assertRaises(Conflict):
            self.service.act(NOC, record["id"], record["version"] - 1, "confirm_batch", {"seq": 1})

    def test_ledger_actions_audited(self):
        record = self._to_tested()
        record = self._register(record, "R1", 100, ["G1"])
        record = self._confirm(record, 1)
        record = self._revert(record, 1)
        timeline = self.service.timeline(NOC, record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("register_batch", actions)
        self.assertIn("confirm_batch", actions)
        self.assertIn("revert_batch", actions)
