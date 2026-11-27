"""LLM task -> local policy -> MCP -> deterministic answer, with restart checkpoints."""

import hashlib
import json
import re
from uuid import uuid4
from pydantic import ValidationError
from src.well_agent_mcp.results import write_json, utc_now
from .mcp_client import connect
from .routerai import AgentError, BASE_URL, strict_json
from .prompts import PROMPT_VERSION
from .report import REASONS, build_answer
from .schemas import AgentContext, AgentResult, Task


def fingerprint(request, ctx, model, base_url=BASE_URL):
    data = {'request': request, 'run_id': ctx.run_id, 'registry': str(ctx.registry.resolve()),
            'allow_run': ctx.allow_run, 'model': model, 'prompt_version': PROMPT_VERSION}
    if base_url != BASE_URL:
        data["llm_base_url"] = base_url
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


async def run_agent(request, ctx, planner, connection=connect):
    ctx = AgentContext.model_validate(ctx)
    result = AgentResult(request_id=ctx.request_id, trace_id=str(uuid4()))
    journal = {'schema_version': 1, 'trace_id': result.trace_id, 'request_id': ctx.request_id,
               'started_at': utc_now(), 'prompt_version': PROMPT_VERSION, 'llm': None, 'events': []}
    path = ctx.registry / 'agent_traces' / (result.trace_id + '.json')
    state_path = ctx.registry / 'agent_requests' / (ctx.request_id + '.json')
    try:
        if not isinstance(request, str) or not request.strip() or len(request) > 4000:
            raise AgentError('INVALID_REQUEST', 'Нужен непустой текстовый запрос длиной до 4000 символов.')
        key = getattr(planner, 'api_key', '')
        if (key and key in request) or re.search(r'sk-or-v1-[A-Za-z0-9_-]{16,}', request):
            raise AgentError('SENSITIVE_INPUT', 'Запрос содержит API-ключ; он не отправлен и не записан.')
        journal['request'] = request
        expected = fingerprint(request, ctx, planner.model, getattr(planner, 'base_url', BASE_URL))
        if state_path.exists():
            try:
                state = strict_json(state_path.read_text(encoding='utf-8'))
                if state['fingerprint'] != expected:
                    raise AgentError('REQUEST_CONFLICT', 'Этот request_id связан с другим запросом или контекстом.')
                task = Task.model_validate(state['task'])
                metadata = state['llm']
            except (OSError, ValueError, KeyError, TypeError):
                raise AgentError('CHECKPOINT_INVALID', 'Повреждена запись запроса. Новый расчёт не запускался.') from None
            journal['plan_reused'] = True
        else:
            if hasattr(planner, 'request_body'):
                journal['llm_request'] = {
                    'base_url': planner.base_url, 'body': planner.request_body(request)}
            task, metadata = await planner.plan(request)
            task = Task.model_validate(task)
            state = {'schema_version': 1, 'fingerprint': expected, 'task': task.model_dump(), 'llm': metadata}
            state_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                # First writer wins; another invocation may not replace an already selected task.
                with state_path.open('x', encoding='utf-8') as stream:
                    stream.write(json.dumps(state, ensure_ascii=False, allow_nan=False))
            except FileExistsError:
                raise AgentError('REQUEST_BUSY', 'Этот request_id уже обрабатывается. Повтори его после завершения.') from None
            journal['plan_reused'] = False
        result.task = task
        journal['llm'] = metadata
        journal['task'] = task.model_dump()
        if task.action == 'refuse':
            result.status, result.answer = 'REFUSED', REASONS[task.reason]
            result.error = {'code': task.reason, 'message': result.answer}
        elif task.action == 'run_steady_state' and not ctx.allow_run:
            result.status = 'REFUSED'
            result.answer = 'Запуск расчёта не разрешён в текущем контексте. Доступно чтение сохранённых результатов.'
            result.error = {'code': 'RUN_NOT_ALLOWED', 'message': result.answer}
        else:
            # Persist the chosen operation before any possible launch.
            write_json(path, journal)
            async with connection(ctx.registry) as gateway:
                if task.action == 'run_steady_state':
                    args = {'seed_run_id': ctx.run_id, 'request_id': ctx.request_id, 'scenario_set': 'base_plus10'}
                    payload = await gateway.call('run_steady_state', args)
                    result.evidence.append({'tool': 'run_steady_state', 'arguments': args, 'payload': payload})
                    journal['events'] = result.evidence
                    write_json(path, journal)
                    run_id = payload['run_id']
                else:
                    run_id = ctx.run_id
                if run_id:
                    args = {'run_id': run_id, 'scenario': task.scenario}
                    payload = await gateway.call('read_results', args)
                    result.evidence.append({'tool': 'read_results', 'arguments': args, 'payload': payload})
            last = result.evidence[-1]['payload']
            result.execution_status = last['execution_status']
            result.numerical_validation = last['numerical_validation']
            passed = all(e['payload']['tool_status'] == 'SUCCESS' and
                         e['payload']['execution_status'] == 'SUCCESS' and
                         e['payload']['numerical_validation'] == 'OK' for e in result.evidence)
            result.status = 'SUCCESS' if passed else 'CHECK_REQUIRED'
            result.answer = build_answer(result.evidence, task.action)
            failure = next((e['payload']['error'] for e in result.evidence if e['payload']['error']), None)
            if failure:
                result.error = {'code': failure['code'], 'message': failure['message']}
    except AgentError as error:
        result.status = 'ERROR'
        result.error = {'code': error.code, 'message': error.message}
        result.answer = error.message
    except (OSError, ValidationError):
        result.status = 'ERROR'
        result.error = {'code': 'LOCAL_VALIDATION_ERROR', 'message': 'Ошибка локальной проверки или записи. Подробности не отправлены в LLM.'}
        result.answer = result.error['message']
    except Exception:
        result.status = 'ERROR'
        # SDK task groups can wrap protocol/connection errors in an ExceptionGroup.
        result.error = {'code': 'MCP_UNAVAILABLE', 'message': 'Связь с MCP прервана. Если был запуск, его исход неизвестен; повтори тот же request_id.'}
        result.execution_status = 'UNKNOWN'
        result.answer = result.error['message']
    journal['events'] = result.evidence
    journal['finished_at'] = utc_now()
    journal['result'] = result.model_dump(mode='json')
    try:
        write_json(path, journal)
    except OSError:
        result.status = 'ERROR'
        result.error = {'code': 'TRACE_WRITE_FAILED', 'message': 'Не удалось сохранить журнал агента. Повторяй только тот же request_id.'}
        result.answer += '\n' + result.error['message']
    return result

