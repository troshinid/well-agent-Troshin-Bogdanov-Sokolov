"""Client for the existing stdio server; LLM keys are not inherited."""

import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from src.well_agent_mcp.runtime_environment import server_environment
from src.well_agent_mcp.schemas import ReadRequest, ReadResponse, RunRequest, RunResponse
from .routerai import AgentError


class Gateway:
    def __init__(self, session):
        self.session = session

    async def call(self, name, arguments):
        request, response = {'read_results': (ReadRequest, ReadResponse),
                             'run_steady_state': (RunRequest, RunResponse)}[name]
        request.model_validate(arguments)
        called = await self.session.call_tool(name, arguments=arguments)
        payload = response.model_validate(called.structuredContent).model_dump(mode='json')
        if bool(called.isError) != (payload['tool_status'] == 'ERROR'):
            raise AgentError('MCP_INVALID_RESPONSE', 'Противоречивые статусы ответа MCP.')
        if name == 'read_results' and (payload['run_id'] not in [None, arguments['run_id']]
                                      or payload['scenario'] not in [None, arguments['scenario']]):
            raise AgentError('MCP_INVALID_RESPONSE', 'MCP вернул результат другого запроса.')
        if name == 'run_steady_state' and (payload['request_id'] not in [None, arguments['request_id']]
                                          or payload['seed_run_id'] not in [None, arguments['seed_run_id']]):
            raise AgentError('MCP_INVALID_RESPONSE', 'MCP вернул результат другого запуска.')
        return payload


@asynccontextmanager
async def connect(registry):
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'src.well_agent_mcp', 'serve', '--registry', str(registry)],
        cwd=str(Path(__file__).resolve().parents[2]), encoding='utf-8', env=server_environment())
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=7350)) as session:
            await session.initialize()
            tools = await session.list_tools()
            if not {'read_results', 'run_steady_state'} <= {tool.name for tool in tools.tools}:
                raise AgentError('MCP_UNAVAILABLE', 'Сервер не предоставил нужные инструменты.')
            yield Gateway(session)
