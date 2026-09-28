import unittest

from src.domain import Actor, Conflict, ValidationError
from src.rules import DomainRules


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
FLOW = [
    ('approve', 'repair_manager', {'repair_manager': 'RM-2'}, 'approved'),
    ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}, 'mobilized'),
    ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}, 'surveyed'),
    ('splice', 'cable_engineer', {'splice_loss_db': 0.12, 'spare_used_km': 16}, 'spliced'),
    ('test', 'noc_operator', {'end_to_end_loss_db': 0.3}, 'tested'),
    ('register_batch', 'noc_operator', {'route': 'R-北', 'capacity_gbps': 400, 'customer_groups': ['政企']}, 'restoring'),
]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        self.assertEqual(prepared["repair_distance_km"], 15.0)
        self.assertEqual(prepared["required_spare_km"], 15.75)
        self.assertTrue(prepared["repair_feasible"])

    def test_action_calculation(self):
        action, role, data, expected_state = FLOW[0]
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, summary = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertEqual(payload["repair_manager"], "RM-2")

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["sea_state"] = 12
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)

    def test_restoration_batch_rules(self):
        record = {"id": 1, "state": "tested", "payload": self.rules.prepare_create(CREATE_DATA)}
        plan = self.rules.plan_restoration(record, "register_batch", {"route": "R-北", "capacity_gbps": 300, "customer_groups": ["政企"]}, [])
        self.assertEqual(plan["state"], "restoring")
        record = {"id": 1, "state": "restoring", "payload": record["payload"]}
        batch = {"batch_no": 1, "route": "R-北", "capacity_gbps": 300, "customer_groups": ["政企"], "status": "pending"}
        with self.assertRaises(ValidationError):
            self.rules.plan_restoration(record, "register_batch", {"route": "R-南", "capacity_gbps": 200, "customer_groups": ["互联网"]}, [batch])
        with self.assertRaises(Conflict):
            self.rules.plan_restoration(record, "register_batch", {"route": "R-北", "capacity_gbps": 100, "customer_groups": ["互联网"]}, [batch])
        with self.assertRaises(Conflict):
            self.rules.plan_restoration(record, "register_batch", {"route": "R-南", "capacity_gbps": 100, "customer_groups": ["政企"]}, [batch])
