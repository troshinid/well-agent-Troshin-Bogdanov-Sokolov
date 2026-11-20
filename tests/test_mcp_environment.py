"""Actual stdio environment propagation, using synthetic non-secret values."""

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.well_agent_mcp.runtime_environment import environment_presence, server_environment


class EnvironmentTests(unittest.TestCase):
    def test_windows_names_and_license_values_are_preserved_without_unrelated_secrets(self):
        source = {'ProgramData': 'synthetic-programdata', 'SLBSLS_LICENSE_FILE': 'synthetic-license-setting',
                  'LM_LICENSE_FILE': 'synthetic-lm-setting', 'OPENAI_API_KEY': 'DO_NOT_FORWARD',
                  'PYTHONHOME': 'DO_NOT_FORWARD', 'UNRELATED_TOKEN': 'DO_NOT_FORWARD'}
        result = server_environment(source)
        self.assertEqual(result, {'PROGRAMDATA': 'synthetic-programdata',
            'SLBSLS_LICENSE_FILE': 'synthetic-license-setting', 'LM_LICENSE_FILE': 'synthetic-lm-setting'})
        presence = environment_presence(result)
        self.assertTrue(presence['PROGRAMDATA']);self.assertTrue(presence['SLBSLS_LICENSE_FILE'])
        self.assertFalse(presence['ALLUSERSPROFILE'])
        self.assertTrue(all(type(value) is bool for value in presence.values()))
        self.assertNotIn('synthetic-license-setting', json.dumps(presence))

    def test_no_invented_defaults_or_shell_function_values(self):
        self.assertEqual(server_environment({}), {})
        self.assertEqual(server_environment({'SLBSLS_LICENSE_FILE': '() unwanted-function'}), {})

    def test_actual_stdio_child_receives_selected_environment(self):
        # Test-only MCP server reports presence/equality flags, not environment values.
        source = textwrap.dedent('''\
            import os
            import anyio
            from mcp.server.lowlevel import Server
            from mcp.server.stdio import stdio_server
            from mcp.types import Tool
            server = Server('environment-test')
            @server.list_tools()
            async def tools():
                return [Tool(name='probe', inputSchema={'type':'object','properties':{},'additionalProperties':False})]
            @server.call_tool()
            async def call(name, args):
                return {'programdata_seen': os.environ.get('PROGRAMDATA') == 'synthetic-programdata',
                        'license_seen': os.environ.get('SLBSLS_LICENSE_FILE') == 'synthetic-license-setting',
                        'unrelated_secret_absent': 'UNRELATED_TOKEN' not in os.environ}
            async def main():
                async with stdio_server() as (reader, writer):
                    await server.run(reader, writer, server.create_initialization_options())
            anyio.run(main)
        ''')
        async def exercise(script):
            with patch.dict('os.environ', {'PROGRAMDATA': 'synthetic-programdata',
                    'SLBSLS_LICENSE_FILE': 'synthetic-license-setting', 'UNRELATED_TOKEN': 'DO_NOT_FORWARD'}):
                # Reproduce the old omission first, then exercise the fixed boundary.
                for forward in [False, True]:
                    env = server_environment() if forward else None
                    params = StdioServerParameters(command=sys.executable, args=[str(script)], env=env)
                    with anyio.fail_after(20):
                        async with stdio_client(params) as (reader, writer):
                            async with ClientSession(reader, writer) as session:
                                await session.initialize()
                                result = await session.call_tool('probe', arguments={})
                                self.assertFalse(result.isError)
                                self.assertEqual(result.structuredContent,
                                    {'programdata_seen': forward, 'license_seen': forward, 'unrelated_secret_absent': True})
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'environment_probe.py';script.write_text(source, encoding='utf-8')
            anyio.run(exercise, script)
