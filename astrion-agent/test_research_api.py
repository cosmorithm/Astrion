"""FastAPI 研究工作台接口的离线联调测试。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from research import api


class FakeStreamingModel:
    def __init__(self, key, base_url, model, emit):
        self.emit = emit
        self.stream_role = 'root'
        self.usage_totals = {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}
        self.usage_calls = 1
        self.usage_unknown = 0
    def chat(self, messages, tools=None):
        self.emit({'type': 'model_delta', 'role': self.stream_role, 'kind': 'answer', 'text': '研究'})
        self.emit({'type': 'model_delta', 'role': self.stream_role, 'kind': 'answer', 'text': '完成'})
        return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'f', 'type': 'function',
                'function': {'name': 'finish', 'arguments': json.dumps({'answer': '研究完成'}, ensure_ascii=False)}}]}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = patch.object(api, 'DB_PATH', Path(self.tmp.name) / 'db.sqlite3')
        self.db.start(); self.addCleanup(self.db.stop)
        self.client = TestClient(api.app)
        self.addCleanup(self.client.close)

    def test_project_documents_memory_and_streamed_run(self):
        project = self.client.post('/api/projects', json={'name': '科研'}).json()['id']
        doc = self.client.post(f'/api/projects/{project}/documents', json={'name': 'notes.md', 'content': '一份资料。'})
        self.assertEqual(doc.status_code, 200)
        self.assertEqual(self.client.get(f'/api/projects/{project}/documents').json()[0]['name'], 'notes.md')
        memory = self.client.post('/api/memories', json={'scope': 'conversation', 'content': '中文回答'}).json()['id']
        self.assertEqual(len(self.client.get('/api/memories', params={'project_id': project}).json()), 1)
        self.assertTrue(self.client.post(f'/api/memories/{memory}/forget').json()['forgotten'])
        self.assertEqual(self.client.get('/api/memories', params={'project_id': project}).json(), [])
        with patch.dict(os.environ, {'AGENT_API_KEY': 'fake-key'}, clear=True), patch.object(api, 'StreamingChatLLM', FakeStreamingModel), patch.object(api.agent, 'load_env'):
            response = self.client.post('/api/research/stream', json={'project_id': project, 'task': '分析资料'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/event-stream', response.headers['content-type'])
        events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
        self.assertEqual(''.join(e['text'] for e in events if e['type'] == 'model_delta'), '研究完成')
        self.assertTrue(any(e['type'] == 'event' and e['event']['type'] == 'model.call.started' for e in events))
        self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(events[-1]['result']['result']['status'], 'completed')
        self.assertNotIn('fake-key', response.text)

    def test_invalid_project_and_duplicate_document_rejected(self):
        self.assertEqual(self.client.get('/api/projects/999/documents').status_code, 400)
        project = self.client.post('/api/projects', json={'name': '项目'}).json()['id']
        payload = {'name': 'notes.md', 'content': '文本'}
        self.assertEqual(self.client.post(f'/api/projects/{project}/documents', json=payload).status_code, 200)
        self.assertEqual(self.client.post(f'/api/projects/{project}/documents', json=payload).status_code, 400)
        self.assertEqual(self.client.post(f'/api/projects/{project}/documents', json={'name': '../hack.md', 'content': '文本'}).status_code, 400)

    def test_leaked_protocol_stream_ends_as_error_without_report(self):
        from test_response_contract import RAW
        class Upstream:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def __iter__(self):
                for text in (RAW[:5], RAW[5:]):
                    yield ('data: ' + json.dumps({'choices':[{'delta':{'content':text}}]}) + '\n').encode()
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n'
                yield b'data: [DONE]\n'
        project = self.client.post('/api/projects', json={'name': 'protocol failure'}).json()['id']
        with patch.dict(os.environ, {'AGENT_API_KEY':'fake-only'}, clear=True), patch.object(api.agent, 'load_env'), patch('urllib.request.urlopen', return_value=Upstream()):
            response = self.client.post('/api/research/stream', json={'project_id':project,'task':'综述','max_steps':1})
        events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
        self.assertNotIn('DSML', ''.join(e['text'] for e in events if e['type']=='model_delta'))
        self.assertEqual(events[-1]['type'], 'done')
        output = events[-1]['result']
        self.assertEqual(output['result']['status'], 'error')
        self.assertIsNone(output['result']['answer'])
        self.assertEqual(output['result']['error']['code'], 'tool_protocol_in_answer')
        self.assertNotIn('report_artifact', output)
        self.assertEqual(self.client.get(f'/api/projects/{project}/artifacts').json()['artifacts'], [])

if __name__ == '__main__': unittest.main()
