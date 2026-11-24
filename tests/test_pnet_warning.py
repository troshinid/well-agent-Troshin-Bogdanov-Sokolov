"""Numerical observations must never become a claim that a warning is resolved."""

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.check_pnet_warning import analyse, collect
from test_mcp_read_results import Fixture


class PnetDiagnosticTests(unittest.TestCase):
    def test_ratio_matches_warning_but_cause_remains_unestablished(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            raw = fixture.data['studies']['base']['runs']['run']
            raw['warnings'] = ['Warning 26007903 from PNET_3 : Branch agzu6_out: branch error is significant (18.18182%)!']
            raw['pressure_components_pa'] = {'CumulativeFrictionPressureDifference': 100,
                'CumulativeElevationPressureDifference': 199899.9,
                'CumulativeAccelerationPressureDifference': 0.1}
            result = analyse(fixture.data)
            stage = result['stages'][0]
            self.assertTrue(stage['warning_matches'][0]['matches_at_5_decimal_places'])
            self.assertAlmostEqual(stage['drop_minus_components_pa'], 0)
            self.assertEqual(result['assessment'], 'CAUSE_NOT_ESTABLISHED')
            self.assertEqual(result['physical_validation'], 'NOT_ASSESSED')

    def test_missing_components_or_stages_are_not_zero_filled(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.data['studies'].pop('plus10')
            result = analyse(fixture.data)
            self.assertIsNone(result['stages'][0]['component_sum_pa'])
            self.assertIsNone(result['stages'][0]['drop_minus_components_pa'])
            self.assertFalse(result['all_stages_available'])
            self.assertIsNone(result['run_restart']['plus10']['inlet_pressure_pa_absolute_restart_minus_run_pa'])

    def test_archive_preserves_source_files_and_excludes_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outputs:
            fixture = Fixture(directory)
            run_id = fixture.register()
            stage = fixture.root / 'base/run'
            stage.mkdir(parents=True)
            (stage / 'engine_console.txt').write_text('Synthetic diagnostic message')
            (fixture.root / '.env').write_text('DO_NOT_COLLECT')
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in fixture.root.rglob('*') if p.is_file()}
            output = Path(outputs) / 'diagnostic.zip'
            result = collect(fixture.registry, run_id, output)
            self.assertEqual(len(result['missing_stage_artifacts']), 7)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), {'report.json', 'base/run/engine_console.txt', 'diagnostic.json'})
                self.assertEqual(json.loads(archive.read('diagnostic.json'))['run_id'], run_id)
                self.assertEqual(archive.read('report.json'), fixture.report.read_bytes())
            for path, digest in before.items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            with self.assertRaises(FileExistsError):
                collect(fixture.registry, run_id, output)

    def test_invalid_or_changed_registered_run_cannot_be_collected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outputs:
            fixture = Fixture(directory)
            run_id = fixture.register()
            with self.assertRaises(ValueError):
                collect(fixture.registry, '../report', Path(outputs) / 'invalid.zip')
            with self.assertRaises(ValueError):
                collect(fixture.registry, run_id, fixture.root / 'output.zip')
            fixture.report.write_text('{}')
            from src.well_agent_mcp.results import ReportError
            with self.assertRaises(ReportError):
                collect(fixture.registry, run_id, Path(outputs) / 'changed.zip')


if __name__ == '__main__':
    unittest.main()
