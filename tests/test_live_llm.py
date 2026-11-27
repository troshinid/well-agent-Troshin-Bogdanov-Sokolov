"""End-to-end with a local HTTP stand-in, real stdio MCP and a synthetic registry."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scripts.check_live_llm import EXPECTED_TASK, REQUESTS, check_pair, load_json
from src.agent.routerai import DEFAULT_MODEL
from test_mcp_read_results import Fixture


class LiveCheckTests(unittest.TestCase):
    def test_same_answers_and_real_connection_refusal(self):
        calls = []

        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((body['messages'][-1]['content'], self.headers.get('Authorization')))
                data = json.dumps({'model': DEFAULT_MODEL, 'choices': [{'finish_reason': 'stop',
                                  'message': {'content': json.dumps(EXPECTED_TASK)}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        with tempfile.TemporaryDirectory() as temp, ThreadingHTTPServer(('127.0.0.1', 0), Endpoint) as http:
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            try:
                fixture = Fixture(temp)
                output = Path(temp) / 'output'
                with patch.dict(os.environ, {'ROUTERAI_API_KEY': 'stand-in-provider-key'}):
                    summary = check_pair(fixture.registry, fixture.register(), output,
                                         DEFAULT_MODEL, f'http://127.0.0.1:{http.server_port}/v1')
            finally:
                http.shutdown()
                thread.join()
            self.assertEqual(summary['passed'], 6, summary)
            self.assertEqual(len({row['request_id'] for row in summary['cases']}), 6)
            self.assertEqual([request for request, _ in calls], list(REQUESTS))
            self.assertTrue(all(auth != 'Bearer stand-in-provider-key' for _, auth in calls))
            self.assertEqual(len(list(output.glob('*_mcp.json'))), 3)
            self.assertTrue(all(load_json(output / f'offline_{i}.json')['error']['code'] == 'LLM_UNAVAILABLE'
                                for i in range(1, 4)))
