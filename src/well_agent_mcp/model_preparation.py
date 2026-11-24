"""Plan a fixed network build from local inputs and verify the new copy."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import anyio
from jsonschema import Draft202012Validator
from mcp.types import Tool

from .results import utc_now, write_json
from .runner import REPOSITORY, safe_command_path
from .runtime_environment import server_environment

SCRIPTS = REPOSITORY / 'scripts'
# The Toolkit scripts also run standalone on Python 3.8 and import each other by name.
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))
from build_network_model import prepare, read_json, sha

PLAN_SCHEMA = {'type': 'object', 'properties': {}, 'additionalProperties': False}
APPLY_SCHEMA = {'type': 'object', 'properties': {'plan_id': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}},
                'required': ['plan_id'], 'additionalProperties': False}
TOOLS = [
    Tool(name='prepare_model', description='Проверить манифест и показать план сборки сети АГЗУ-6.', inputSchema=PLAN_SCHEMA),
    Tool(name='apply_approved_changes', description='Подтвердить план у оператора, собрать и проверить копию.',
         inputSchema=APPLY_SCHEMA),
]


class Preparation:
    def __init__(self, registry, data, toolkit):
        self.registry = Path(registry).resolve()
        self.data = Path(data).resolve()
        self.toolkit = Path(toolkit).resolve()
        self.base = self.data / 'models/t1_fluid_auto.pips'
        self.rates = self.data / 'fluid/scenario_rates.json'
        self.settings = REPOSITORY / 'config/network_setup.json'

    def inputs(self):
        pipe = self.data / 'geometry/pipe.json'
        if not pipe.is_file():
            reference = read_json(self.data / 'models/agzu6_network_4.json')['input_hashes']['pipe_json']
            pipe = next((p for p in self.data.rglob('pipe.json') if sha(p) == reference), None)
            if pipe is None:
                raise ValueError('Не найден pipe.json, соответствующий проверенной модели участка.')
        profile = (pipe.parent / read_json(pipe)['profile']).resolve()
        profile.relative_to(self.data)
        return pipe, profile

    def plan(self):
        pipe_path, _ = self.inputs()
        fluid, pipe, rates, settings, geometry, _, hashes = prepare(self.base, pipe_path, self.rates, self.settings)
        plan = {
            'source_model': str(self.base), 'output_root': str(self.registry / 'model_builds'),
            'create': {'source': pipe['from_node'], 'flowline': pipe['pipe_id'], 'sink': pipe['to_node']},
            'connect': [[pipe['from_node'], pipe['pipe_id']], [pipe['pipe_id'], pipe['to_node']]],
            'fluid': fluid['fluid_id'], 'profile_points': geometry['points'],
            'horizontal_length_m': geometry['horizontal_length_m'],
            'pipe_mm': {k: pipe[k] for k in ('inner_diameter_mm', 'wall_thickness_mm', 'roughness_mm')},
            'inlet_temperature_c': settings['inlet_temperature_c'],
            'outlet_pressure_bara': settings['outlet_pressure_bar_absolute'],
            'liquid_stock_m3_day': {s: rates[k]['liquid_m3_day'] for s, k in settings['studies'].items()},
            'correlations': settings['flow_correlations'], 'thermal': settings['thermal']['configuration_status'],
            'input_hashes': hashes,
        }
        encoded = json.dumps(plan, sort_keys=True, allow_nan=False).encode()
        return hashlib.sha256(encoded).hexdigest(), plan

    def snapshot(self, folder, hashes):
        pipe, profile = self.inputs()
        inputs = folder / 'inputs'
        inputs.mkdir()
        sources = {'fluid_model': (self.base, 'fluid.pips'), 'fluid_manifest': (self.base.with_suffix('.json'), 'fluid.json'),
                   'rates_json': (self.rates, 'rates.json'), 'network_setup_json': (self.settings, 'settings.json'),
                   'profile_csv': (profile, 'profile.csv'), 'pipe_json': (pipe, 'manifest.json')}
        for key, (path, name) in sources.items():
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != hashes[key]:
                raise ValueError('Изменился вход после подтверждения: ' + key)
            (inputs / name).write_bytes(content)
        # The archived manifest keeps its original hash. The builder reads a local
        # reference so even an absolute original profile path cannot change the run.
        manifest = read_json(inputs / 'manifest.json')
        manifest['profile'] = 'profile.csv'
        write_json(inputs / 'pipe.json', manifest)

    def execute(self, folder):
        if sys.platform != 'win32' or not all((self.toolkit / f).is_file() for f in ('python.exe', 'Scripts/activate.bat')):
            raise ValueError('Для записи нужен Windows с PIPESIM Toolkit. Проверьте --toolkit-root.')
        env = server_environment()
        env.update({'WELL_AGENT_PTK_ROOT': safe_command_path(self.toolkit), 'WELL_AGENT_BUILD': safe_command_path(folder),
                    'PYTHONIOENCODING': 'utf-8'})
        safe_command_path(REPOSITORY)
        command = [str(Path(os.environ['SystemRoot']) / 'System32/cmd.exe'), '/d', '/v:off', '/c',
                   r'scripts\prepare_network_worker.cmd']
        with (folder / 'worker.log').open('xb') as log:
            try:
                process = subprocess.Popen(command, cwd=REPOSITORY, env=env, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT)
            except OSError:
                (folder / 'exit.txt').write_text('1', encoding='ascii')
                raise
        try:
            process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            return
        if not (folder / 'exit.txt').exists():
            (folder / 'exit.txt').write_text(str(process.returncode), encoding='ascii')

    def finished(self, folder, hashes):
        receipt = folder / 'exit.txt'
        if not receipt.exists() or not receipt.read_text(encoding='ascii').strip():
            return {'status': 'PENDING', 'attempt': str(folder)}
        if receipt.read_text(encoding='ascii').strip() != '0':
            return {'status': 'BUILD_FAILED', 'attempt': str(folder)}
        report = read_json(folder / 'network.json')
        after = report['after_reopen']
        model = folder / 'network.pips'
        expected = dict(hashes, pipe_json=sha(folder / 'inputs/pipe.json'))
        if (report['status'] != 'NETWORK_STRUCTURE_VERIFIED' or after['status'] != 'PASS'
                or after['connections_match'] is not True or after['geometry_matches'] is not True
                or report['input_hashes'] != expected or report['model_sha256'] != sha(model)
                or report['source_fluid_model_unchanged'] is not True or sha(self.base) != hashes['fluid_model']):
            raise ValueError('Не пройдена сверка модели, входов и отчёта после сборки.')
        return {'status': 'VERIFIED', 'model': str(model), 'report': str(folder / 'network.json'),
                'after_reopen': after['status'], 'geometry_points': after['geometry_points'],
                'connections_match': after['connections_match'], 'model_sha256': report['model_sha256']}

    def build(self, plan_id, plan, trace_id):
        # Only a confirmed call may reserve an attempt. mkdir elects one writer
        # across server processes. An unconfirmed outcome cannot be resubmitted.
        root = self.registry / 'model_builds' / plan_id
        root.mkdir(parents=True, exist_ok=True)
        index = 1
        while True:
            folder = root / ('attempt-' + str(index))
            try:
                folder.mkdir()
                break
            except FileExistsError:
                previous = self.finished(folder, plan['input_hashes'])
                if previous['status'] != 'BUILD_FAILED':
                    if previous['status'] == 'VERIFIED':
                        previous['status'] = 'ALREADY_BUILT'
                    return previous
                index += 1
        try:
            write_json(folder / 'approval.json', {'plan_id': plan_id, 'approved_at': utc_now(),
                                                  'trace_id': trace_id, 'plan': plan})
            self.snapshot(folder, plan['input_hashes'])
            self.execute(folder)
        except (OSError, ValueError, KeyError):
            # Errors before dispatch are known failures. A launched worker may
            # still own the attempt when the connection is interrupted.
            if not (folder / 'worker.log').exists():
                (folder / 'exit.txt').write_text('1', encoding='ascii')
            raise
        return self.finished(folder, plan['input_hashes'])

    async def call(self, name, args, confirm):
        trace_id = str(uuid4())
        trace = {'trace_id': trace_id, 'tool': name, 'started_at': utc_now()}
        result = {'trace_id': trace_id, 'tool_status': 'ERROR'}
        try:
            schema = PLAN_SCHEMA if name == 'prepare_model' else APPLY_SCHEMA
            if not Draft202012Validator(schema).is_valid(args):
                raise ValueError('Неверные аргументы инструмента.')
            plan_id, plan = await anyio.to_thread.run_sync(self.plan)
            trace.update(plan=plan, plan_id=plan_id)
            result['plan_id'] = plan_id
            if name == 'prepare_model':
                result.update(tool_status='SUCCESS', status='PLAN_READY', plan=plan)
            elif args['plan_id'] != plan_id:
                result.update(status='PLAN_CHANGED', error='Запросите новый план и проверьте изменения.')
            elif not await confirm(plan_id, plan):
                trace['approved'] = False
                result.update(tool_status='SUCCESS', status='DECLINED')
            else:
                trace['approved'] = True
                # Persist intent before any model write, including on disconnect.
                write_json(self.registry / 'traces' / (trace_id + '.json'), trace)
                built = await anyio.to_thread.run_sync(self.build, plan_id, plan, trace_id)
                result.update(built)
                if built['status'] in ('VERIFIED', 'ALREADY_BUILT', 'PENDING'):
                    result['tool_status'] = 'SUCCESS'
        except Exception as error:
            result.update(status='CHECK_REQUIRED', error=str(error))
        trace.update(result=result, finished_at=utc_now())
        write_json(self.registry / 'traces' / (trace_id + '.json'), trace)
        return result
