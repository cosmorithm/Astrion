"""Router decisions must produce real isolated workers and visible, bounded handoff."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from research.runner import run_research
from research.store import Store
from test_research import ScriptedLLM, call


class CountingModel(ScriptedLLM):
    def __init__(self, responses, role='root'):
        super().__init__(responses)
        self.role = role
        self.usage_totals = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
        self.usage_calls = self.usage_unknown = 0
        self.last_usage = None

    def chat(self, messages, tools=None):
        self.usage_calls += 1
        self.last_usage = {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}
        for key, value in self.last_usage.items():
            self.usage_totals[key] += value
        return super().chat(messages, tools)


class DelegationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'db.sqlite3')
        self.addCleanup(self.store.close)
        self.project = self.store.create_project('router')
        self.doc = self.store.add_document_text(self.project, 'paper.md', '第一行证据\n第二行证据')
        self.allowed = {'finish', 'read_knowledge', 'list_documents', 'run_worker',
                        'run_workers', 'read_worker_report'}

    def make_model(self, router_content):
        doc = self.doc
        children = []
        class Parent(CountingModel):
            def fork(self, role):
                if role == 'router':
                    child = CountingModel([{'role': 'assistant', 'content': router_content}], role)
                else:
                    child = CountingModel([
                        call('read_knowledge', {'document_id': doc, 'start_line': 1, 'end_line': 1}, role+'-read'),
                        call('finish', {'answer': '已读第一行'}, role+'-done'),
                    ], role)
                children.append(child)
                return child
        return Parent([{'role': 'assistant', 'content': '最终回答'}]), children

    def test_structured_dispatch_is_visible_and_costed_without_expanding_permissions(self):
        proposal = json.dumps({'mode': 'delegate', 'reason': '分头读资料', 'tasks': [
            {'task': '核对论文方法', 'context': '方法'}, {'task': '核对论文结论', 'context': '结论'}]}, ensure_ascii=False)
        model, clients = self.make_model(proposal)
        task = '只允许阅读第一行，不要联网；分别核对方法和结论。'
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_research(self.store, self.project, task, model, max_steps=3, max_workers=2,
                                  worker_steps=3, budget_chars=20000, tool_allowlist=self.allowed,
                                  delegation_mode='structured')
        routing = result['delegation_routing']
        self.assertEqual(routing['proposal']['mode'], 'delegate')
        self.assertEqual(routing['dispatch']['status'], 'returned')
        self.assertEqual(len(result['workers']), 2)
        root_input = json.dumps(model.inputs[0][0], ensure_ascii=False)
        self.assertIn('delegation_observation', root_input)
        self.assertIn('已读第一行', root_input)
        self.assertIn('未核查数据', root_input)
        for child in [c for c in clients if c.role.startswith('worker_')]:
            messages, tools = child.inputs[0]
            text = json.dumps(messages, ensure_ascii=False)
            self.assertIn(task, text)
            names = {item['function']['name'] for item in tools}
            self.assertIn('read_knowledge', names)
            self.assertFalse(names & {'execute_python', 'write_project_file', 'read_webpage', 'run_worker', 'run_workers'})
        router_events = [x['event'] for x in result['trace'] if x['role'] == 'router']
        self.assertEqual([x['type'] for x in router_events], ['model.call.started', 'model.call.finished', 'decision.finished'])
        self.assertTrue(all(x['step'] is None for x in router_events))
        self.assertEqual(routing['router_usage']['totals']['total_tokens'], 10)
        self.assertEqual(result['usage']['totals']['total_tokens'], 60)  # router 10 + two workers 40 + root 10
        self.assertEqual(result['usage_including_root_review']['totals']['total_tokens'], 60)

    def test_invalid_router_output_falls_back_and_keeps_accounting(self):
        model, _ = self.make_model('not JSON')
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_research(self.store, self.project, '查论文', model, max_steps=3, max_workers=2,
                                  worker_steps=3, budget_chars=20000, tool_allowlist=self.allowed,
                                  delegation_mode='structured')
        self.assertEqual(result['delegation_routing']['proposal']['status'], 'fallback')
        self.assertEqual(result['workers'], [])
        self.assertNotIn('delegation_observation', json.dumps(model.inputs[0][0], ensure_ascii=False))
        self.assertEqual(result['usage']['totals']['total_tokens'], 20)

    def test_zero_workers_does_not_invoke_router_model(self):
        model, clients = self.make_model('unused')
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_research(self.store, self.project, '查论文', model, max_steps=3, max_workers=0,
                                  worker_steps=3, budget_chars=20000, tool_allowlist=self.allowed,
                                  delegation_mode='structured')
        self.assertEqual(result['delegation_routing']['proposal']['status'], 'skipped')
        self.assertEqual(clients, [])
        self.assertEqual(result['usage']['totals']['total_tokens'], 10)

    def test_continuation_preserves_historical_receipt_and_skips_incomplete_constraints(self):
        proposal = json.dumps({'mode': 'delegate', 'reason': '独立核对', 'tasks': [
            {'task': '检查方法', 'context': '方法'}]}, ensure_ascii=False)
        first_model, _ = self.make_model(proposal)
        with contextlib.redirect_stdout(io.StringIO()):
            first = run_research(self.store, self.project, '只用本地资料', first_model,
                                 max_steps=3, max_workers=2, worker_steps=3, budget_chars=20000,
                                 tool_allowlist=self.allowed, delegation_mode='structured')
        second_model, children = self.make_model(proposal)
        with contextlib.redirect_stdout(io.StringIO()):
            second = run_research(self.store, self.project, '继续，保留原有约束', second_model,
                                  max_steps=3, max_workers=2, worker_steps=3, budget_chars=20000,
                                  tool_allowlist=self.allowed, delegation_mode='structured',
                                  continue_run_id=first['run_id'])
        self.assertEqual(second['delegation_routing']['proposal']['reason'],
                         'continuation_constraints_unavailable')
        self.assertEqual(children, [])
        context = json.dumps(second_model.inputs[0][0], ensure_ascii=False)
        self.assertIn('只用本地资料', context)
        self.assertIn('上一轮研究的历史委派记录', context)
        self.assertIn('不能用于本轮', context)


if __name__ == '__main__':
    unittest.main()
