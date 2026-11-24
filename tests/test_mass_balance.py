"""Synthetic mass conservation tests; no paid API, Toolkit or PIPESIM calls."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from scripts.mass_balance import calculate, criteria, extract, kg_per_second, unavailable
from src.agent.report import build_answer
from src.well_agent_mcp.schemas import OUTPUT_SCHEMA
from test_mcp_read_results import Fixture

with patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'scripts')] + sys.path):
    from scripts.run_network_model import assess


class MassArithmeticTests(unittest.TestCase):
    def test_no_approved_tolerance_retains_real_difference(self):
        result = calculate(2.0, 1.5)
        self.assertEqual(result['signed_residual_kg_s'], 0.5)
        self.assertEqual(result['absolute_residual_kg_s'], 0.5)
        self.assertEqual(result['relative_residual_percent'], 25.0)
        self.assertEqual(result['assessment'], 'NOT_ASSESSED')
        self.assertIsNone(result['allowed_residual_kg_s'])
        self.assertEqual(calculate(1, 1.25)['signed_residual_kg_s'], -0.25)

    def test_units_are_explicit_and_volumes_are_rejected(self):
        self.assertEqual(kg_per_second(3600, 'kg/h'), 1)
        self.assertAlmostEqual(kg_per_second(1, 'lb/sec'), 0.45359237)
        for unit in ['sm3/s', 'm3/day', 'unknown', None]:
            with self.subTest(unit=unit), self.assertRaises(ValueError):
                kg_per_second(1, unit)

    def test_nonfinite_missing_boolean_and_reverse_flow_rejected(self):
        for value in [True, None, '1', float('nan'), float('inf'), -1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                calculate(value, 1)

    def test_tolerances_require_provenance_and_use_inclusive_boundary(self):
        config = {'absolute_tolerance_kg_s': 0.25, 'relative_tolerance': None,
                  'approval_reference': 'SYNTHETIC_TEST_ONLY'}
        self.assertEqual(calculate(1, 0.75, config)['assessment'], 'OK')
        self.assertEqual(calculate(1, 0.749, config)['assessment'], 'OUT_OF_TOLERANCE')
        for patch_values in [{'approval_reference': None}, {'absolute_tolerance_kg_s': -1},
                             {'relative_tolerance': True}, {'extra': 1}]:
            with self.subTest(patch_values=patch_values), self.assertRaises(ValueError):
                criteria(dict(config, **patch_values))

    def test_relative_rule_and_zero_reference(self):
        config = {'absolute_tolerance_kg_s': None, 'relative_tolerance': 0.25,
                  'approval_reference': 'SYNTHETIC_TEST_ONLY'}
        self.assertEqual(calculate(1, 0.75, config)['assessment'], 'OK')
        zero = calculate(0, 0, config)
        self.assertEqual(zero['reason'], 'ZERO_REFERENCE_REQUIRES_ABSOLUTE_TOLERANCE')
        self.assertIsNone(zero['relative_residual_percent'])
        self.assertEqual(zero['assessment'], 'NOT_ASSESSED')
        config['absolute_tolerance_kg_s'] = 0.01
        self.assertEqual(calculate(0, 0.02, config)['assessment'], 'OUT_OF_TOLERANCE')
        self.assertEqual(calculate(0, 0.005, config)['assessment'], 'OK')
        # The two criteria combine as max(abs_tol, rel_tol * inlet), before rounding.
        config['absolute_tolerance_kg_s'] = 0.5
        self.assertEqual(calculate(1, 0.5, config)['allowed_residual_kg_s'], 0.5)


class WorkerMassTests(unittest.TestCase):
    def payload(self):
        nodes = {'Pressure': {'Unit': 'Pa a', 'agzu6_out': 1200000.0, 'tiein_agzu4_agzu1': 1e6},
                 'Temperature': {'Unit': 'K', 'agzu6_out': 293.15, 'tiein_agzu4_agzu1': 293.15}}
        for phase, value in [('Liquid', 10), ('Oil', 4), ('Water', 6), ('Gas', 80)]:
            nodes['VolumeFlowrate' + phase + 'StockTank'] = {
                'Unit': 'sm3/s', 'agzu6_out': value / 86400, 'tiein_agzu4_agzu1': value / 86400}
        return {'state': 'Completed', 'summary': {'Error': [], 'Warning': []}, 'messages': [], 'node': nodes,
                'profile': {'agzu6_out': {'Temperature': [293.15, 293.15], 'Pressure': [1.2e6, 1e6],
                                         'TotalDistance': [0.0, 100.0], 'Elevation': [0.0, 0.0],
                                         'MassFlowrate': [0.65, 0.65]},
                            'tiein_agzu4_agzu1': {'Pressure': [1e6], 'TotalDistance': [0.0],
                                                'MassFlowrate': [0.650000001]}},
                'profile_units': {'Temperature': 'K', 'Pressure': 'Pa a', 'TotalDistance': 'm',
                                  'Elevation': 'm', 'MassFlowrate': 'kg/s'}}

    def assess(self, payload, config=None):
        return assess(payload, 'agzu6_out', 'tiein_agzu4_agzu1',
                      {'liquid_m3_day': 10, 'oil_m3_day': 4, 'water_m3_day': 6, 'gas_m3_day': 80},
                      293.15, 1e6, {'points': 2, 'axis_length_m': 100, 'rise_m': 0},
                      mass_balance_criteria=config)

    def test_mass_export_and_unknown_units_never_become_pass(self):
        raw = self.assess(self.payload())
        self.assertEqual(raw['status'], 'NUMERICAL_CHECKS_PASSED')
        self.assertTrue(raw['checks']['mass_flow_available'])
        self.assertEqual(raw['mass_balance']['assessment'], 'NOT_ASSESSED')
        self.assertEqual(raw['mass_balance']['source_variable'], 'MassFlowrate')
        self.assertEqual(raw['mass_balance']['sink']['value'], 0.650000001)
        for unit in [None, 'sm3/s']:
            payload = self.payload()
            payload['profile_units']['MassFlowrate'] = unit
            raw = self.assess(payload)
            self.assertEqual(raw['status'], 'CHECK_REQUIRED')
            self.assertFalse(raw['checks']['mass_flow_available'])
            self.assertIsNone(raw['mass_balance']['source']['value'])
        payload = self.payload()
        del payload['profile']['tiein_agzu4_agzu1']['MassFlowrate']
        self.assertFalse(self.assess(payload)['checks']['mass_flow_available'])

    def test_distinct_boundary_branches_and_correct_endpoints(self):
        payload = self.payload()
        payload['profile']['agzu6_out']['MassFlowrate'] = [1.0, 0.9]
        payload['profile']['tiein_agzu4_agzu1'] = {
            'MassFlowrate': [0.85, 0.8], 'Pressure': [1e6, 1e6], 'TotalDistance': [0.0, 0.0]}
        result = extract(payload)
        self.assertEqual(result['source']['value'], 1.0)
        self.assertEqual(result['sink']['value'], 0.8)
        self.assertAlmostEqual(result['signed_residual_kg_s'], 0.2)

    def test_unaligned_or_empty_mass_profile_is_not_used(self):
        for values in [[], [0.65], [0.65, None], [0.65, float('nan')]]:
            payload = self.payload()
            payload['profile']['agzu6_out']['MassFlowrate'] = values
            self.assertFalse(self.assess(payload)['checks']['mass_flow_available'])

    def test_failure_of_approved_mass_criterion_blocks_success(self):
        config = {'absolute_tolerance_kg_s': 1e-10, 'relative_tolerance': None,
                  'approval_reference': 'SYNTHETIC_TEST_ONLY'}
        result = self.assess(self.payload(), config)
        self.assertEqual(result['mass_balance']['assessment'], 'OUT_OF_TOLERANCE')
        self.assertFalse(result['checks']['mass_balance_within_tolerance'])
        self.assertEqual(result['status'], 'CHECK_REQUIRED')


class MassProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Fixture(self.temp.name)
        self.config = criteria()
        self.fixture.data['result_features'] = ['total_mass_balance_v1']
        self.fixture.data['mass_balance_criteria'] = self.config
        for study in self.fixture.data['studies'].values():
            for raw in study['runs'].values():
                raw['mass_balance'] = calculate(0.65, 0.650000001, self.config)
                raw['mass_balance']['source_variable'] = 'MassFlowrate'
                raw['checks']['mass_flow_available'] = True

    def call(self):
        result = self.fixture.call(self.fixture.register())
        Draft202012Validator(OUTPUT_SCHEMA).validate(result)
        return result

    def test_mcp_projects_mass_and_agent_explains_missing_tolerance(self):
        result = self.call()
        self.assertEqual(result['tool_status'], 'SUCCESS')
        mass = result['results']['base']['mass_balance']
        self.assertEqual(mass['source']['unit'], 'kg/s')
        self.assertAlmostEqual(mass['signed_residual_kg_s'], -1e-9)
        self.assertEqual(mass['assessment'], 'NOT_ASSESSED')
        answer = build_answer([{'tool': 'read_results', 'payload': result}], 'read_results')
        self.assertIn('массовый расход входа', answer)
        self.assertIn('Допуск массового баланса не утверждён', answer)

    def test_forged_residual_status_units_and_criteria_are_rejected(self):
        original = copy.deepcopy(self.fixture.data)
        for stage in ['run', 'restart']:
            for mutation in ['residual', 'assessment', 'unit', 'unit_missing', 'object_id', 'criteria', 'missing']:
                with self.subTest(stage=stage, mutation=mutation):
                    self.fixture.data = copy.deepcopy(original)
                    raw = self.fixture.data['studies']['base']['runs'][stage]
                    mass = raw['mass_balance']
                    if mutation == 'residual': mass['signed_residual_kg_s'] = 999
                    elif mutation == 'assessment': mass['assessment'] = 'OK'
                    elif mutation == 'unit': mass['source']['unit'] = 'sm3/day'
                    elif mutation == 'unit_missing': mass['source'].pop('unit')
                    elif mutation == 'object_id': mass['source']['object_id'] = 'other_node'
                    elif mutation == 'criteria': mass['criteria'] = dict(self.config, absolute_tolerance_kg_s=1)
                    else: del raw['mass_balance']
                    self.assertEqual(self.call()['error']['code'], 'REPORT_INVALID')

    def test_legacy_report_is_readable_but_mass_not_exported(self):
        self.fixture.data.pop('result_features')
        self.fixture.data.pop('mass_balance_criteria')
        for study in self.fixture.data['studies'].values():
            for raw in study['runs'].values():
                raw.pop('mass_balance')
                raw['checks'].pop('mass_flow_available')
        result = self.call()
        self.assertEqual(result['tool_status'], 'SUCCESS')
        mass = result['results']['base']['mass_balance']
        self.assertEqual(mass['reason'], 'NOT_EXPORTED')
        self.assertIsNone(mass['absolute_residual_kg_s'])

    def test_missing_export_does_not_produce_success(self):
        raw = self.fixture.data['studies']['base']['runs']['run']
        raw['mass_balance'] = unavailable(self.config, 'MISSING_OR_INVALID_DATA')
        raw['mass_balance_error'] = "KeyError: 'MassFlowrateFluid'"
        raw['checks']['mass_flow_available'] = False
        raw['status'] = 'CHECK_REQUIRED'
        self.fixture.data['studies']['base']['runs'].pop('restart')
        self.fixture.data['status'] = 'CHECK_REQUIRED'
        self.fixture.data['stage'] = 'base:run'
        self.fixture.data['studies'].pop('plus10')
        result = self.call()
        self.assertEqual(result['tool_status'], 'ERROR')
        self.assertEqual(result['results']['base']['mass_balance']['reason'], 'MISSING_OR_INVALID_DATA')
        self.assertEqual(result['numerical_validation'], 'NOT_ASSESSED')
        self.assertEqual(result['results']['base']['numerical_validation'], 'NOT_ASSESSED')
        self.assertEqual(result['results']['base']['mass_balance_error'], "KeyError: 'MassFlowrateFluid'")
        answer = build_answer([{'tool': 'run_steady_state', 'payload': result},
                               {'tool': 'read_results', 'payload': result}], 'run_steady_state')
        self.assertEqual(answer.count('Ошибка RESULT_INCOMPLETE:'), 1)
        self.assertIn("ошибка экспорта массовых расходов: KeyError: 'MassFlowrateFluid'", answer)

    def test_known_numeric_failure_is_still_out_of_tolerance_when_mass_missing(self):
        raw = self.fixture.data['studies']['base']['runs']['run']
        raw['mass_balance'] = unavailable(self.config, 'MISSING_OR_INVALID_DATA')
        raw['checks']['mass_flow_available'] = False
        raw['checks']['outlet_pressure'] = False
        raw['status'] = 'CHECK_REQUIRED'
        self.fixture.data['studies']['base']['runs'].pop('restart')
        self.fixture.data['status'] = 'CHECK_REQUIRED'
        self.fixture.data['stage'] = 'base:run'
        self.fixture.data['studies'].pop('plus10')
        self.assertEqual(self.call()['numerical_validation'], 'OUT_OF_TOLERANCE')


if __name__ == '__main__':
    unittest.main()
