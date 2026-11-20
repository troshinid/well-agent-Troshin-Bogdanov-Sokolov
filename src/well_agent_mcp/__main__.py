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
from .schemas import INPUT_SCHEMA, OUTPUT_SCHEMA


async def check_server(registry, run_id):
    """Exercise actual SDK client/server processes, not a mocked function call."""
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'src.well_agent_mcp', 'serve', '--registry', str(registry)],
        cwd=str(Path(__file__).resolve().parents[2]), encoding='utf-8')
    validator = Draft202012Validator(OUTPUT_SCHEMA)
    report = {'status': 'CHECK_REQUIRED', 'transport': 'stdio', 'run_id': run_id, 'checks': {}, 'calls': {}}
    with anyio.fail_after(60):
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as session:
                initialized = await session.initialize()
                report['protocol_version'] = initialized.protocolVersion
                listed = await session.list_tools()
                report['checks']['one_read_results_tool'] = [tool.name for tool in listed.tools] == ['read_results']
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
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        if args.command == 'register':
            run_id = Registry(args.registry).register(args.report, args.model, args.rates)
            print('Registration: REGISTERED')
            print('run_id: ' + run_id)
            print('Реестр: ' + str(args.registry.resolve()))
        elif args.command == 'serve':
            from .server import serve as run_server
            anyio.run(run_server, args.registry.resolve())
        else:
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
    except ReportError as error:
        print('{}: {}'.format(error.payload.code, error.payload.message), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, StopIteration, TimeoutError) as error:
        print('{}: {}'.format(type(error).__name__, error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
