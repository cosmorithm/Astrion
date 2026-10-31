"""研究模块离线集成测试，使用真实 SQLite 和脚本化模型。"""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from research.store import Store
from research.runner import run_research, audit_citations


def call(name, args, call_id):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.inputs = []
    def chat(self, messages, tools=None):
        self.inputs.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        return next(self.responses)


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'db.sqlite')
        self.addCleanup(self.store.close)
        self.a = self.store.create_project('项目 A')
        self.b = self.store.create_project('项目 B')

    def test_global_project_scope_and_forgotten_memory_survive_reopen(self):
        global_id = self.store.add_memory('conversation', '请使用中文')
        a_id = self.store.add_memory('project', '实验甲使用中文', self.a)
        self.store.add_memory('project', '实验乙使用中文', self.b)
        self.assertEqual({m['id'] for m in self.store.search_memories(self.a, '中文')}, {global_id, a_id})
        self.assertEqual(len(self.store.search_memories(self.b, '中文')), 2)
        self.assertTrue(self.store.forget(global_id))
        self.assertFalse(self.store.forget(global_id))
        self.store.close()
        self.store = Store(self.root / 'db.sqlite')
        self.addCleanup(self.store.close)
        self.assertEqual([m['id'] for m in self.store.memories(self.a)], [a_id])
        self.assertEqual(self.store.db.execute('SELECT status FROM memories WHERE id=?', (global_id,)).fetchone()[0], 'forgotten')

    def test_invalid_memory_scope_does_not_write(self):
        with self.assertRaises(ValueError): self.store.add_memory('conversation', '内容', self.a)
        with self.assertRaises(ValueError): self.store.add_memory('project', '内容')
        self.assertEqual(self.store.memories(self.a), [])

    def test_read_adjacent_lines_is_bounded_and_project_scoped(self):
        doc = self.store.add_document_text(self.a, 'paper.txt', '\n'.join(f'第{i}行' for i in range(1, 101)))
        result = self.store.read_document(self.a, doc, 9, 12)
        self.assertEqual(result['excerpt'], '第9行\n第10行\n第11行\n第12行')
        self.assertEqual((result['start_line'], result['end_line'], result['total_lines']), (9, 12, 100))
        self.assertFalse(result['truncated'])
        self.assertEqual(self.store.read_document(self.a, doc, 99, 105)['end_line'], 100)
        with self.assertRaises(ValueError): self.store.read_document(self.b, doc, 1, 10)
        for start, end in [(0, 10), (10, 9), (1, 81), (101, 105), (True, 10)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.store.read_document(self.a, doc, start, end)

    def test_repeated_search_notices_and_adjacent_read_citations(self):
        doc = self.store.add_document_text(self.a, 'paper.txt', '\n'.join(['研究依据'] * 10 + ['后续结论'] * 5))
        llm = ScriptedLLM([
            call('search_knowledge', {'query': '依据'}, 's1'),
            call('search_knowledge', {'query': '依据'}, 's2'),
            call('read_knowledge', {'document_id': doc, 'start_line': 11, 'end_line': 15}, 'r1'),
            {'role': 'assistant', 'content': f'后续结论 [D{doc}:L11-L15]'},
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_research(self.store, self.a, '研究', llm, max_steps=4)
        observed = [json.loads(i['event']['payload']['content']) for i in result['trace']
                    if i['event']['type'] == 'tool.call.finished' and i['event']['payload']['name'] == 'search_knowledge']
        self.assertEqual([o['new_ranges'] for o in observed], [1, 0])
        self.assertEqual(observed[0]['results'], observed[1]['results'])
        self.assertEqual(result['citation_audit']['status'], 'locations_found')
        self.assertEqual(result['result']['steps'], 4)
        self.assertEqual(llm.inputs[-1][1], [])
        self.assertEqual(result['result']['status'], 'no_tool_call')
        self.assertIn('最后一轮', llm.inputs[-1][0][0]['content'])

    def test_truncated_direct_read_is_not_a_verified_citation(self):
        doc = self.store.add_document_text(self.a, 'long.txt', 'x' * 6100)
        llm = ScriptedLLM([
            call('read_knowledge', {'document_id': doc, 'start_line': 1, 'end_line': 1}, 'r1'),
            call('finish', {'answer': f'引用 [D{doc}:L1-L1]'}, 'f1'),
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_research(self.store, self.a, '读取', llm)
        self.assertEqual(result['citation_audit']['status'], 'invalid_locations')

    def test_citations_can_span_adjacent_evidence_but_not_unread_gaps(self):
        trace = [{'event': {'type': 'tool.call.finished', 'payload': {
            'name': 'read_knowledge', 'outcome': 'success', 'content': json.dumps({'results': [
                {'document_id': 1, 'start_line': 1, 'end_line': 10, 'truncated': False},
                {'document_id': 1, 'start_line': 11, 'end_line': 20, 'truncated': False},
                {'document_id': 1, 'start_line': 22, 'end_line': 25, 'truncated': False},
            ]})}}}]
        result = audit_citations('[D1:L8-L15] [D1:L19-L23]', trace)
        self.assertEqual([c['in_retrieved_range'] for c in result['checks']], [True, False])

    def test_contained_read_is_not_new_evidence(self):
        doc = self.store.add_document_text(self.a, 'paper.txt', '\n'.join(['原文'] * 30))
        llm = ScriptedLLM([
            call('read_knowledge', {'document_id': doc, 'start_line': 1, 'end_line': 20}, 'r1'),
            call('read_knowledge', {'document_id': doc, 'start_line': 5, 'end_line': 15}, 'r2'),
            call('finish', {'answer': '已回答'}, 'f1'),
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_research(self.store, self.a, '研究', llm)
        reads = [json.loads(i['event']['payload']['content']) for i in result['trace']
                 if i['event']['type'] == 'tool.call.finished' and i['event']['payload']['name'] == 'read_knowledge']
        self.assertEqual([r['new_ranges'] for r in reads], [1, 0])

    def test_document_search_preserves_lines_and_project_isolation(self):
        a = self.root / 'a.md'; a.write_text('第一行\nAgent 检索原始证据\n第三行', encoding='utf-8')
        b = self.root / 'b.md'; b.write_text('Agent 项目 B 私有资料', encoding='utf-8')
        aid = self.store.add_document(self.a, a)
        self.store.add_document(self.b, b)
        result = self.store.search_documents(self.a, 'Agent 证据')
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]['document_id'], result[0]['start_line'], result[0]['end_line']), (aid, 1, 3))
        self.assertIn('原始证据', result[0]['excerpt'])
        with self.assertRaises(ValueError): self.store.search_documents(999, 'Agent')
        with self.assertRaises(ValueError): self.store.search_documents(self.a, '')

    def test_root_delegates_child_cannot_delegate_and_results_return_to_root(self):
        source = self.root / 'study.md'; source.write_text('Agent 检索资料需要保留依据。', encoding='utf-8')
        doc = self.store.add_document(self.a, source)
        llm = ScriptedLLM([
            call('run_worker', {'task': '研究资料', 'context': '核对依据'}, 'r1'),
            call('search_knowledge', {'query': 'Agent 依据'}, 'w1'),
            call('run_worker', {'task': '违反层数', 'context': ''}, 'w2'),
            call('finish', {'answer': f'找到依据 [D{doc}:L1-L1]'}, 'w3'),
            call('finish', {'answer': f'结论 [D{doc}:L1-L1]'}, 'r2'),
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_research(self.store, self.a, '总任务', llm, max_workers=1)
        self.assertEqual(result['result']['status'], 'completed')
        self.assertEqual(result['workers'][0]['status'], 'completed')
        self.assertEqual(result['citation_audit']['status'], 'locations_found')
        self.assertFalse(result['citation_audit']['semantic_support_checked'])
        root_tools = {t['function']['name'] for t in llm.inputs[0][1]}
        child_tools = {t['function']['name'] for t in llm.inputs[1][1]}
        self.assertIn('run_worker', root_tools)
        self.assertNotIn('run_worker', child_tools)
        self.assertNotIn('write_file', root_tools | child_tools)
        child_error = next(item for item in result['trace'] if item['role'] == 'worker' and item['event']['type'] == 'tool.call.finished' and item['event']['payload']['name'] == 'run_worker')
        self.assertEqual(child_error['event']['payload']['outcome'], 'rejected')
        self.assertNotIn('总任务', json.dumps(llm.inputs[1][0], ensure_ascii=False))
        self.assertIn('找到依据', json.dumps(llm.inputs[-1][0], ensure_ascii=False))

    def test_exhausted_worker_is_not_misreported_as_completed(self):
        llm = ScriptedLLM([
            call('run_worker', {'task': '探索', 'context': ''}, 'r1'),
            call('search_knowledge', {'query': 'something'}, 'w1'),
            call('finish', {'answer': '子任务未完成'}, 'r2'),
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_research(self.store, self.a, '研究', llm, worker_steps=1)
        self.assertEqual(result['workers'][0]['status'], 'step_limit')
        self.assertIsNone(result['workers'][0]['answer'])
        self.assertEqual(result['citation_audit']['status'], 'no_citations')

    def test_citation_audit_rejects_unseen_and_truncated_text(self):
        event = {'role': 'root', 'event': {'type': 'tool.call.finished', 'payload': {
            'name': 'search_knowledge', 'outcome': 'success',
            'content': json.dumps({'results': [{'document_id': 1, 'start_line': 1, 'end_line': 10, 'truncated': True}]})}}}
        result = audit_citations('文本 [D1:L1-L2] [D2:L1-L2]', [event])
        self.assertEqual(result['status'], 'invalid_locations')
        self.assertFalse(any(c['in_retrieved_range'] for c in result['checks']))

if __name__ == '__main__': unittest.main()
