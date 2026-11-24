"""Synthetic reports only. No PIPESIM, field data, credentials or licenses needed."""

import json
import tempfile
import unittest
from pathlib import Path

import anyio
from jsonschema import Draft202012Validator

from src.well_agent_mcp.__main__ import check_server
from src.well_agent_mcp.results import REQUIRED_CHECKS, ReadResults, Registry, ReportError, checksum
from src.well_agent_mcp.schemas import OUTPUT_SCHEMA


class Fixture:
    def __init__(self, directory):
        self.root = Path(directory)
        self.model = self.root / 'network.pips';self.model.write_bytes(b'synthetic model; not a PIPESIM file')
        self.rates = self.root / 'rates.json'
        self.rates.write_text(json.dumps({'stock_tank_conditions': {'temperature_c': '15.5555555556',
            'pressure_pa': '101352.9322095696', 'pressure_scale': 'absolute'}}), encoding='utf-8')
        self.manifest = self.model.with_suffix('.json')
        self.manifest.write_text(json.dumps({'status': 'NETWORK_STRUCTURE_VERIFIED',
            'model_sha256': checksum(self.model.read_bytes()),
            'input_hashes': {'rates_json': checksum(self.rates.read_bytes())},
            'after_reopen': {'connections': [
                {'Source': 'agzu6_out', 'Destination': 'agzu6_to_tiein'},
                {'Source': 'agzu6_to_tiein', 'Destination': 'tiein_agzu4_agzu1'}]}}), encoding='utf-8')
        self.report = self.root / 'report.json'
        def run(pressure, warned):
            return {'status': 'NUMERICAL_CHECKS_PASSED_WITH_WARNINGS' if warned else 'NUMERICAL_CHECKS_PASSED',
                'checks': {k: True for k in REQUIRED_CHECKS},
                'warnings': ['Synthetic warning on first run'] if warned else [],
                'inlet_pressure_pa_absolute': pressure, 'outlet_pressure_pa_absolute': 1e6,
                'stock_rates_m3_day': {'liquid': {'source': 10.001, 'sink': 10.001, 'expected': 10.0}},
                'profile': {'points': 53, 'temperature_min_c': 20.0, 'temperature_max_c': 20.0}}
        self.data = {'status': 'NUMERICAL_CHECKS_PASSED_WITH_WARNINGS', 'stage': 'complete',
            'source_sha256': checksum(self.model.read_bytes()), 'manifest_sha256': checksum(self.manifest.read_bytes()),
            'rates_sha256': checksum(self.rates.read_bytes()), 'source_model_unchanged': True,
            'physical_validation': 'NOT_ASSESSED', 'numerical_limits': {'outlet_pressure_pa': 100.0,
                'temperature_k': 0.001, 'rate_relative': 0.0001, 'run_restart_pressure_pa': 1.0},
            'studies': {name: {'repeatability': {'matches': True, 'restart_minus_run_inlet_pressure_pa': 0.0},
                'runs': {'run': run(pressure, True), 'restart': run(pressure, False)}}
                for name, pressure in [('base', 1200000.0), ('plus10', 1200100.0)]}}
        self.registry = self.root / 'registry'

    def register(self):
        self.report.write_text(json.dumps(self.data, allow_nan=False), encoding='utf-8')
        return Registry(self.registry).register(self.report, self.model, self.rates)

    def call(self, run_id, scenario='compare', **extra):
        result = ReadResults(self.registry).call({'run_id': run_id, 'scenario': scenario, **extra})
        Draft202012Validator(OUTPUT_SCHEMA).validate(result)
        return result


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(self.temporary.name)

    def test_registered_report_units_warnings_and_actual_rate(self):
        f = self.fixture;run_id = f.register();result = f.call(run_id)
        self.assertEqual(result['tool_status'], 'SUCCESS')
        self.assertEqual(result['execution_status'], 'SUCCESS')
        self.assertEqual(result['numerical_validation'], 'OK')
        self.assertEqual(result['physical_validation'], 'NOT_ASSESSED')
        base = result['results']['base']
        self.assertEqual(base['inlet_pressure'], {'object_id': 'agzu6_out', 'value': 12.0, 'unit': 'bar', 'reference': 'absolute'})
        self.assertEqual(base['liquid_rate_source']['value'], 10.001)
        self.assertEqual(base['result_origin'], 'restart')
        self.assertEqual(base['repeatability'], 'CONFIRMED')
        self.assertEqual(len(result['warnings']), 2)
        self.assertTrue(all(w['stage'] == 'run' for w in result['warnings']))
        self.assertAlmostEqual(result['comparison']['inlet_pressure_plus10_minus_base']['value'], 0.001)
        self.assertAlmostEqual(result['stock_tank_conditions']['pressure']['value'], 1.013529322095696)
        trace = f.registry / 'traces' / (result['trace_id'] + '.json')
        self.assertEqual(json.loads(trace.read_text(encoding='utf-8'))['result'], result)
        self.assertEqual(f.register(), run_id)

    def test_single_scenario(self):
        f = self.fixture;run_id = f.register()
        for name in ['base', 'plus10']:
            result = f.call(run_id, name)
            self.assertEqual(set(result['results']), {name})
            self.assertIsNone(result['comparison'])

    def test_unknown_run_and_bad_arguments_and_no_secret_logging(self):
        f = self.fixture;run_id = f.register()
        self.assertEqual(f.call('12345678-1234-4234-8234-123456789abc')['error']['code'], 'RUN_NOT_FOUND')
        for args in [('../report.json', 'compare', {}), (run_id, 'plus20', {}),
                     (run_id, 'compare', {'api_key': 'DO_NOT_LOG_THIS'})]:
            result = f.call(args[0], args[1], **args[2])
            self.assertEqual(result['error']['code'], 'INVALID_ARGUMENT')
            self.assertFalse(result['results'])
        for path in (f.registry / 'traces').glob('*.json'):
            self.assertNotIn('DO_NOT_LOG_THIS', path.read_text(encoding='utf-8'))

    def test_changed_files_are_rejected(self):
        f = self.fixture;run_id = f.register()
        for path in [f.report, f.model, f.manifest, f.rates]:
            original = path.read_bytes();path.write_bytes(original + b' ')
            self.assertEqual(f.call(run_id)['error']['code'], 'INPUT_MISMATCH')
            path.write_bytes(original)

    def test_missing_report_is_unknown_not_not_run(self):
        f = self.fixture;run_id = f.register();f.report.unlink()
        result = f.call(run_id)
        self.assertEqual(result['error']['code'], 'REPORT_NOT_FOUND')
        self.assertEqual(result['execution_status'], 'UNKNOWN')

    def test_partial_report_keeps_base(self):
        f = self.fixture;f.data['status'] = 'CHECK_REQUIRED';f.data['stage'] = 'plus10:run'
        del f.data['studies']['plus10']
        result = f.call(f.register())
        self.assertEqual(result['execution_status'], 'PARTIAL')
        self.assertEqual(set(result['results']), {'base'})
        self.assertIsNone(result['comparison'])
        self.assertEqual(result['error']['code'], 'RESULT_INCOMPLETE')

    def test_missing_pressure_is_null_and_not_zero(self):
        f = self.fixture
        for stage in ['run', 'restart']:
            f.data['studies']['base']['runs'][stage].pop('inlet_pressure_pa_absolute')
        result = f.call(f.register())
        self.assertEqual(result['error']['code'], 'RESULT_INCOMPLETE')
        self.assertIsNone(result['results']['base']['inlet_pressure']['value'])
        self.assertIsNone(result['comparison'])

    def test_solver_failure_keeps_only_the_other_solved_scenario(self):
        # Failed iterations can still export plausible pressure, mass and profile data.
        f = self.fixture
        for state_completed in (True, False):
            with self.subTest(state_completed=state_completed):
                stage = f.data['studies']['plus10']['runs']['restart']
                stage['checks'].update(completed=state_completed, no_solver_errors=False)
                stage['status'] = f.data['status'] = 'CHECK_REQUIRED'
                run_id = f.register()
                for scenario, expected_results, expected_status in (
                        ('compare', {'base'}, 'PARTIAL'), ('plus10', set(), 'FAILED')):
                    result = f.call(run_id, scenario)
                    self.assertEqual(result['tool_status'], 'ERROR')
                    self.assertEqual(result['error']['code'], 'CALCULATION_FAILED')
                    self.assertEqual(result['execution_status'], expected_status)
                    self.assertEqual(set(result['results']), expected_results)
                    self.assertIsNone(result['comparison'])
                    self.assertNotEqual(result['numerical_validation'], 'OK')
                    trace = f.registry / 'traces' / (result['trace_id'] + '.json')
                    self.assertEqual(json.loads(trace.read_text(encoding='utf-8'))['result'], result)

    def test_global_input_mismatch_is_not_pass(self):
        f = self.fixture;f.data['status'] = 'CHECK_REQUIRED'
        f.data['input_mismatch'] = {'check': 'flow_correlations'}
        result = f.call(f.register())
        self.assertEqual(result['error']['code'], 'INPUT_MISMATCH')
        self.assertEqual(result['numerical_validation'], 'OUT_OF_TOLERANCE')
        self.assertEqual(result['results']['base']['numerical_validation'], 'NOT_ASSESSED')
        self.assertIsNone(result['comparison'])

    def test_false_checks_with_pass_label_are_rejected(self):
        f = self.fixture;f.data['studies']['base']['runs']['restart']['checks']['liquid_balance'] = False
        self.assertEqual(f.call(f.register())['error']['code'], 'REPORT_INVALID')

    def test_false_repeatability_label_is_rejected(self):
        f = self.fixture;f.data['studies']['base']['runs']['restart']['inlet_pressure_pa_absolute'] += 500
        self.assertEqual(f.call(f.register())['error']['code'], 'REPORT_INVALID')

    def test_negative_absolute_pressure_is_rejected(self):
        f = self.fixture
        for stage in ['run', 'restart']:
            f.data['studies']['base']['runs'][stage]['inlet_pressure_pa_absolute'] = -1
        self.assertEqual(f.call(f.register())['error']['code'], 'REPORT_INVALID')

    def test_reversed_temperature_range_is_rejected(self):
        f = self.fixture
        for stage in ['run', 'restart']:
            f.data['studies']['base']['runs'][stage]['profile']['temperature_min_c'] = 25.0
        self.assertEqual(f.call(f.register())['error']['code'], 'REPORT_INVALID')

    def test_run_only_does_not_confirm_repeatability(self):
        f = self.fixture;f.data['status'] = 'CHECK_REQUIRED'
        f.data['studies']['base']['runs'].pop('restart')
        result = f.call(f.register(), 'base')
        self.assertEqual(result['results']['base']['result_origin'], 'run')
        self.assertEqual(result['results']['base']['repeatability'], 'NOT_ASSESSED')

    def test_nan_and_duplicate_json_keys_are_rejected(self):
        f = self.fixture
        for text in ['{"status": NaN}', '{"status": "ERROR", "status": "IN_PROGRESS"}']:
            f.report.write_text(text, encoding='utf-8')
            with self.assertRaises(ReportError) as caught:
                Registry(f.registry).register(f.report, f.model, f.rates)
            self.assertEqual(caught.exception.payload.code, 'REPORT_INVALID')

    def test_real_stdio_client_server(self):
        f = self.fixture;run_id = f.register()
        original = {p: p.read_bytes() for p in [f.report, f.model, f.manifest, f.rates]}
        result = anyio.run(check_server, f.registry, run_id)
        self.assertEqual(result['status'], 'MCP_CHECK_PASSED', result['checks'])
        self.assertTrue(all(result['checks'].values()))
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
