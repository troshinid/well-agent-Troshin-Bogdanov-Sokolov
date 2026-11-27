"""No paid API calls or PIPESIM. LLM/worker responses are explicitly simulated.

One integration test uses a real MCP stdio client/server with synthetic files.
"""

import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
import httpx
from src.agent.loop import run_agent
from src.agent.routerai import AgentError, RouterAIPlanner, DEFAULT_MODEL
from src.agent.schemas import AgentContext, AgentResult, Task
from src.well_agent_mcp.results import ReadResults
from src.well_agent_mcp.schemas import ReadRequest, ReadResponse, RunResponse
from test_mcp_read_results import Fixture

KEY = 'sk-routerai-test-' + '0' * 64  # Synthetic, never a usable credential.
MODEL = 'test/model'
READ = {'action': 'read_results', 'scenario': 'compare', 'reason': 'SUPPORTED'}
RUN = {'action': 'run_steady_state', 'scenario': 'compare', 'reason': 'SUPPORTED'}


class MockRouterAI:
    def __init__(self, task=None, status=200, raw=None):
        self.task, self.status, self.raw, self.requests = task or READ, status, raw, []

    def handle(self, request):
        assert str(request.url) == 'https://routerai.ru/api/v1/chat/completions'
        assert request.method == 'POST'
        assert request.headers['Authorization'] == 'Bearer ' + KEY
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={'error': {'message': KEY}})
        body = {'model': MODEL, 'choices': [{'finish_reason': 'stop', 'message': {
            'content': self.raw if self.raw is not None else json.dumps(self.task)}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}}
        return httpx.Response(200, json=body)

    def planner(self):
        return RouterAIPlanner(KEY, MODEL, transport=httpx.MockTransport(self.handle))


class FakeGateway:
    def __init__(self, fixture, run_id):
        self.fixture, self.run_id, self.calls, self.jobs = fixture, run_id, [], set()
        self.failure = None

    async def call(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == 'read_results':
            return ReadResults(self.fixture.registry).call(arguments)
        payload = ReadResults(self.fixture.registry).call({'run_id': self.run_id, 'scenario': 'compare'})
        reused = arguments['request_id'] in self.jobs
        self.jobs.add(arguments['request_id'])
        payload.update(seed_run_id=arguments['seed_run_id'], request_id=arguments['request_id'],
                       job_state='FINISHED', worker_exit_code=0, reused=reused)
        if self.failure:
            payload.update(tool_status='ERROR', execution_status='UNKNOWN', numerical_validation='NOT_ASSESSED',
                           run_id=None, results={}, comparison=None, report_ref=None,
                           job_state='OUTCOME_UNKNOWN', worker_exit_code=None,
                           error={'code': 'TIMEOUT', 'field': 'worker', 'message': 'Synthetic timeout'})
        return RunResponse.model_validate(payload).model_dump(mode='json')

    @asynccontextmanager
    async def connect(self, registry):
        yield self


class ContractExampleTests(unittest.TestCase):
    def test_published_examples_match_contract(self):
        examples = Path(__file__).resolve().parents[1] / 'examples'
        request = json.loads((examples / 'request.json').read_text(encoding='utf-8'))
        context = AgentContext.model_validate(request['ctx'])
        success = AgentResult.model_validate_json((examples / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual((success.status, success.request_id), ('SUCCESS', context.request_id))
        self.assertEqual([event['tool'] for event in success.evidence], ['read_results'])
        event = success.evidence[0]
        self.assertEqual(ReadRequest.model_validate(event['arguments']).run_id, context.run_id)
        self.assertEqual(ReadResponse.model_validate(event['payload']).run_id, context.run_id)

        refusal = AgentResult.model_validate_json((examples / 'error.json').read_text(encoding='utf-8'))
        self.assertEqual((refusal.status, refusal.execution_status), ('REFUSED', 'NOT_RUN'))
        self.assertEqual(refusal.error['code'], 'AMBIGUOUS')
        self.assertEqual(refusal.evidence, [])


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name)
        self.run_id = self.f.register()
        self.ctx = AgentContext(registry=self.f.registry, run_id=self.run_id)
        self.gateway = FakeGateway(self.f, self.run_id)

    async def ask(self, mock=None, request='Сравни base и plus10', ctx=None):
        return await run_agent(request, ctx or self.ctx, (mock or MockRouterAI()).planner(), self.gateway.connect)

    def journals(self):
        return '\n'.join(p.read_text(encoding='utf-8') for p in self.f.registry.rglob('*.json'))

    async def test_schema_planning_and_grounded_answer(self):
        mock = MockRouterAI()
        result = await self.ask(mock)
        self.assertEqual(result.status, 'SUCCESS')
        self.assertEqual([c[0] for c in self.gateway.calls], ['read_results'])
        self.assertIn('12 bara', result.answer)
        self.assertIn('15.5555555556 °C, 1.0135293221 бара абсолютного давления', result.answer)
        self.assertIn('Исходные объёмы заданы при 20 °C и 101325 Па', result.answer)
        self.assertIn('Synthetic warning', result.answer)
        self.assertIn('NOT_ASSESSED', result.answer)
        sent = mock.requests[0]
        self.assertNotIn('provider', sent)
        self.assertTrue(sent['response_format']['json_schema']['strict'])
        self.assertFalse(sent['response_format']['json_schema']['schema']['additionalProperties'])
        self.assertNotIn(str(self.f.registry), json.dumps(sent))
        self.assertNotIn(self.run_id, json.dumps(sent))
        self.assertNotIn(KEY, self.journals())

    async def test_read_one_scenario(self):
        result = await self.ask(MockRouterAI(dict(READ, scenario='base')))
        self.assertEqual(set(result.evidence[-1]['payload']['results']), {'base'})
        self.assertNotIn('Разница P', result.answer)

    async def test_run_needs_local_permission(self):
        result = await self.ask(MockRouterAI(RUN), request='Пересчитай base и plus10')
        self.assertEqual(result.status, 'REFUSED')
        self.assertEqual(result.error['code'], 'RUN_NOT_ALLOWED')
        self.assertEqual(self.gateway.calls, [])

    async def test_allowed_run_and_read_and_repeat_checkpoint(self):
        mock = MockRouterAI(RUN)
        ctx = self.ctx.model_copy(update={'allow_run': True})
        first = await self.ask(mock, 'Пересчитай base и plus10', ctx)
        again = await self.ask(mock, 'Пересчитай base и plus10', ctx)
        self.assertEqual(first.status, 'SUCCESS')
        self.assertEqual(again.status, 'SUCCESS')
        self.assertEqual(len(mock.requests), 1)
        self.assertEqual(len(self.gateway.jobs), 1)
        self.assertEqual(self.gateway.calls[0][1], {'seed_run_id': self.run_id,
                         'request_id': self.ctx.request_id, 'scenario_set': 'base_plus10'})
        self.assertTrue(again.evidence[0]['payload']['reused'])
        self.assertIn('Повтор request_id', again.answer)
        self.assertNotEqual(first.trace_id, again.trace_id)

    async def test_changed_request_cannot_reuse_id(self):
        await self.ask()
        result = await self.ask(request='Покажи только base')
        self.assertEqual(result.error['code'], 'REQUEST_CONFLICT')
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_timeout_keeps_id_and_never_reads_old_result(self):
        self.gateway.failure = True
        ctx = self.ctx.model_copy(update={'allow_run': True})
        result = await self.ask(MockRouterAI(RUN), 'Пересчитай base и plus10', ctx)
        self.assertEqual(result.status, 'CHECK_REQUIRED')
        self.assertEqual(result.execution_status, 'UNKNOWN')
        self.assertEqual(result.error['code'], 'TIMEOUT')
        self.assertEqual([c[0] for c in self.gateway.calls], ['run_steady_state'])
        self.assertNotIn('P входа =', result.answer)
        self.assertIn('тот же request_id', result.answer)

    async def test_unapproved_changes_and_injection_refused(self):
        # Simulate LLM classification, not a claim that a real LLM passed injection tests.
        for reason in ['MODEL_CHANGE', 'UNTRUSTED_INSTRUCTION', 'AMBIGUOUS', 'OUT_OF_SCOPE']:
            with self.subTest(reason=reason):
                ctx = self.ctx.model_copy(update={'request_id': str(uuid4()), 'allow_run': True})
                result = await self.ask(MockRouterAI({'action': 'refuse', 'scenario': 'compare', 'reason': reason}), ctx=ctx)
                self.assertEqual(result.status, 'REFUSED')
                self.assertFalse(result.evidence)
        self.assertEqual(self.gateway.calls, [])

    async def test_llm_cannot_add_parameters_paths_or_results(self):
        for extra in [{'pressure': 12}, {'command': 'cmd.exe'}, {'run_id': str(uuid4())}, {'answer': 'P=999'}]:
            with self.subTest(extra=extra):
                result = await self.ask(MockRouterAI(dict(READ, **extra)))
                self.assertEqual(result.error['code'], 'LLM_INVALID_RESPONSE')
                self.assertFalse(result.evidence)
        self.assertEqual(self.gateway.calls, [])

    async def test_invalid_json_and_inconsistent_task(self):
        for raw in ['not json', '{"action":"read_results","action":"refuse"}',
                    json.dumps(dict(READ, reason='MODEL_CHANGE')), json.dumps(dict(RUN, scenario='base'))]:
            with self.subTest(raw=raw):
                result = await self.ask(MockRouterAI(raw=raw))
                self.assertEqual(result.error['code'], 'LLM_INVALID_RESPONSE')
        self.assertEqual(self.gateway.calls, [])

    async def test_http_errors_not_echoed_or_retried(self):
        for status, code in [(401, 'LLM_AUTH_ERROR'), (402, 'LLM_CREDIT_ERROR'),
                             (400, 'LLM_REQUEST_REJECTED'), (404, 'LLM_REQUEST_REJECTED'),
                             (429, 'LLM_RATE_LIMIT'), (503, 'LLM_UNAVAILABLE')]:
            mock = MockRouterAI(status=status)
            result = await self.ask(mock)
            self.assertEqual(result.error['code'], code)
            self.assertEqual(len(mock.requests), 1)
        self.assertEqual(self.gateway.calls, [])
        self.assertNotIn(KEY, self.journals())

    async def test_api_timeout_is_not_hydraulic_timeout(self):
        def timeout(request):
            raise httpx.ReadTimeout('Synthetic timeout')
        planner = RouterAIPlanner(KEY, MODEL, transport=httpx.MockTransport(timeout))
        result = await run_agent('Сравни base и plus10', self.ctx, planner, self.gateway.connect)
        self.assertEqual(result.error['code'], 'LLM_TIMEOUT')
        self.assertEqual(result.execution_status, 'NOT_RUN')
        self.assertEqual(self.gateway.calls, [])

    async def test_key_in_request_is_not_sent_or_logged(self):
        mock = MockRouterAI()
        result = await self.ask(mock, 'Мой ключ ' + KEY)
        self.assertEqual(result.error['code'], 'SENSITIVE_INPUT')
        self.assertEqual(mock.requests, [])
        self.assertNotIn(KEY, self.journals())

    async def test_partial_data_are_preserved_not_marked_success(self):
        del self.f.data['studies']['plus10']
        self.f.data['status'] = 'CHECK_REQUIRED'
        self.f.data['stage'] = 'plus10:run'
        self.f.report.write_text(json.dumps(self.f.data), encoding='utf-8')
        self.ctx.run_id = self.f.register()
        result = await self.ask()
        self.assertEqual(result.status, 'CHECK_REQUIRED')
        self.assertEqual(result.execution_status, 'PARTIAL')
        self.assertEqual(set(result.evidence[-1]['payload']['results']), {'base'})
        self.assertNotIn('Разница P', result.answer)

    async def test_corrupt_checkpoint_blocks_mcp(self):
        await self.ask()
        state = self.f.registry / 'agent_requests' / (self.ctx.request_id + '.json')
        state.write_text('broken', encoding='utf-8')
        result = await self.ask()
        self.assertEqual(result.error['code'], 'CHECKPOINT_INVALID')
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_previous_provider_checkpoint_cannot_be_reused(self):
        with patch('src.agent.loop.PROMPT_VERSION', 'pipesim-router-1'):
            await self.ask()
        mock = MockRouterAI()
        result = await self.ask(mock)
        self.assertEqual(result.error['code'], 'REQUEST_CONFLICT')
        self.assertEqual(mock.requests, [])
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_missing_llm_configuration_has_trace_and_no_call(self):
        import agent
        with patch('agent.load_dotenv'), patch.dict('os.environ', {}, clear=True):
            result = agent.run('Сравни base и plus10', self.ctx)
        self.assertEqual(result.error['code'], 'LLM_NOT_CONFIGURED')
        self.assertEqual(result.execution_status, 'NOT_RUN')
        self.assertTrue((self.f.registry / 'agent_traces' / (result.trace_id + '.json')).is_file())
        self.assertFalse(result.evidence)

    async def test_real_stdio_mcp_with_simulated_llm(self):
        result = await run_agent('Сравни base и plus10', self.ctx, MockRouterAI().planner())
        self.assertEqual(result.status, 'SUCCESS')
        self.assertEqual(result.evidence[0]['tool'], 'read_results')
        trace = result.evidence[0]['payload']['trace_id']
        self.assertTrue((self.f.registry / 'traces' / (trace + '.json')).is_file())
        self.assertTrue((self.f.registry / 'agent_traces' / (result.trace_id + '.json')).is_file())


class RouterAIConfigurationTests(unittest.TestCase):
    def test_configure_defaults_and_preserves_existing_file(self):
        import agent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('agent.ROOT', root), patch('agent.getpass.getpass', return_value=KEY), \
                    patch('builtins.input', return_value=''), patch('builtins.print') as output:
                agent.configure()
                expected = 'ROUTERAI_API_KEY=' + KEY + '\nROUTERAI_MODEL=' + DEFAULT_MODEL + '\n'
                self.assertEqual((root / '.env').read_text(), expected)
                self.assertNotIn(KEY, str(output.call_args_list))
                with self.assertRaises(AgentError) as error:
                    agent.configure()
                self.assertEqual(error.exception.code, 'CONFIG_EXISTS')
                self.assertEqual((root / '.env').read_text(), expected)

    def test_run_uses_routerai_key_and_default_model(self):
        import agent
        with tempfile.TemporaryDirectory() as directory:
            context = AgentContext(registry=Path(directory), run_id=str(uuid4()))
            env = {'ROUTERAI_API_KEY': KEY, 'OPENROUTER_API_KEY': 'unused-old-key',
                   'OPENROUTER_MODEL': 'unused/old-model'}
            with patch('agent.load_dotenv'), patch.dict('os.environ', env, clear=True), \
                    patch('agent.anyio.run', return_value='synthetic-result') as execute:
                self.assertEqual(agent.run('Сравни base и plus10', context), 'synthetic-result')
                planner = execute.call_args.args[-1]
                self.assertEqual(planner.api_key, KEY)
                self.assertEqual(planner.model, DEFAULT_MODEL)

    def test_old_key_is_not_used_as_routerai_key(self):
        import agent
        with tempfile.TemporaryDirectory() as directory:
            context = AgentContext(registry=Path(directory), run_id=str(uuid4()))
            with patch('agent.load_dotenv'), \
                    patch.dict('os.environ', {'OPENROUTER_API_KEY': KEY}, clear=True):
                result = agent.run('Сравни base и plus10', context)
            self.assertEqual(result.error['code'], 'LLM_NOT_CONFIGURED')
            self.assertEqual(result.execution_status, 'NOT_RUN')


if __name__ == '__main__':
    unittest.main()
