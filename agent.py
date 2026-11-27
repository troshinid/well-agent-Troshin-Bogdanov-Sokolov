"""Synchronous benchmark entry point and a Windows-friendly CLI."""

import argparse
import getpass
import json
import logging
import os
import sys
from pathlib import Path
from uuid import uuid4
import anyio
from dotenv import load_dotenv
from pydantic import ValidationError
from src.agent.loop import run_agent
from src.agent.routerai import AgentError, RouterAIPlanner, BASE_URL, DEFAULT_MODEL
from src.agent.schemas import AgentContext, AgentResult

ROOT = Path(__file__).resolve().parent


def run(request, ctx) -> AgentResult:
    """Use from synchronous code; async callers can await run_agent directly."""
    context = AgentContext.model_validate(ctx)
    load_dotenv(ROOT / '.env', override=False)
    try:
        base_url = context.llm_base_url or os.environ.get('LLM_BASE_URL') or BASE_URL
        model = context.llm_model or os.environ.get('ROUTERAI_MODEL', DEFAULT_MODEL)
        # A RouterAI credential belongs only to its original endpoint.
        key = context.llm_api_key.get_secret_value() if context.llm_api_key else None
        if key is None and base_url.rstrip('/') == BASE_URL:
            key = os.environ.get('ROUTERAI_API_KEY')
        planner = RouterAIPlanner(key, model, base_url=base_url)
    except AgentError as error:
        result = AgentResult(request_id=context.request_id, trace_id=str(uuid4()),
            answer=error.message, error={'code': error.code, 'message': error.message})
        from src.well_agent_mcp.results import write_json, utc_now
        try:
            write_json(context.registry / 'agent_traces' / (result.trace_id + '.json'),
                {'schema_version': 1, 'trace_id': result.trace_id, 'request_id': context.request_id,
                 'finished_at': utc_now(), 'events': [], 'result': result.model_dump(mode='json')})
        except OSError:
            result.error = {'code': 'TRACE_WRITE_FAILED', 'message': 'Не удалось сохранить журнал настройки агента.'}
            result.answer += '\n' + result.error['message']
        return result
    return anyio.run(run_agent, request, context, planner)


def configure():
    path = ROOT / '.env'
    if path.exists():
        raise AgentError('CONFIG_EXISTS', '.env уже существует. Отредактируй его локально; ключ не выводится.')
    key = getpass.getpass('RouterAI API key (ввод скрыт): ').strip()
    model = input('RouterAI model ID [' + DEFAULT_MODEL + ']: ').strip() or DEFAULT_MODEL
    RouterAIPlanner(key, model)  # Validate locally, without sending a request.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write('ROUTERAI_API_KEY=' + key + '\nROUTERAI_MODEL=' + model + '\n')
    print('LLM: CONFIGURED (локальная запись; API ещё не проверен)')


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    parser = argparse.ArgumentParser(description='well-agent: текстовый запрос -> RouterAI -> MCP -> PIPESIM')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('configure-llm', help='Скрытый ввод ключа и запись нового локального .env')
    build = sub.add_parser('prepare-model', help='Показать план сборки сети и запросить подтверждение через MCP')
    build.add_argument('--data', type=Path, default=Path.home() / 'Desktop' / 'well-agent-data')
    build.add_argument('--toolkit-root', type=Path, default=Path(r'C:\Program Files\Schlumberger\PIPESIM_PTK_2021.1'))
    build.add_argument('--plan-only', action='store_true')
    ask = sub.add_parser('ask', help='Чтение по умолчанию; --allow-run разрешает повтор согласованной пары')
    ask.add_argument('--registry', required=True, type=Path)
    ask.add_argument('--run-id', required=True)
    ask.add_argument('--request', required=True)
    ask.add_argument('--request-id', help='Тот же UUID для повтора/уточнения исхода запуска')
    ask.add_argument('--allow-run', action='store_true')
    ask.add_argument('--output', required=True, type=Path, help='Новый JSON; существующий файл не перезаписывается')
    args = parser.parse_args()
    try:
        if args.command == 'prepare-model':
            from src.well_agent_mcp.prepare_cli import prepare_model
            return anyio.run(prepare_model, args.data.resolve(), args.toolkit_root.resolve(), args.plan_only)
        if args.command == 'configure-llm':
            configure()
            return 0
        if args.output.exists():
            parser.error('Отчёт существует. Выбери новое имя --output; для повтора сохрани прежний --request-id.')
        context = AgentContext(registry=args.registry.resolve(), run_id=args.run_id,
                               request_id=args.request_id or str(uuid4()), allow_run=args.allow_run)
        # Reserve the output and persist request_id before network or process calls.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps({'status': 'PENDING', 'request_id': context.request_id}, indent=2) + '\n')
        print('request_id: ' + context.request_id, flush=True)
        print('Разбор запроса через RouterAI…', flush=True)
        result = run(args.request, context)
        from src.well_agent_mcp.results import write_json
        write_json(args.output, result.model_dump(mode='json'))
        print('Agent: ' + result.status)
        print(result.answer)
        print('Отчёт: ' + str(args.output.resolve()))
        trace = context.registry / 'agent_traces' / (result.trace_id + '.json')
        if trace.is_file():
            print('Журнал: ' + str(trace))
        return 0 if result.status in ['SUCCESS', 'REFUSED'] else 1
    except (AgentError, OSError, ValidationError) as error:
        print(error.code + ': ' + error.message if isinstance(error, AgentError)
              else 'CONFIG_ERROR: проверь UUID, пути и доступ к записи. Секреты не выводятся.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

