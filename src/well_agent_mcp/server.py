"""stdio MCP server, pinned to the tested SDK 1.29.0 API."""

import json

import anyio
from jsonschema import Draft202012Validator
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from .results import ReadResults
from .schemas import INPUT_SCHEMA, OUTPUT_SCHEMA


def create_server(registry):
    server = Server('well-agent-results', version='0.1.0')
    service = ReadResults(registry)
    output_validator = Draft202012Validator(OUTPUT_SCHEMA)

    @server.list_tools()
    async def list_tools():
        return [Tool(name='read_results',
                     description='Прочитать зарегистрированный отчёт АГЗУ-6: base, plus10 или сравнение. '
                                 'Не запускает расчёт и не изменяет модель. Предупреждения Run и Restart сохраняются.',
                     inputSchema=INPUT_SCHEMA, outputSchema=OUTPUT_SCHEMA,
                     annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                 idempotentHint=True, openWorldHint=False))]

    # Application validation supplies structured INVALID_ARGUMENT errors and traces.
    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        if name != 'read_results':
            raise ValueError('Unknown tool: ' + name)
        payload = await anyio.to_thread.run_sync(service.call, arguments)
        output_validator.validate(payload)
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(payload, ensure_ascii=False, allow_nan=False))],
                              structuredContent=payload, isError=payload['tool_status'] == 'ERROR')

    return server


async def serve(registry):
    server = create_server(registry)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
