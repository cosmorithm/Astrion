import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from unittest.mock import patch

from research.config import context_policy
from research.runner import run_research
from research.runs import RunRepository
from research.store import Store
from research import api
from test_research import ScriptedLLM, call
from research.streaming import DeltaAssembler
from tools.research_history import HistoryTools


class TraceContextTests(unittest.TestCase):
    def test_provider_reasoning_is_retained_in_response_without_synthetic_text(self):
        stream = DeltaAssembler(lambda *_: None)
        stream.add({'choices': [{'delta': {'reasoning_content': 'step 1 '}}]})
        stream.add({'choices': [{'delta': {'reasoning_content': 'step 2', 'content': 'answer'}, 'finish_reason': 'stop'}]})
        self.assertEqual(stream.message()['reasoning_content'], 'step 1 step 2')
    def test_decision_messages_and_tools_are_persisted_exactly_as_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'db')
            self.addCleanup(store.close)
            project = store.create_project('project')
            doc = store.add_document_text(project, 'source.md', 'Exact evidence')
            model = ScriptedLLM([
                call('read_knowledge', {'document_id': doc, 'start_line': 1, 'end_line': 1}, 'read'),
                call('finish', {'answer': 'done'}, 'finish'),
            ])
            with contextlib.redirect_stdout(io.StringIO()):
                result = run_research(store, project, 'question', model)
            page = RunRepository(store, project).events_page(result['run_id'], limit=20)
            start = [i['event']['payload'] for i in page['events'] if i['event']['type'] == 'model.call.started']
            self.assertEqual(len(start), 2)
            for n, payload in enumerate(start):
                self.assertEqual(payload['request_snapshot']['messages'], model.inputs[n][0])
                self.assertEqual(payload['request_snapshot']['tools'], model.inputs[n][1])
            self.assertIn('Exact evidence', json.dumps(start[1]['request_snapshot'], ensure_ascii=False))
            finished = [i['event']['payload'] for i in page['events'] if i['event']['type'] == 'model.call.finished']
            self.assertEqual(finished[-1]['response']['tool_calls'][0]['function']['name'], 'finish')
            all_events = []
            offset = 0
            repo = RunRepository(store, project)
            while offset is not None:
                page = repo.events_page(result['run_id'], offset, 2)
                all_events.extend(page['events']); offset = page['next_offset']
            self.assertEqual(all_events, result['trace'])
            history = HistoryTools(store, project)
            listed = json.loads(history.listing())['runs']
            self.assertEqual(listed[0]['run_id'], result['run_id'])
            model_index = next(n for n, item in enumerate(all_events) if item['event']['type'] == 'model.call.started')
            excerpt = json.loads(history.read(result['run_id'], model_index, limit=100))
            self.assertEqual(excerpt['event_index'], model_index)
            self.assertTrue(excerpt['untrusted_historical_data'])
            self.assertIsNotNone(excerpt['next_offset'])
            other = store.create_project('other')
            with self.assertRaises(ValueError): RunRepository(store, other).events_page(result['run_id'])
            with self.assertRaises(ValueError): HistoryTools(store, other).read(result['run_id'], model_index)
            store.close()
            store = Store(Path(tmp) / 'db')
            self.addCleanup(store.close)
            self.assertTrue(RunRepository(store, project).events_page(result['run_id'])['events'])

    def test_window_policy_does_not_assume_third_party_or_unknown_models(self):
        official = context_policy('https://api.deepseek.com', 'deepseek-flash')
        self.assertEqual(official['window_tokens'], 1048576)
        self.assertEqual(official['budget_chars'], 600000)
        for base, model in [('https://other.invalid', 'deepseek-flash'), ('https://api.deepseek.com', 'unknown')]:
            self.assertIsNone(context_policy(base, model)['window_tokens'])
            self.assertEqual(context_policy(base, model)['budget_chars'], 12000)

    def test_event_api_history_and_project_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'db'
            store = Store(db)
            project = store.create_project('project'); other = store.create_project('other')
            with contextlib.redirect_stdout(io.StringIO()):
                result = run_research(store, project, 'question', ScriptedLLM([{'role': 'assistant', 'content': 'answer'}]))
            store.close()
            with patch.object(api.app.state, 'store_factory', lambda: Store(db)), TestClient(api.app) as client:
                url = f'/api/projects/{project}/runs/{result["run_id"]}/events'
                response = client.get(url, params={'limit': 1})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['next_offset'], 1)
                self.assertEqual(client.get(url, params={'limit': 21}).status_code, 400)
                self.assertEqual(client.get(f'/api/projects/{other}/runs/{result["run_id"]}/events').status_code, 400)
