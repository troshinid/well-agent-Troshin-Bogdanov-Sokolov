"""Register trusted local files, serve MCP, or exercise a real stdio client."""

import argparse
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import anyio
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .results import Registry, ReportError
from .runner import load_profile, profile_config
from .runtime_environment import environment_presence, server_environment
from .schemas import INPUT_SCHEMA, OUTPUT_SCHEMA, RUN_INPUT_SCHEMA, RUN_OUTPUT_SCHEMA


async def check_server(registry, run_id):
    """Exercise actual SDK client/server processes, not a mocked function call."""
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'src.well_agent_mcp', 'serve', '--registry', str(registry)],
        cwd=str(Path(__file__).resolve().parents[2]), encoding='utf-8', env=server_environment())
    validator = Draft202012Validator(OUTPUT_SCHEMA)
    report = {'status': 'CHECK_REQUIRED', 'transport': 'stdio', 'run_id': run_id, 'checks': {}, 'calls': {}}
    with anyio.fail_after(60):
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as session:
                initialized = await session.initialize()
                report['protocol_version'] = initialized.protocolVersion
                listed = await session.list_tools()
                report['checks']['tools_available'] = {tool.name for tool in listed.tools} == {'read_results', 'run_steady_state'}
                tool = next(tool for tool in listed.tools if tool.name == 'read_results')
                report['checks']['input_schema'] = tool.inputSchema == INPUT_SCHEMA
                report['checks']['output_schema'] = tool.outputSchema == OUTPUT_SCHEMA
                cases = [(name, {'run_id': run_id, 'scenario': name}, None) for name in ['base', 'plus10', 'compare']]
                cases.extend([
                    ('unknown_run', {'run_id': str(uuid4()), 'scenario': 'compare'}, 'RUN_NOT_FOUND'),
                    ('bad_scenario', {'run_id': run_id, 'scenario': 'plus20'}, 'INVALID_ARGUMENT'),
                    ('extra_argument', {'run_id': run_id, 'scenario': 'compare', 'pressure': 12}, 'INVALID_ARGUMENT'),
                ])
                for label, args, expected_error in cases:
                    result = await session.call_tool('read_results', arguments=args)
                    payload = result.structuredContent
                    validator.validate(payload)
                    text = next(c.text for c in result.content if c.type == 'text')
                    report['checks'][label + '_text_matches_json'] = json.loads(text) == payload
                    report['calls'][label] = payload
                    if expected_error:
                        report['checks'][label] = result.isError is True and payload['error']['code'] == expected_error
                    else:
                        report['checks'][label] = (not result.isError and payload['tool_status'] == 'SUCCESS'
                            and payload['execution_status'] == 'SUCCESS' and payload['numerical_validation'] == 'OK'
                            and payload['physical_validation'] == 'NOT_ASSESSED')
                        wanted = {'base', 'plus10'} if label == 'compare' else {label}
                        report['checks'][label + '_selection'] = set(payload['results']) == wanted
                        if label != 'compare':
                            report['checks'][label + '_no_comparison'] = payload['comparison'] is None
                pair = report['calls']['compare']
                if pair['comparison'] is not None:
                    actual = pair['comparison']['inlet_pressure_plus10_minus_base']['value']
                    expected = pair['results']['plus10']['inlet_pressure']['value'] - pair['results']['base']['inlet_pressure']['value']
                    report['checks']['comparison_arithmetic'] = abs(actual - expected) < 1e-12
                else:
                    report['checks']['comparison_arithmetic'] = False
                trace_ids = [result['trace_id'] for result in report['calls'].values()]
                report['checks']['unique_trace_ids'] = len(set(trace_ids)) == len(trace_ids)
                report['checks']['trace_files_written'] = all(
                    (Path(registry) / 'traces' / (trace_id + '.json')).is_file() for trace_id in trace_ids)
    report['status'] = 'MCP_CHECK_PASSED' if all(report['checks'].values()) else 'CHECK_REQUIRED'
    return report


async def check_run_server(registry, seed_run_id, request_id):
    profile = load_profile(Registry(registry), seed_run_id)
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'src.well_agent_mcp', 'serve', '--registry', str(registry)],
        cwd=str(Path(__file__).resolve().parents[2]), encoding='utf-8', env=server_environment())
    report = {'status': 'CHECK_REQUIRED', 'transport': 'stdio', 'seed_run_id': seed_run_id,
              'request_id': request_id, 'checks': {}, 'calls': {},
              'forwarded_environment_presence': environment_presence(params.env)}
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer,
                read_timeout_seconds=timedelta(seconds=profile['wait_seconds'] + 90)) as session:
            initialized = await session.initialize()
            report['protocol_version'] = initialized.protocolVersion
            tools = await session.list_tools()
            tool = next(t for t in tools.tools if t.name == 'run_steady_state')
            report['checks']['input_schema'] = tool.inputSchema == RUN_INPUT_SCHEMA
            report['checks']['output_schema'] = tool.outputSchema == RUN_OUTPUT_SCHEMA
            validator = Draft202012Validator(RUN_OUTPUT_SCHEMA)
            args = {'seed_run_id': seed_run_id, 'request_id': request_id, 'scenario_set': 'base_plus10'}
            for label, parameters in [('bad_scenario', dict(args, scenario_set='arbitrary')),
                                       ('extra_argument', dict(args, command='python'))]:
                called = await session.call_tool('run_steady_state', arguments=parameters)
                payload = called.structuredContent;validator.validate(payload)
                report['calls'][label] = payload
                report['checks'][label] = called.isError and payload['error']['code'] == 'INVALID_ARGUMENT'
            called = await session.call_tool('run_steady_state', arguments=args)
            payload = called.structuredContent;validator.validate(payload)
            report['calls']['run'] = payload
            report['checks']['run_success'] = (not called.isError and payload['tool_status'] == 'SUCCESS'
                and payload['execution_status'] == 'SUCCESS' and payload['numerical_validation'] == 'OK'
                and payload['physical_validation'] == 'NOT_ASSESSED' and payload['job_state'] == 'FINISHED'
                and payload['worker_exit_code'] == 0)
            if not report['checks']['run_success']:
                # request_id remains in the caller's checkpoint for reconciliation.
                return report
            again = await session.call_tool('run_steady_state', arguments=args)
            reused = again.structuredContent;validator.validate(reused)
            report['calls']['repeat_request'] = reused
            report['checks']['idempotent_run_id'] = (not again.isError and reused['reused'] is True
                and reused['run_id'] == payload['run_id'] and reused['results'] == payload['results'])
            read = await session.call_tool('read_results', arguments={'run_id': payload['run_id'], 'scenario': 'compare'})
            reading = read.structuredContent;Draft202012Validator(OUTPUT_SCHEMA).validate(reading)
            report['calls']['read_results'] = reading
            report['checks']['read_new_results'] = not read.isError and reading['results'] == payload['results']
            report['checks']['warnings_preserved'] = reading['warnings'] == payload['warnings']
            seed = await session.call_tool('read_results', arguments={'run_id': seed_run_id, 'scenario': 'compare'})
            reference = seed.structuredContent
            report['checks']['same_prepared_model_reproduction'] = (not seed.isError and all(
                abs(payload['results'][name]['inlet_pressure']['value'] - reference['results'][name]['inlet_pressure']['value'])
                * 100000 <= payload['numerical_limits']['run_restart_pressure_pa'] for name in ['base', 'plus10']))
            ids = [r['trace_id'] for r in report['calls'].values()]
            report['checks']['trace_files_written'] = all((Path(registry) / 'traces' / (x + '.json')).is_file() for x in ids)
            report['checks']['unique_trace_ids'] = len(ids) == len(set(ids))
    report['status'] = 'MCP_RUN_CHECK_PASSED' if all(report['checks'].values()) else 'CHECK_REQUIRED'
    return report


def main():
    parser = argparse.ArgumentParser(description='MCP read_results: регистрация отчёта и проверка stdio')
    sub = parser.add_subparsers(dest='command', required=True)
    register = sub.add_parser('register', help='Зарегистрировать локальные файлы без запуска PIPESIM')
    for arg in ['registry', 'report', 'model', 'rates']:
        register.add_argument('--' + arg, required=True, type=Path)
    serve = sub.add_parser('serve', help='MCP stdio; stdout предназначен только для протокола')
    serve.add_argument('--registry', required=True, type=Path)
    check = sub.add_parser('check', help='Запустить сервер и проверить его через настоящий MCP-клиент')
    check.add_argument('--registry', required=True, type=Path)
    check.add_argument('--run-id', required=True)
    check.add_argument('--output', required=True, type=Path, help='Новый JSON-файл проверки')
    configure = sub.add_parser('configure-runner', help='Разрешить повтор согласованных сценариев в подготовленной модели')
    configure.add_argument('--registry', required=True, type=Path)
    configure.add_argument('--seed-run-id', required=True)
    configure.add_argument('--prepared-model', required=True, type=Path)
    configure.add_argument('--toolkit-root', required=True, type=Path)
    configure.add_argument('--output-root', required=True, type=Path)
    configure.add_argument('--wait-seconds', type=int, default=600)
    run_check = sub.add_parser('run-check', help='Запустить новый расчёт через MCP и проверить повтор запроса')
    run_check.add_argument('--registry', required=True, type=Path)
    run_check.add_argument('--seed-run-id', required=True)
    run_check.add_argument('--request-id', help='Повторить запрос с тем же UUID; по умолчанию новый UUID')
    run_check.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        if args.command == 'register':
            run_id = Registry(args.registry).register(args.report, args.model, args.rates)
            print('Registration: REGISTERED')
            print('run_id: ' + run_id)
            print('Реестр: ' + str(args.registry.resolve()))
        elif args.command == 'configure-runner':
            profile_config(args.registry, args.seed_run_id, args.prepared_model, args.toolkit_root,
                           args.output_root, args.wait_seconds)
            print('Runner: CONFIGURED')
            print('seed_run_id: ' + args.seed_run_id)
        elif args.command == 'serve':
            from .server import serve as run_server
            anyio.run(run_server, args.registry.resolve())
        elif args.command == 'check':
            if args.output.exists():
                parser.error('Выходной файл существует. Укажите новое имя для --output')
            report = anyio.run(check_server, args.registry.resolve(), args.run_id)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
            print('MCP: ' + report['status'])
            pair = report['calls']['compare']
            for name, result in pair['results'].items():
                print('{}: P входа = {} bara'.format(name, result['inlet_pressure']['value']))
            print('Отчёт: ' + str(args.output.resolve()))
            return 0 if report['status'] == 'MCP_CHECK_PASSED' else 1
        else:
            if args.output.exists():
                parser.error('Выходной файл существует. Укажите новое имя для --output')
            request_id = args.request_id or str(uuid4())
            checkpoint = {'status': 'IN_PROGRESS', 'seed_run_id': args.seed_run_id, 'request_id': request_id}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(checkpoint, indent=2) + '\n')
            print('request_id: ' + request_id, flush=True)
            print('Запуск через MCP; подробный вывод Toolkit сохраняется в worker.log.', flush=True)
            try:
                report = anyio.run(check_run_server, args.registry.resolve(), args.seed_run_id, request_id)
            except Exception as error:
                # Keep the ID even if the MCP connection is interrupted.
                report = dict(checkpoint, status='CHECK_REQUIRED', error='{}: {}'.format(type(error).__name__, error))
            from .results import write_json
            write_json(args.output, report)
            print('MCP run: ' + report['status'])
            if report.get('error'):
                print(report['error'])
            result = report.get('calls', {}).get('run', {})
            if result.get('run_id'):
                print('new run_id: ' + result['run_id'])
            if result.get('error'):
                print(result['error'])
            for name, value in result.get('results', {}).items():
                print('{}: P входа = {} bara'.format(name, value['inlet_pressure']['value']))
            print('Отчёт: ' + str(args.output.resolve()))
            return 0 if report['status'] == 'MCP_RUN_CHECK_PASSED' else 1
    except ReportError as error:
        print('{}: {}'.format(error.payload.code, error.payload.message), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, StopIteration, TimeoutError) as error:
        print('{}: {}'.format(type(error).__name__, error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
