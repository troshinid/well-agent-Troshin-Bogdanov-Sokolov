"""Isolated build decisions. A real PIPESIM run remains a Windows integration check."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import anyio

from src.well_agent_mcp.model_preparation import Preparation, sha
from src.well_agent_mcp.results import write_json


class ModelBuildTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        self.service = Preparation(data / 'mcp_registry', data, data / 'fake_toolkit')
        self.service.base.parent.mkdir()
        self.service.base.write_bytes(b'fixture')
        write_json(self.service.rates, {'base_stock_rates': {'liquid_m3_day': 10},
                                        'plus10_stock_rates': {'liquid_m3_day': 11}})
        write_json(self.service.base.with_suffix('.json'), {
            'status': 'FLUID_MODEL_VERIFIED', 'model_sha256': sha(self.service.base),
            'input_hashes': {'scenario_rates_json': sha(self.service.rates)}, 'fluid_id': 't1'})
        self.pipe = data / 'geometry/pipe.json'
        write_json(self.pipe, {'from_node': 'src', 'pipe_id': 'line', 'to_node': 'sink',
                               'outer_diameter_mm': 159, 'inner_diameter_mm': 135, 'wall_thickness_mm': 12,
                               'roughness_mm': 0.2, 'reference_length_m': 100, 'profile': 'profile.csv'})
        self.pipe.with_name('profile.csv').write_text(
            'point_id,station,horizontal_distance_m,axis_distance_m,pipe_bottom_m,pipe_axis_m,source_pdf_page,review_status\n'
            '1,A,0,0,0,0.0795,1,TEAM_REVIEWED\n2,B,100,100,0,0.0795,1,TEAM_REVIEWED\n', encoding='utf-8')
        self.confirm = AsyncMock(return_value=True)
        self.plan_id, self.plan = self.service.plan()

    def apply(self):
        return anyio.run(self.service.call, 'apply_approved_changes', {'plan_id': self.plan_id}, self.confirm)

    def test_changed_inputs_do_not_prompt_or_start(self):
        self.service.execute = Mock()
        pipe = json.loads(self.pipe.read_text(encoding='utf-8'))
        write_json(self.pipe, dict(pipe, roughness_mm=0.3))
        self.assertEqual(self.apply()['status'], 'PLAN_CHANGED')
        self.confirm.assert_not_called()
        self.service.execute.assert_not_called()

    def test_verified_repeat_and_modified_copy(self):
        def simulated_build(folder):
            model = folder / 'network.pips'
            model.write_bytes(b'simulated network')
            write_json(folder / 'network.json', {'status': 'NETWORK_STRUCTURE_VERIFIED', 'model_sha256': sha(model),
                       'source_fluid_model_unchanged': True, 'after_reopen': {
                           'status': 'PASS', 'geometry_points': 2, 'geometry_matches': True, 'connections_match': True},
                       'input_hashes': dict(self.plan['input_hashes'], pipe_json=sha(folder / 'inputs/pipe.json'))})
            (folder / 'exit.txt').write_text('0', encoding='ascii')
        self.service.execute = Mock(side_effect=simulated_build)
        result = self.apply()
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertEqual(self.apply()['status'], 'ALREADY_BUILT')
        Path(result['model']).write_bytes(b'changed')
        self.assertEqual(self.apply()['status'], 'CHECK_REQUIRED')
        self.service.execute.assert_called_once()

    def test_pending_attempt_is_not_submitted_twice(self):
        self.service.execute = Mock()
        self.assertEqual(self.apply()['status'], 'PENDING')
        self.assertEqual(self.apply()['status'], 'PENDING')
        self.service.execute.assert_called_once()

    def test_cli_decline_over_real_stdio(self):
        result = subprocess.run([sys.executable, 'agent.py', 'prepare-model', '--data', str(self.service.data)],
                                cwd=Path(__file__).resolve().parents[1], input=b'\n', capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertIn(b'PLAN_READY', result.stdout)
        self.assertIn(b'DECLINED', result.stdout)
        traces = [json.loads(p.read_text(encoding='utf-8')) for p in (self.service.registry / 'traces').glob('*.json')]
        self.assertEqual({r['tool'] for r in traces}, {'prepare_model', 'apply_approved_changes'})
        self.assertTrue(all(r['plan_id'] == self.plan_id for r in traces))
        self.assertFalse((self.service.registry / 'model_builds').exists())
