"""Compare the same saved PIPESIM pair through the normal agent entry point."""
import argparse
import json
import os
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from agent import run
from src.agent.routerai import BASE_URL, DEFAULT_MODEL
from src.agent.schemas import AgentContext
from src.well_agent_mcp.results import Registry, ReportError, utc_now, write_json


REQUESTS = (
    'Сравни сохранённые результаты base и plus10 для участка АГЗУ-6.',
    'Для отчёта по АГЗУ-6 покажи вместе два уже рассчитанных режима, сначала plus10, затем base.',
    'АГЗУ-6: сопоставь сохрaнённые резульаты базового режима (base) и варианта плюс десять (plus10).',
)
SCENARIOS = {'base', 'plus10'}
EXPECTED_TASK = {'action': 'read_results', 'scenario': 'compare', 'reason': 'SUPPORTED'}
DEFAULT_RUN = 'a4e97b37-1d84-580e-a09c-9a7e978cac49'


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def record(mode, index, request, context, directory, baseline):
    label = f'{mode}_{index}'
    print(f'{label} ...', flush=True)
    write_json(directory / (label + '.json'), {'status': 'PENDING', 'request_id': context.request_id})
    result = run(request, context).model_dump(mode='json')
    write_json(directory / (label + '.json'), result)
    trace_path = context.registry / 'agent_traces' / (result['trace_id'] + '.json')
    journal = {}
    if trace_path.is_file():
        shutil.copyfile(trace_path, directory / (label + '_trace.json'))
        journal = load_json(trace_path)
    call = journal.get('llm_request') or {}
    body = call.get('body', {})
    messages = body.get('messages', [])
    events = result['evidence']
    code = (result['error'] or {}).get('code')
    checks = {
        'fresh_call': bool(call) and not journal.get('plan_reused'),
        'llm_input': body.get('model') == context.llm_model and
                     call.get('base_url') == context.llm_base_url and
                     bool(messages) and messages[-1] == {'role': 'user', 'content': request},
        'linked_journal': (journal.get('request_id') == context.request_id == result['request_id']
                           and journal.get('result') == result and journal.get('events') == events),
    }
    snapshot = None
    if mode == 'live':
        checks['plan'] = result['task'] == EXPECTED_TASK and journal.get('task') == EXPECTED_TASK
        checks['model_answered'] = (journal.get('llm') or {}).get('model_requested') == context.llm_model
        checks['success'] = result['status'] == 'SUCCESS' and code is None and result['numerical_validation'] == 'OK'
        checks['one_read'] = [e['tool'] for e in events] == ['read_results']
        payload = events[0]['payload'] if checks['one_read'] else {}
        checks['same_run'] = (checks['one_read'] and events[0]['arguments'] ==
                              {'scenario': 'compare', 'run_id': context.run_id} and payload.get('run_id') == context.run_id)
        results = payload.get('results') or {}
        comparison = payload.get('comparison') or {}
        checks['both_scenarios'] = (
            set(results) == SCENARIOS and all((results[s].get('inlet_pressure') or {}).get('value') is not None
                                              for s in SCENARIOS)
            and (comparison.get('inlet_pressure_plus10_minus_base') or {}).get('value') is not None)
        if checks['both_scenarios']:
            snapshot = {'results': results, 'comparison': comparison, 'answer': result['answer']}
        checks['same_numbers_and_answer'] = snapshot is not None and (index == 1 or snapshot == baseline)
        tool_trace = context.registry / 'traces' / (str(payload.get('trace_id')) + '.json')
        checks['tool_journal'] = tool_trace.is_file()
        if tool_trace.is_file():
            shutil.copyfile(tool_trace, directory / (label + '_mcp.json'))
    else:
        checks['unavailable'] = result['status'] == 'ERROR' and code == 'LLM_UNAVAILABLE'
        checks['no_plan_or_tools'] = result['task'] is None and not events and result['execution_status'] == 'NOT_RUN'
    row = {'case': label, 'request': request, 'request_id': context.request_id, 'trace_id': result['trace_id'],
           'status': result['status'], 'error_code': code, 'tools': [e['tool'] for e in events],
           'checks': checks, 'passed': all(checks.values())}
    print(label, 'PASS' if row['passed'] else 'FAIL', result['status'], code or '', flush=True)
    return row, snapshot


def check_pair(registry, run_id, directory, model, live_url=BASE_URL):
    template = AgentContext(registry=registry, run_id=run_id, llm_model=model)
    Registry(registry).read(run_id)
    directory.mkdir(parents=True, exist_ok=False)
    summary = {'started_at': utc_now(), 'status': 'IN_PROGRESS', 'run_id': run_id, 'model': model,
               'planned': 2 * len(REQUESTS), 'cases': []}
    path = directory / 'summary.json'
    write_json(path, summary)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        # Keep this local port allocated without accepting any TCP connection.
        reserved.bind(('127.0.0.1', 0))
        offline_url = f'http://127.0.0.1:{reserved.getsockname()[1]}/v1'
        baseline = None
        for mode, url in [('live', live_url), ('offline', offline_url)]:
            for index, request in enumerate(REQUESTS, 1):
                context = template.model_copy(update={'request_id': str(uuid4()), 'llm_base_url': url})
                row, snapshot = record(mode, index, request, context, directory, baseline)
                if mode == 'live' and index == 1:
                    baseline = snapshot
                summary['cases'].append(row)
                write_json(path, summary)
                if mode == 'live' and row['status'] == 'ERROR':
                    break
    summary['passed'] = sum(row['passed'] for row in summary['cases'])
    summary['failed'] = len(summary['cases']) - summary['passed']
    summary['not_run'] = summary['planned'] - len(summary['cases'])
    summary['status'] = 'PASS' if summary['passed'] == summary['planned'] else 'CHECK_REQUIRED'
    summary['finished_at'] = utc_now()
    write_json(path, summary)
    return summary


def main():
    data = Path.home() / 'Desktop' / 'well-agent-data'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, default=data / 'mcp_registry')
    parser.add_argument('--run-id', default=DEFAULT_RUN)
    parser.add_argument('--output-dir', type=Path)
    settings = parser.parse_args()
    # When using the portable archive, start it from the configured project folder.
    load_dotenv(Path.cwd() / '.env', override=False)
    registry = settings.registry.resolve()
    directory = settings.output_dir or registry.parent / ('llm_check_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    try:
        summary = check_pair(registry, settings.run_id, directory, os.environ.get('ROUTERAI_MODEL') or DEFAULT_MODEL)
    except (OSError, ValueError, ReportError) as exc:
        print('Проверка не завершена:', str(exc), file=sys.stderr)
        return 2
    print('LIVE_LLM_CHECK:', summary['status'])
    print('PASS:', summary['passed'], 'FAIL:', summary['failed'], 'NOT_RUN:', summary['not_run'])
    print('Отчёт:', directory.resolve() / 'summary.json')
    return 0 if summary['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
