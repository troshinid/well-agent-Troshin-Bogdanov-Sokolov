"""Runner orchestration tests. Fake executor only; this is NOT a PIPESIM test."""

import copy
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import patch
from uuid import uuid4

import anyio
from jsonschema import Draft202012Validator
from mcp.shared.memory import create_connected_server_and_client_session, create_client_server_memory_streams

from test_mcp_read_results import Fixture
from src.well_agent_mcp.results import ReadResults, ReportError, checksum, write_json
from src.well_agent_mcp.runner import RunSteadyState, WindowsExecutor, profile_config
from src.well_agent_mcp.runner import REPOSITORY
from src.well_agent_mcp.schemas import RUN_OUTPUT_SCHEMA
from src.well_agent_mcp.server import create_server
from src.well_agent_mcp.__main__ import check_run_server


class FakeProcess:
    pid = 12345

    def __init__(self, exit_code):
        self.returncode = exit_code

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired('synthetic-worker', timeout)
        return self.returncode

    def poll(self):
        return self.returncode


class FakeExecutor:
    def __init__(self, fixture, mode='success'):
        self.fixture, self.mode, self.calls = fixture, mode, []

    def finish(self, folder, partial=False):
        f = self.fixture
        calculation = folder / 'calculation';calculation.mkdir()
        model = calculation / 'isothermal_auto.pips';model.write_bytes(b'synthetic result model')
        report = copy.deepcopy(f.data)
        report.update(model_file=str(model), model_sha256=checksum(model.read_bytes()),
                      input_configuration_mode='READ_ONLY_PREPARED_MODEL', prepared_model_unchanged=True,
                      prepared_model_sha256=f.data['model_sha256'], seed_report_sha256=checksum(f.report.read_bytes()),
                      script_sha256=checksum((REPOSITORY / 'scripts/run_network_model.py').read_bytes()))
        if partial:
            report['status'] = 'CHECK_REQUIRED';report['stage'] = 'plus10:run'
            del report['studies']['plus10']
        write_json(calculation / 'report.json', report)
        (folder / 'exit_code.txt').write_text('1' if partial else '0', encoding='ascii')

    def start(self, profile, files, folder):
        self.calls.append(folder)
        if self.mode == 'spawn_failure':
            raise ReportError('TOOLKIT_UNAVAILABLE', 'toolkit_root', 'synthetic unavailable Toolkit')
        (folder / 'worker.log').write_text('Python toolkit license is checked out successfully\n', encoding='utf-8')
        if self.mode == 'timeout':
            return FakeProcess(None)
        if self.mode == 'license_failure':
            (folder / 'worker.log').write_text('License checkout failed: no available licenses', encoding='utf-8')
        if self.mode in ['license_failure', 'worker_failure']:
            (folder / 'exit_code.txt').write_text('1', encoding='ascii')
            return FakeProcess(1)
        if self.mode == 'no_receipt':
            return FakeProcess(2)
        if self.mode == 'zero_without_report':
            (folder / 'exit_code.txt').write_text('0', encoding='ascii')
            return FakeProcess(0)
        partial = self.mode in ['partial', 'partial_license']
        self.finish(folder, partial=partial)
        if self.mode == 'partial_license':
            (folder / 'worker.log').write_text('License checkout failed', encoding='utf-8')
        return FakeProcess(1 if partial else 0)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        f = self.fixture = Fixture(self.temp.name)
        self.prepared = f.root / 'prepared.pips';self.prepared.write_bytes(b'synthetic prepared model')
        f.data['model_sha256'] = checksum(self.prepared.read_bytes())
        self.seed = f.register()
        self.toolkit = f.root / 'toolkit';(self.toolkit / 'Scripts').mkdir(parents=True)
        (self.toolkit / 'python.exe').write_bytes(b'fake')
        (self.toolkit / 'Scripts/activate.bat').write_bytes(b'fake')
        self.output = f.root / 'jobs'
        profile_config(f.registry, self.seed, self.prepared, self.toolkit, self.output, 1)

    def arguments(self):
        return {'seed_run_id': self.seed, 'request_id': str(uuid4()), 'scenario_set': 'base_plus10'}

    def call(self, service, args):
        result = service.call(args)
        Draft202012Validator(RUN_OUTPUT_SCHEMA).validate(result)
        return result

    def test_new_run_then_same_request_is_not_submitted_again(self):
        executor = FakeExecutor(self.fixture);service = RunSteadyState(self.fixture.registry, executor)
        args = self.arguments();first = self.call(service, args);second = self.call(service, args)
        self.assertEqual(first['tool_status'], 'SUCCESS')
        self.assertEqual(first['job_state'], 'FINISHED')
        self.assertEqual(first['worker_exit_code'], 0)
        self.assertNotEqual(first['run_id'], self.seed)
        self.assertTrue(second['reused']);self.assertEqual(len(executor.calls), 1)
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertNotEqual(first['trace_id'], second['trace_id'])
        self.assertEqual(first['results'], second['results'])
        self.assertEqual(first['physical_validation'], 'NOT_ASSESSED')
        read = ReadResults(self.fixture.registry).call({'run_id': first['run_id'], 'scenario': 'compare'})
        self.assertEqual(read['results'], first['results'])
        self.assertEqual(len(first['warnings']), 2)
        self.assertEqual(self.prepared.read_bytes(), b'synthetic prepared model')

    def test_bad_arguments_never_start_a_process(self):
        executor = FakeExecutor(self.fixture);service = RunSteadyState(self.fixture.registry, executor)
        for extra in [{'scenario_set': 'arbitrary'}, {'command': 'python'}, {'pressure': 12}, {'request_id': '../bad'}]:
            args = dict(self.arguments(), **extra)
            self.assertEqual(self.call(service, args)['error']['code'], 'INVALID_ARGUMENT')
        self.assertFalse(executor.calls)

    def test_missing_configuration_and_unknown_seed(self):
        service = RunSteadyState(self.fixture.registry, FakeExecutor(self.fixture))
        (self.fixture.registry / 'runner.json').unlink()
        self.assertEqual(self.call(service, self.arguments())['error']['code'], 'RUNNER_NOT_CONFIGURED')
        self.assertEqual(self.call(service, dict(self.arguments(), seed_run_id=str(uuid4())))['error']['code'], 'RUN_NOT_FOUND')

    def test_prepared_model_or_code_changed_blocks_start(self):
        executor = FakeExecutor(self.fixture);service = RunSteadyState(self.fixture.registry, executor)
        original = self.prepared.read_bytes();self.prepared.write_bytes(original + b'change')
        self.assertEqual(self.call(service, self.arguments())['error']['code'], 'INPUT_MISMATCH')
        self.prepared.write_bytes(original)
        path = self.fixture.registry / 'runner.json';config = json.loads(path.read_text())
        config['code_hashes']['scripts/run_network_model.py'] = '0' * 64;write_json(path, config)
        self.assertEqual(self.call(service, self.arguments())['error']['code'], 'INPUT_MISMATCH')
        self.assertFalse(executor.calls)

    def test_same_request_with_changed_profile_is_conflict(self):
        executor = FakeExecutor(self.fixture);service = RunSteadyState(self.fixture.registry, executor)
        args = self.arguments();self.call(service, args)
        profile_config(self.fixture.registry, self.seed, self.prepared, self.toolkit, self.output, 2)
        self.assertEqual(self.call(service, args)['error']['code'], 'REQUEST_CONFLICT')
        self.assertEqual(len(executor.calls), 1)

    def test_timeout_does_not_kill_or_resubmit_and_can_reconcile_after_restart(self):
        executor = FakeExecutor(self.fixture, 'timeout');service = RunSteadyState(self.fixture.registry, executor)
        args = self.arguments();pending = self.call(service, args)
        self.assertEqual(pending['error']['code'], 'TIMEOUT')
        self.assertEqual(pending['job_state'], 'OUTCOME_UNKNOWN')
        self.assertEqual(pending['execution_status'], 'UNKNOWN')
        other_executor = FakeExecutor(self.fixture)
        restarted_server = RunSteadyState(self.fixture.registry, other_executor)
        self.assertEqual(self.call(restarted_server, args)['error']['code'], 'TIMEOUT')
        self.assertEqual(self.call(restarted_server, self.arguments())['error']['code'], 'BUSY')
        self.assertEqual(len(executor.calls), 1);self.assertEqual(len(other_executor.calls), 0)
        executor.finish(executor.calls[0])
        finished = self.call(restarted_server, args)
        self.assertEqual(finished['tool_status'], 'SUCCESS')
        self.assertTrue(finished['reused'])
        self.assertFalse((self.fixture.registry / 'runner.lock').exists())

    def test_partial_results_are_preserved(self):
        service = RunSteadyState(self.fixture.registry, FakeExecutor(self.fixture, 'partial'))
        result = self.call(service, self.arguments())
        self.assertEqual(result['tool_status'], 'ERROR')
        self.assertEqual(result['execution_status'], 'PARTIAL')
        self.assertEqual(result['error']['code'], 'RESULT_INCOMPLETE')
        self.assertEqual(set(result['results']), {'base'})
        self.assertIsNone(result['comparison']);self.assertIsNotNone(result['run_id'])

    def test_license_failure_is_distinct_from_normal_license_message(self):
        for mode, expected in [('license_failure', 'LICENSE_UNAVAILABLE'), ('worker_failure', 'WORKER_FAILED')]:
            with self.subTest(mode=mode):
                result = self.call(RunSteadyState(self.fixture.registry, FakeExecutor(self.fixture, mode)), self.arguments())
                self.assertEqual(result['error']['code'], expected)
                self.assertEqual(result['worker_exit_code'], 1)

    def test_license_failure_with_partial_report_keeps_base(self):
        service = RunSteadyState(self.fixture.registry, FakeExecutor(self.fixture, 'partial_license'))
        result = self.call(service, self.arguments())
        self.assertEqual(result['error']['code'], 'LICENSE_UNAVAILABLE')
        self.assertEqual(result['execution_status'], 'PARTIAL')
        self.assertEqual(set(result['results']), {'base'})

    def test_zero_exit_without_report_is_not_success(self):
        executor = FakeExecutor(self.fixture, 'zero_without_report')
        service = RunSteadyState(self.fixture.registry, executor)
        args = self.arguments();first = self.call(service, args);again = self.call(service, args)
        self.assertEqual(first['error']['code'], 'RESULT_INCOMPLETE')
        self.assertEqual(first['execution_status'], 'UNKNOWN')
        self.assertEqual(again['error']['code'], 'RESULT_INCOMPLETE')
        self.assertEqual(len(executor.calls), 1)

    def test_spawn_failure_is_recorded_as_not_started(self):
        executor = FakeExecutor(self.fixture, 'spawn_failure');service = RunSteadyState(self.fixture.registry, executor)
        args = self.arguments()
        self.assertEqual(self.call(service, args)['error']['code'], 'TOOLKIT_UNAVAILABLE')
        again = self.call(service, args)
        self.assertEqual(again['execution_status'], 'NOT_RUN')
        self.assertEqual(again['error']['code'], 'TOOLKIT_UNAVAILABLE')
        self.assertEqual(len(executor.calls), 1)

    def test_failure_without_receipt_remains_failed_on_repeat(self):
        executor = FakeExecutor(self.fixture, 'no_receipt');service = RunSteadyState(self.fixture.registry, executor)
        args = self.arguments();self.call(service, args)
        self.assertEqual(self.call(service, args)['error']['code'], 'WORKER_FAILED')
        self.assertEqual(len(executor.calls), 1)

    def test_changed_completed_report_cannot_be_registered_again_under_same_request(self):
        executor = FakeExecutor(self.fixture);service = RunSteadyState(self.fixture.registry, executor)
        args = self.arguments();self.call(service, args)
        report = executor.calls[0] / 'calculation/report.json'
        report.write_bytes(report.read_bytes() + b' ')
        self.assertEqual(self.call(service, args)['error']['code'], 'INPUT_MISMATCH')
        self.assertEqual(len(executor.calls), 1)

    def test_deleted_completed_report_is_not_resubmitted(self):
        executor = FakeExecutor(self.fixture);service = RunSteadyState(self.fixture.registry, executor)
        args = self.arguments();self.call(service, args)
        (executor.calls[0] / 'calculation/report.json').unlink()
        self.assertEqual(self.call(service, args)['error']['code'], 'REPORT_NOT_FOUND')
        self.assertEqual(len(executor.calls), 1)

    def test_concurrent_retry_does_not_unseal_a_completed_report(self):
        waiting, release = threading.Event(), threading.Event()
        class SlowReturn(FakeProcess):
            def wait(self, timeout):
                waiting.set()
                if not release.wait(5):
                    raise subprocess.TimeoutExpired('test synchronization', 5)
                return 0
        class Executor(FakeExecutor):
            def start(self, profile, files, folder):
                super().start(profile, files, folder)
                return SlowReturn(0)
        executor = Executor(self.fixture);service = RunSteadyState(self.fixture.registry, executor)
        args = self.arguments();first = []
        thread = threading.Thread(target=lambda: first.append(service.call(args)))
        thread.start()
        try:
            self.assertTrue(waiting.wait(5))
            retry = self.call(service, args)
            self.assertEqual(retry['tool_status'], 'SUCCESS')
            path = executor.calls[0] / 'calculation/report.json'
            path.write_bytes(path.read_bytes() + b' ')
        finally:
            release.set();thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first[0]['error']['code'], 'INPUT_MISMATCH')
        self.assertEqual(len(executor.calls), 1)

    def test_windows_bridge_uses_fixed_command_and_private_log(self):
        f = self.fixture
        profile = json.loads((f.registry / 'runner.json').read_text())
        files = json.loads((f.registry / (self.seed + '.json')).read_text())['files']
        folder = f.root / 'command-test';folder.mkdir()
        with patch('src.well_agent_mcp.runner.sys.platform', 'win32'), patch.dict(os.environ, {'SystemRoot': str(f.root)}), \
                patch('src.well_agent_mcp.runner.subprocess.Popen', return_value=FakeProcess(0)) as popen:
            WindowsExecutor().start(profile, files, folder)
        command = popen.call_args.args[0];kwargs = popen.call_args.kwargs
        self.assertEqual(command[1:], ['/d', '/v:off', '/c', 'scripts\\pipesim_worker.cmd'])
        self.assertNotIn('shell', kwargs)
        self.assertEqual(kwargs['stdout'].name, str(folder / 'worker.log'))
        self.assertEqual(kwargs['env']['WELL_AGENT_PTK_ROOT'], str(self.toolkit))
        self.assertEqual(kwargs['env']['WELL_AGENT_PREPARED_MODEL'], str(self.prepared))

    def test_real_mcp_exchange_with_fake_executor(self):
        executor = FakeExecutor(self.fixture);service = RunSteadyState(self.fixture.registry, executor)
        async def exercise():
            async with create_connected_server_and_client_session(create_server(self.fixture.registry, service)) as client:
                tools = await client.list_tools()
                self.assertEqual({t.name for t in tools.tools}, {'read_results', 'run_steady_state'})
                args = self.arguments()
                first = await client.call_tool('run_steady_state', arguments=args)
                again = await client.call_tool('run_steady_state', arguments=args)
                self.assertFalse(first.isError);self.assertFalse(again.isError)
                self.assertTrue(again.structuredContent['reused'])
                self.assertEqual(first.structuredContent['run_id'], again.structuredContent['run_id'])
                Draft202012Validator(RUN_OUTPUT_SCHEMA).validate(first.structuredContent)
                bad = await client.call_tool('run_steady_state', arguments=dict(args, command='bad'))
                self.assertTrue(bad.isError);self.assertEqual(bad.structuredContent['error']['code'], 'INVALID_ARGUMENT')
        anyio.run(exercise)
        self.assertEqual(len(executor.calls), 1)

    def test_run_check_client_flow_with_fake_executor(self):
        executor = FakeExecutor(self.fixture);service = RunSteadyState(self.fixture.registry, executor)
        @asynccontextmanager
        async def memory_transport(_parameters):
            server = create_server(self.fixture.registry, service)
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                async with anyio.create_task_group() as group:
                    group.start_soon(server.run, *server_streams, server.create_initialization_options())
                    try:
                        yield client_streams
                    finally:
                        group.cancel_scope.cancel()
        async def exercise():
            with patch('src.well_agent_mcp.__main__.stdio_client', memory_transport):
                report = await check_run_server(self.fixture.registry, self.seed, str(uuid4()))
            self.assertEqual(report['status'], 'MCP_RUN_CHECK_PASSED', report['checks'])
            self.assertTrue(all(report['checks'].values()))
        anyio.run(exercise)
        self.assertEqual(len(executor.calls), 1)


if __name__ == '__main__':
    unittest.main()
