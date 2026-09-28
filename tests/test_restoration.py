import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
PREFIX = [
    ('approve', 'repair_manager', {'repair_manager': 'RM-2'}),
    ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}),
    ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}),
    ('splice', 'cable_engineer', {'splice_loss_db': 0.12, 'spare_used_km': 16}),
    ('test', 'noc_operator', {'end_to_end_loss_db': 0.3}),
]


class RestorationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _to_tested(self, reference='CABLE-40001'):
        record = self.service.create(Actor('creator', 'noc_operator'), reference, CREATE_DATA)
        for action, role, data in PREFIX:
            record = self.service.act(Actor('operator', role), record['id'], record['version'], action, data)
        self.assertEqual(record['state'], 'tested')
        return record

    def _register(self, record, batch_no, route, capacity, groups):
        return self.service.act(Actor('noc', 'noc_operator'), record['id'], record['version'], 'register_batch',
                                {'route': route, 'capacity_gbps': capacity, 'customer_groups': groups})

    def test_register_creates_pending_batch_without_available_capacity(self):
        record = self._to_tested()
        record = self._register(record, 1, 'R-北', 200, ['政企', '金融'])
        self.assertEqual(record['state'], 'restoring')
        view = record['restoration']
        self.assertEqual(view['counts'], {'pending': 1, 'confirmed': 0, 'revoked': 0})
        self.assertEqual(view['confirmed_capacity_gbps'], 0)
        self.assertEqual(view['pending_capacity_gbps'], 200)
        self.assertEqual(view['remaining_gap_gbps'], 400)
        self.assertFalse(view['can_complete'])
        self.assertEqual(view['batches'][0]['status'], 'pending')

    def test_confirmed_capacity_accumulates_and_gap_shrinks(self):
        record = self._to_tested()
        record = self._register(record, 1, 'R-北', 200, ['政企'])
        record = self.service.act(Actor('noc', 'noc_operator'), record['id'], record['version'], 'confirm_batch', {'batch_no': 1})
        view = record['restoration']
        self.assertEqual(view['confirmed_capacity_gbps'], 200)
        self.assertEqual(view['pending_capacity_gbps'], 0)
        self.assertEqual(view['remaining_gap_gbps'], 200)
        self.assertFalse(view['can_complete'])

    def test_pending_capacity_counts_toward_overallocation(self):
        record = self._to_tested()
        record = self._register(record, 1, 'R-北', 300, ['政企'])
        with self.assertRaises(ValidationError):
            self._register(record, 2, 'R-南', 200, ['互联网'])

    def test_duplicate_route_rejected(self):
        record = self._to_tested()
        record = self._register(record, 1, 'R-北', 200, ['政企'])
        with self.assertRaises(Conflict):
            self._register(record, 2, 'R-北', 100, ['互联网'])

    def test_duplicate_customer_group_rejected(self):
        record = self._to_tested()
        record = self._register(record, 1, 'R-北', 200, ['政企', '金融'])
        with self.assertRaises(Conflict):
            self._register(record, 2, 'R-南', 100, ['金融'])

    def test_pending_batch_blocks_completion(self):
        record = self._to_tested()
        record = self._register(record, 1, 'R-北', 400, ['政企'])
        with self.assertRaises(Conflict):
            self.service.act(Actor('mgr', 'repair_manager'), record['id'], record['version'], 'complete_restoration', {})

    def test_unfilled_capacity_blocks_completion(self):
        record = self._to_tested()
        record = self._register(record, 1, 'R-北', 300, ['政企'])
        record = self.service.act(Actor('noc', 'noc_operator'), record['id'], record['version'], 'confirm_batch', {'batch_no': 1})
        with self.assertRaises(ValidationError):
            self.service.act(Actor('mgr', 'repair_manager'), record['id'], record['version'], 'complete_restoration', {})

    def test_revoke_pending_releases_only_that_batch(self):
        record = self._to_tested()
        record = self._register(record, 1, 'R-北', 300, ['政企'])
        record = self._register(record, 2, 'R-南', 100, ['金融'])
        record = self.service.act(Actor('noc', 'noc_operator'), record['id'], record['version'], 'revoke_batch', {'batch_no': 1})
        view = record['restoration']
        self.assertEqual(view['counts'], {'pending': 1, 'confirmed': 0, 'revoked': 1})
        self.assertEqual(view['pending_capacity_gbps'], 100)
        self.assertEqual(view['batches'][0]['route'], 'R-北')
        self.assertEqual(view['batches'][0]['status'], 'revoked')
        self.assertIsNotNone(view['batches'][0]['revoked_at'])
        record = self._register(record, 3, 'R-东', 300, ['政企'])
        view = record['restoration']
        self.assertEqual(view['pending_capacity_gbps'], 400)

    def test_revoke_confirmed_rolls_back_available_capacity(self):
        record = self._to_tested()
        record = self._register(record, 1, 'R-北', 400, ['政企'])
        record = self.service.act(Actor('noc', 'noc_operator'), record['id'], record['version'], 'confirm_batch', {'batch_no': 1})
        self.assertTrue(record['restoration']['capacity_complete'])
        record = self.service.act(Actor('mgr', 'repair_manager'), record['id'], record['version'], 'revoke_batch', {'batch_no': 1})
        view = record['restoration']
        self.assertEqual(view['confirmed_capacity_gbps'], 0)
        self.assertEqual(view['remaining_gap_gbps'], 400)
        self.assertFalse(view['can_complete'])
        with self.assertRaises(Conflict):
            self.service.act(Actor('mgr', 'repair_manager'), record['id'], record['version'], 'revoke_batch', {'batch_no': 1})

    def test_full_flow_completes_when_capacity_filled(self):
        record = self._to_tested()
        record = self._register(record, 1, 'R-北', 200, ['政企', '金融'])
        record = self.service.act(Actor('noc', 'noc_operator'), record['id'], record['version'], 'confirm_batch', {'batch_no': 1})
        record = self._register(record, 2, 'R-南', 200, ['互联网'])
        record = self.service.act(Actor('noc', 'noc_operator'), record['id'], record['version'], 'confirm_batch', {'batch_no': 2})
        self.assertTrue(record['restoration']['can_complete'])
        record = self.service.act(Actor('mgr', 'repair_manager'), record['id'], record['version'], 'complete_restoration', {})
        self.assertEqual(record['state'], 'restored')
        detail = self.service.get_record(Actor('creator', 'noc_operator'), record['id'])
        view = detail['restoration']
        self.assertEqual(view['confirmed_capacity_gbps'], 400)
        self.assertEqual(view['remaining_gap_gbps'], 0)
        self.assertEqual(len(view['batches']), 2)
        timeline = self.service.timeline(Actor('creator', 'noc_operator'), record['id'])
        actions = [event['action'] for event in timeline]
        self.assertEqual(actions[-1], 'complete_restoration')
        self.assertEqual(actions, ['created'] + [step[0] for step in PREFIX] + ['register_batch', 'confirm_batch', 'register_batch', 'confirm_batch', 'complete_restoration'])

    def test_stale_version_rejected_on_batch_action(self):
        record = self._to_tested()
        record = self._register(record, 1, 'R-北', 400, ['政企'])
        with self.assertRaises(Conflict):
            self.service.act(Actor('noc', 'noc_operator'), record['id'], record['version'] - 1, 'confirm_batch', {'batch_no': 1})

    def test_role_permission_enforced(self):
        record = self._to_tested()
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor('eng', 'cable_engineer'), record['id'], record['version'], 'register_batch',
                             {'route': 'R-北', 'capacity_gbps': 400, 'customer_groups': ['政企']})

    def test_batch_actions_require_tested_or_restoring(self):
        record = self.service.create(Actor('creator', 'noc_operator'), 'CABLE-40002', CREATE_DATA)
        with self.assertRaises(Conflict):
            self._register(record, 1, 'R-北', 400, ['政企'])

    def test_confirm_unknown_batch_rejected(self):
        record = self._to_tested()
        record = self._register(record, 1, 'R-北', 400, ['政企'])
        with self.assertRaises(NotFound):
            self.service.act(Actor('noc', 'noc_operator'), record['id'], record['version'], 'confirm_batch', {'batch_no': 9})


if __name__ == '__main__':
    unittest.main()
