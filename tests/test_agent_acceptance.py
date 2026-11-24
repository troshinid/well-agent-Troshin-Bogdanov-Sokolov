"""Acceptance harness checks with simulated HTTP and synthetic reports only."""

import copy
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from scripts.check_agent_acceptance import CASES, ReadGuard, run_suite
from src.agent.routerai import AgentError, RouterAIPlanner, DEFAULT_MODEL
from src.well_agent_mcp.results import ReadResults
from test_agent import KEY
from test_mcp_read_results import Fixture


class AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = Fixture(self.temp.name)
        self.run_id = self.fixture.register()
        self.calls = []
        self.backend_calls = []
        self.overrides = {}
        self.status = 200

    def respond(self, request):
        body = json.loads(request.content)
        text = body['messages'][-1]['content']
        self.calls.append(text)
        if self.status != 200:
            return httpx.Response(self.status, json={'error': {'message': KEY}})
        case = next(case for case in CASES if case[1] == text)
        task = self.overrides.get(case[0], dict(zip(('action', 'scenario', 'reason'), case[2:5])))
        return httpx.Response(200, json={'model': DEFAULT_MODEL, 'choices': [
            {'finish_reason': 'stop', 'message': {'content': json.dumps(task)}}]})

    @asynccontextmanager
    async def backend(self, registry):
        owner = self

        class Gateway:
            async def call(self, name, arguments):
                owner.backend_calls.append(name)
                if name != 'read_results':
                    raise AssertionError('Unexpected non-read operation')
                return ReadResults(registry).call(arguments)

        yield Gateway()

    async def suite(self, directory='acceptance'):
        planner = RouterAIPlanner(KEY, transport=httpx.MockTransport(self.respond))
        return await run_suite(self.fixture.registry, self.run_id,
                               self.root / directory, planner, self.backend)

    async def test_complete_suite_reuses_plan_and_preserves_evidence(self):
        summary = await self.suite()
        self.assertEqual(summary['status'], 'PASS')
        self.assertEqual(summary['passed'], 11)
        self.assertEqual(summary['not_run'], 0)
        self.assertEqual(len(self.calls), 9)
        self.assertEqual(self.backend_calls, ['read_results'] * 4)
        self.assertEqual(summary['checks'][0]['llm']['model_returned'], DEFAULT_MODEL)
        self.assertEqual(summary['checks'][-2]['llm_calls'], 0)
        self.assertEqual(summary['checks'][-1]['observed_error'], 'REQUEST_CONFLICT')
        self.assertNotIn(KEY, (self.root / 'acceptance/summary.json').read_text(encoding='utf-8'))
        before = copy.deepcopy(self.calls)
        with self.assertRaises(FileExistsError):
            await self.suite()
        self.assertEqual(self.calls, before)

    async def test_wrong_read_on_refusal_is_blocked_and_fails(self):
        self.overrides['injection'] = {'action': 'read_results', 'scenario': 'compare', 'reason': 'SUPPORTED'}
        summary = await self.suite()
        case = next(row for row in summary['checks'] if row['name'] == 'injection')
        self.assertFalse(case['passed'])
        self.assertEqual(case['observed_error'], 'ACCEPTANCE_TOOL_BLOCKED')
        self.assertEqual(summary['status'], 'FAIL')
        self.assertEqual(self.backend_calls, ['read_results'] * 4)

    async def test_wrong_run_on_refusal_is_denied_without_backend(self):
        self.overrides['change_temperature'] = {'action': 'run_steady_state', 'scenario': 'compare', 'reason': 'SUPPORTED'}
        summary = await self.suite()
        case = next(row for row in summary['checks'] if row['name'] == 'change_temperature')
        self.assertFalse(case['passed'])
        self.assertEqual(case['observed_error'], 'RUN_NOT_ALLOWED')
        self.assertEqual(self.backend_calls, ['read_results'] * 4)

    async def test_auth_failure_stops_further_api_calls(self):
        self.status = 401
        summary = await self.suite()
        self.assertEqual(summary['status'], 'INCOMPLETE')
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(summary['not_run'], 10)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.backend_calls, [])

    async def test_guard_blocks_run_even_when_reads_allowed(self):
        guard = ReadGuard(self.backend, allowed=True)
        async with guard.connect(self.fixture.registry) as gateway:
            with self.assertRaises(AgentError) as caught:
                await gateway.call('run_steady_state', {})
        self.assertEqual(caught.exception.code, 'ACCEPTANCE_TOOL_BLOCKED')
        self.assertEqual(self.backend_calls, [])


if __name__ == '__main__':
    unittest.main()
