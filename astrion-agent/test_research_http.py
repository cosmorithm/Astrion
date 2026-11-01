"""本机 HTTP 集成测试：模拟模型分两次发送，检查事件先于结束到达浏览器。"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = json.loads(self.rfile.read(length))
        assert body['stream'] is True
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        def send(item):
            self.wfile.write(('data: ' + json.dumps(item, ensure_ascii=True) + '\n\n').encode())
            self.wfile.flush()
        send({'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'a', 'type': 'function', 'function': {'name': 'finish', 'arguments': '{"answer":"first'}}]}}]})
        time.sleep(0.4)
        send({'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': ' second"}'}}]}, 'finish_reason': 'tool_calls'}]})
        send({'choices': [], 'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}})
        self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()

class HttpStreamTests(unittest.TestCase):
    def test_optional_context_service_runs_through_real_http(self):
        from context_service import ContextHandler
        from context_adapter import HttpContextAdapter
        from memory import Memory
        server = ThreadingHTTPServer(('127.0.0.1', 0), ContextHandler)
        server.context_key = 'test-context-only'
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        adapter = HttpContextAdapter(f'http://127.0.0.1:{server.server_port}/context-edits', key=server.context_key)
        memory = Memory('规则', budget_chars=500, keep_recent=1, context_adapter=adapter)
        memory.enabled_layers = {3}
        memory.add_user('保留样本量')
        memory.add_assistant({'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'r', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]})
        memory.add_tool('r', 'n=128 ' * 300)
        memory.add_assistant({'content': '最近一步'})
        self.assertTrue(memory.compact())
        self.assertEqual(memory.layer_reports[2]['adapter'], 'http-context-edits-v1')
        self.assertEqual(memory.check(), [])
        self.assertIn('n=128', memory.read_evidence(memory._history[2]['_ref']))

    def test_live_sse_propagates_incremental_model_output(self):
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True); thread.start()
        self.addCleanup(upstream.server_close); self.addCleanup(upstream.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            from tokenizers import Tokenizer, models
            tokenizer_path = Path(directory) / 'tokenizer.json'
            Tokenizer(models.WordLevel({'[UNK]': 0}, unk_token='[UNK]')).save(str(tokenizer_path))
            port = unused_port()
            env = {**os.environ, 'AGENT_API_KEY': 'fake-only', 'AGENT_BASE_URL': f'http://127.0.0.1:{upstream.server_port}',
                   'ASTRION_CONTEXT_TOKENIZER_PATH': str(tokenizer_path),
                   'ASTRION_RESEARCH_DB': str(Path(directory) / 'db.sqlite')}
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'research.api:app', '--host', '127.0.0.1', '--port', str(port)],
                                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: process.poll() is None and process.terminate())
            base = f'http://127.0.0.1:{port}'
            for _ in range(100):
                try:
                    urllib.request.urlopen(base + '/api/projects', timeout=1).close()
                    break
                except (OSError, urllib.error.URLError): time.sleep(.05)
            else: self.fail('本机 API 没有启动')
            def post(path, payload):
                return urllib.request.urlopen(urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                    headers={'Content-Type': 'application/json'}, method='POST'), timeout=5)
            with post('/api/projects', {'name': 'test'}) as response:
                project_id = json.load(response)['id']
            first_at = None; done_at = None; collected = []
            with post('/api/research/stream', {'project_id': project_id, 'task': 'test'}) as response:
                for line in response:
                    if not line.startswith(b'data:'): continue
                    event = json.loads(line[5:])
                    if event['type'] == 'model_delta' and event['kind'] == 'answer':
                        collected.append(event['text'])
                        if first_at is None: first_at = time.monotonic()
                    if event['type'] == 'done':
                        done_at = time.monotonic()
                        self.assertEqual(event['result']['result']['answer'], 'first second')
                        break
            self.assertEqual(''.join(collected), 'first second')
            self.assertIsNotNone(first_at)
            self.assertIsNotNone(done_at)
            self.assertGreater(done_at - first_at, 0.15)
            process.terminate(); process.wait(timeout=5)

if __name__ == '__main__': unittest.main()
