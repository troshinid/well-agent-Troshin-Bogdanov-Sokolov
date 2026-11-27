"""One bounded RouterAI request; no automatic retries or raw error logging."""

import json
import re
import httpx
from .prompts import SYSTEM_PROMPT
from .schemas import Task

BASE_URL = 'https://routerai.ru/api/v1'
DEFAULT_MODEL = 'deepseek/deepseek-v4.1-flash-1017'


class AgentError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('Non-finite JSON value')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


class RouterAIPlanner:
    def __init__(self, api_key, model=DEFAULT_MODEL, transport=None, *, base_url=BASE_URL):
        self.base_url = base_url.rstrip("/")
        try:
            url = httpx.URL(self.base_url)
        except httpx.InvalidURL:
            raise AgentError("LLM_NOT_CONFIGURED", "Некорректный адрес LLM.") from None
        self.loopback = url.host in {"127.0.0.1", "::1", "localhost"}
        if (not url.host or url.userinfo or url.query or url.fragment
                or not (url.scheme == "https" or (self.loopback and url.scheme == "http"))):
            raise AgentError("LLM_NOT_CONFIGURED", "Проверь адрес LLM и протокол.")
        if api_key is None and self.loopback:
            api_key = 'local-probe-no-credential'
        if not isinstance(api_key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+', api_key):
            raise AgentError('LLM_NOT_CONFIGURED', 'Не задан ROUTERAI_API_KEY.')
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+/[A-Za-z0-9_.:/-]+', model):
            raise AgentError('LLM_NOT_CONFIGURED', 'Задай ROUTERAI_MODEL: точный ID модели в RouterAI.')
        self.api_key, self.model, self.transport = api_key, model, transport

    def request_body(self, request):
        return {'model': self.model, 'stream': False, 'max_tokens': 1024,
                'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': request}],
                'response_format': {'type': 'json_schema', 'json_schema': {
                    'name': 'pipesim_task', 'strict': True, 'schema': Task.model_json_schema()}}}

    async def plan(self, request):
        if self.api_key in request or re.search(r'sk-or-v1-[A-Za-z0-9_-]{16,}', request):
            raise AgentError('SENSITIVE_INPUT', 'Запрос содержит API-ключ; он не отправлен и не записан.')
        body = self.request_body(request)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15),
                    follow_redirects=False, trust_env=not self.loopback, transport=self.transport) as client:
                async with client.stream('POST', self.base_url + '/chat/completions',
                        headers={'Authorization': 'Bearer ' + self.api_key}, json=body) as response:
                    if response.status_code != 200:
                        code, message = {
                            401: ('LLM_AUTH_ERROR', 'RouterAI отклонил API-ключ.'),
                            402: ('LLM_CREDIT_ERROR', 'RouterAI сообщил об ограничении баланса или бюджета.'),
                            429: ('LLM_RATE_LIMIT', 'RouterAI ограничил частоту запросов. Автоповтора нет.'),
                            400: ('LLM_REQUEST_REJECTED', 'RouterAI отклонил запрос. Проверь ID модели и поддержку json_schema.'),
                            404: ('LLM_REQUEST_REJECTED', 'Модель или поддерживающий схему провайдер недоступны.'),
                        }.get(response.status_code, ('LLM_UNAVAILABLE', 'RouterAI недоступен или отклонил запрос.'))
                        raise AgentError(code, message)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 262144:
                            raise AgentError('LLM_INVALID_RESPONSE', 'Ответ LLM превышает допустимый размер.')
            data = strict_json(raw.decode('utf-8'))
            choice = data['choices'][0]
            if choice.get('finish_reason') != 'stop' or choice['message'].get('refusal'):
                raise AgentError('LLM_INVALID_RESPONSE', 'LLM не вернула завершённую задачу по схеме.')
            task = Task.model_validate(strict_json(choice['message']['content']))
            returned = data.get('model')
            if not isinstance(returned, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+/[A-Za-z0-9_.:/-]{1,160}', returned):
                returned = None
            usage = data.get('usage', {})
            usage = {name: usage[name] for name in ['prompt_tokens', 'completion_tokens', 'total_tokens']
                     if isinstance(usage, dict) and type(usage.get(name)) is int and usage[name] >= 0}
            return task, {'provider': 'routerai', 'model_requested': self.model, 'model_returned': returned, 'usage': usage}
        except httpx.TimeoutException:
            raise AgentError('LLM_TIMEOUT', 'Истекло время ожидания LLM. MCP не запускался.') from None
        except httpx.HTTPError:
            raise AgentError('LLM_UNAVAILABLE', 'Не удалось подключиться к RouterAI. MCP не запускался.') from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise AgentError('LLM_INVALID_RESPONSE', 'Ответ LLM не прошёл локальную проверку схемы.') from None
