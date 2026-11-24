"""Local operator client and opt-in MCP stdio server for model preparation."""
import json
import sys
from datetime import timedelta

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from .runner import REPOSITORY
from .runtime_environment import server_environment


async def prepare_model(data, toolkit, plan_only):
    registry = data / 'mcp_registry'
    params = StdioServerParameters(command=sys.executable, cwd=str(REPOSITORY), env=server_environment(),
        args=['-m', 'src.well_agent_mcp', 'serve', '--registry', str(registry),
              '--build-data', str(data), '--toolkit-root', str(toolkit)])
    shown_id = None

    async def confirm(ctx, request):
        if not shown_id or request.message != 'Записать новую копию по плану ' + shown_id + '?':
            return types.ElicitResult(action='cancel')
        try:
            response = input('Записать отдельную копию? Введите ДА для подтверждения, Enter для отказа: ')
        except EOFError:
            return types.ElicitResult(action='cancel')
        if response.strip().upper() == 'ДА':
            return types.ElicitResult(action='accept', content={'approved': True})
        return types.ElicitResult(action='decline')

    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, elicitation_callback=confirm,
                                 read_timeout_seconds=timedelta(minutes=15)) as session:
            await session.initialize()
            result = (await session.call_tool('prepare_model', arguments={})).structuredContent
            if not result or result.get('status') != 'PLAN_READY':
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 1
            shown_id = result['plan_id']
            print('PLAN_READY', shown_id)
            print(json.dumps(result['plan'], ensure_ascii=False, indent=2))
            print('Журнал плана:', registry / 'traces' / (result['trace_id'] + '.json'), flush=True)
            if plan_only:
                return 0
            call = await session.call_tool('apply_approved_changes', arguments={'plan_id': shown_id})
            result = call.structuredContent
            if not result:
                print(call.content)
                return 1
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print('Журнал решения:', registry / 'traces' / (result['trace_id'] + '.json'))
            return 0 if result['status'] in ('VERIFIED', 'ALREADY_BUILT', 'DECLINED') else 1
