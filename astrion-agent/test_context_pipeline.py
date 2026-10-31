"""五层的行为回归：原文可恢复、作用域隔离、预算收益、服务端故障。"""
import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from context_adapter import HttpContextAdapter
from context_store import ContextStore
from memory import Memory
from test_memory import call, document, summary
from memory_evidence import extract_tool_evidence


def observed(memory, name='read_file', content=None, call_id='read', thought=None):
    content = content or json.dumps({'path': 'paper.txt', 'content': 'evidence n=128\n' * 500})
    message = call(call_id, name, path='paper.txt')
    message['content'] = thought
    memory.add_assistant(message)
    memory.add_tool(call_id, content, evidence=extract_tool_evidence(name, content))
    return memory._history[-1]['_ref']


class ContextPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'context.sqlite3'

    def memory(self, **kwargs):
        memory = Memory('规则', archive=ContextStore(self.path), **kwargs)
        memory.add_user('只用项目 A；准确保留数字，引用原文。')
        return memory

    def test_large_result_is_externalized_without_model_call_and_survives_restart(self):
        memory = self.memory()
        original = 'first n=128\n' + '大段原文\n' * 5000 + 'last accuracy=81%'
        ref = observed(memory, content=json.dumps({'path': 'paper.txt', 'content': original}, ensure_ascii=False))
        evidence_ref = memory._history[-1]['_evidence_refs'][0]
        before = memory.chars()
        self.assertTrue(memory.compact(lambda _: self.fail('L1 不应调用模型'), force=False))
        self.assertLess(memory.chars(), before // 3)
        self.assertEqual(memory.layer_reports[0]['name'], 'tool_result_budget')
        self.assertTrue(memory.layer_reports[0]['changed'])
        memory.checkpoint(task='测试', status='running', steps=1)
        scope = memory.archive.scope
        restored = Memory.from_archive(self.path, scope)
        parts, offset = [], 0
        while True:
            page = json.loads(restored.read_evidence(evidence_ref, offset, 1000))
            parts.append(page['text'])
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        self.assertEqual(''.join(parts), original)
        self.assertEqual(json.loads(restored.archive.get(ref)['content'])['content'], original)
        self.assertEqual(restored.for_model(), memory.for_model())
        self.assertEqual(restored.archive.messages, memory.archive.messages)

    def test_search_finds_removed_references_and_is_paginated(self):
        memory = self.memory()
        for i in range(12):
            memory.archive.put('document', f'样本量 n={i}', {'document_id': i})
        page = json.loads(memory.search_evidence('样本量'))
        self.assertEqual((len(page['results']), page['total'], page['next_offset']), (5, 12, 5))
        last = json.loads(memory.search_evidence('样本量', 10))
        self.assertEqual(len(last['results']), 2)
        self.assertIsNone(last['next_offset'])

    def test_reread_page_reaches_model_without_being_externalized_again(self):
        memory = self.memory(tool_limit_chars=600)
        ref = memory.archive.put('file', '原文' * 3000)
        memory.add_assistant(call('page', 'read_evidence', ref=ref, limit=4000))
        memory.add_tool('page', memory.read_evidence(ref, limit=4000))
        memory.compact(force=False)
        page = json.loads(memory.for_model()[-1]['content'])
        self.assertEqual(page['text'], '原文' * 2000)
        self.assertEqual(page['next_offset'], 4000)
        self.assertNotIn('archived', page)

    def test_scope_isolates_agents_even_when_they_share_a_database(self):
        first, second = self.memory(), self.memory()
        ref = first.archive.put('document', '项目 A 的秘密实验', {'document_id': 1})
        self.assertEqual(json.loads(second.search_evidence('秘密'))['total'], 0)
        with self.assertRaises(ValueError):
            second.read_evidence(ref)
        with self.assertRaises(ValueError):
            second.pin_evidence(ref, '秘密实验')

    def test_pin_is_verbatim_bounded_and_persists_across_all_layers(self):
        memory = self.memory(budget_chars=1000, keep_recent=1)
        record = document()
        observed(memory, name='read_knowledge', content=json.dumps({'results': [record]}),
                 thought='读取过程。' * 300)
        ref = memory._history[-1]['_evidence_refs'][0]
        memory.pin_evidence(ref, record['excerpt'])
        for i in range(5):
            observed(memory, call_id=str(i), thought='继续过程。' * 300)
        memory.compact(summary)
        self.assertIn(record['excerpt'], json.dumps(memory.for_model(), ensure_ascii=False))
        self.assertIn('sha256', json.dumps(memory.for_model()))
        self.assertEqual(memory.check(), [])
        before = copy.deepcopy(memory._pins)
        with self.assertRaises(ValueError):
            memory.pin_evidence(ref, '准确率 99%')
        self.assertEqual(memory._pins, before)
        huge_ref = memory.archive.put('file', 'a' * 2000)
        with self.assertRaises(ValueError):
            memory.pin_evidence(huge_ref, 'a' * 1001)
        memory.pin_evidence(ref, '')
        self.assertFalse(memory._pins)
        self.assertEqual(json.loads(memory.read_evidence(ref))['text'], record['excerpt'])

    def test_snip_only_removes_duplicate_read_batches_and_preserves_journal(self):
        memory = self.memory()
        memory.enabled_layers = {2}
        data = json.dumps({'path': 'paper.txt', 'content': 'n=128'})
        observed(memory, content=data, call_id='a')
        observed(memory, content=data, call_id='b')
        journal = copy.deepcopy(memory.archive.messages)
        self.assertTrue(memory.compact())
        self.assertEqual([m['tool_call_id'] for m in memory.for_model() if m['role'] == 'tool'], ['b'])
        self.assertEqual(memory.archive.messages, journal)
        self.assertEqual(memory.check(), [])
        for name in ('write_file', 'calculator'):
            with self.subTest(name=name):
                m = self.memory()
                m.enabled_layers = {2}
                observed(m, name=name, content='same', call_id='a')
                observed(m, name=name, content='same', call_id='b')
                self.assertFalse(m.compact())

    def test_micro_keeps_call_result_pair_and_skips_recent_or_failed_results(self):
        memory = self.memory(budget_chars=1000, keep_recent=1)
        memory.enabled_layers = {3}
        first = observed(memory, content='old ' * 350, call_id='old')
        memory.add_assistant(call('failed', 'read_file', path='missing'))
        memory.add_tool('failed', 'Error ' * 300, outcome='error')
        observed(memory, content='recent ' * 200, call_id='recent')
        self.assertTrue(memory.compact())
        tools = {m['tool_call_id']: m for m in memory.for_model() if m['role'] == 'tool'}
        self.assertEqual(json.loads(tools['old']['content'])['ref'], first)
        self.assertIn('Error Error', tools['failed']['content'])
        self.assertIn('recent recent', tools['recent']['content'])
        self.assertEqual(memory.check(), [])

    def test_local_fold_keeps_recent_work_and_global_fold_rebuilds_older_state(self):
        memory = self.memory(budget_chars=1000, keep_recent=1)
        for i in range(8):
            memory.add_assistant({'content': f'阶段 {i} ' + '过程文字。' * 300})
        original = copy.deepcopy(memory.archive.messages)
        stages = []
        def summarize(transcript):
            stages.append(json.loads(transcript)['stage'])
            return summary(transcript)
        self.assertTrue(memory.compact(summarize))
        self.assertEqual(stages, ['context_collapse', 'auto_compact'])
        self.assertEqual([r['layer'] for r in memory.layer_reports], [1, 2, 3, 4, 5])
        self.assertTrue(memory.layer_reports[3]['changed'])
        self.assertTrue(memory.layer_reports[4]['changed'])
        self.assertIn('阶段 7', memory.for_model()[-1]['content'])
        self.assertEqual(memory.archive.messages, original)
        for report in memory.layer_reports[3:]:
            saved = json.loads(memory.archive.get(report['history_ref'])['content'])
            self.assertTrue(saved)

    def test_optional_remote_adapter_cannot_clear_a_pinned_user_or_unknown_message(self):
        class BadAdapter:
            def select(self, candidates, budget):
                return [1]  # 用户消息，而非服务器收到的候选工具结果。
        memory = self.memory(budget_chars=1000, keep_recent=1, context_adapter=BadAdapter())
        memory.enabled_layers = {3}
        observed(memory, content='archive ' * 200)
        memory.add_assistant({'content': '最新消息'})
        self.assertTrue(memory.compact())
        report = memory.layer_reports[2]
        self.assertEqual(report['adapter'], 'client_fallback')
        self.assertEqual(report['adapter_error'], 'ValueError')
        self.assertEqual(memory.last_user(), '只用项目 A；准确保留数字，引用原文。')

    def test_http_adapter_sends_only_candidates_and_validates_server_contract(self):
        adapter = HttpContextAdapter('http://127.0.0.1:9001/context-edits', key='dedicated-key')
        candidates = [{'id': 3, 'tool': 'read_file', 'chars': 1200}]
        with patch('context_adapter.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(b'{"version":1,"clear_ids":[3]}')
            self.assertEqual(adapter.select(candidates, 500), [3])
            request = opener.return_value.open.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(payload['candidates'], candidates)
            self.assertNotIn('messages', payload)
            self.assertEqual(request.headers['Authorization'], 'Bearer dedicated-key')
            for raw in (b'{"version":1,"clear_ids":[99]}', b'{"version":1,"clear_ids":[3,3]}', b'{}'):
                opener.return_value.open.return_value = io.BytesIO(raw)
                with self.assertRaises(ValueError):
                    adapter.select(candidates, 500)

    def test_corrupted_persistent_artifact_is_rejected_on_restore(self):
        memory = self.memory()
        ref = observed(memory)
        memory.checkpoint(status='running')
        from contextlib import closing
        with closing(sqlite3.connect(self.path)) as db, db:
            raw = db.execute('SELECT record FROM context_artifacts WHERE ref=?', (ref,)).fetchone()[0]
            record = json.loads(raw)
            record['content'] = 'tampered'
            db.execute('UPDATE context_artifacts SET record=? WHERE ref=?', (json.dumps(record), ref))
        with self.assertRaises(ValueError):
            Memory.from_archive(self.path, memory.archive.scope)

    def test_storage_failure_never_publishes_an_unrecoverable_reference(self):
        memory = self.memory()
        memory.add_assistant(call('a', 'read_file', path='paper.txt'))
        with patch.object(memory.archive, 'put', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                memory.add_tool('a', 'original observation')
        # 没有伪造可重读引用；故障明确抛出，不能继续向模型发送缺失结果。
        self.assertNotIn('archived', json.dumps(memory.for_model()))
        self.assertTrue(memory.check())

    def test_checkpoint_ignores_unfinished_tail_but_retains_its_raw_history(self):
        memory = self.memory()
        observed(memory, content='first')
        memory.checkpoint(status='running', steps=1)
        memory.add_assistant(call('unfinished', 'read_file', path='paper.txt'))
        restored = Memory.from_archive(self.path, memory.archive.scope)
        self.assertEqual(restored.check(), [])
        self.assertEqual(restored.runtime['steps'], 1)
        self.assertEqual(len(restored.archive.messages), len(memory.archive.messages))

    def test_older_session_can_attach_newer_journal_without_losing_crash_tail(self):
        memory = self.memory()
        observed(memory, content='completed observation')
        snapshot = memory.to_dict()
        memory.add_assistant(call('unfinished', 'read_file', path='paper.txt'))
        journal = copy.deepcopy(memory.archive.messages)
        restored = Memory.from_dict(snapshot)
        restored.attach_archive(self.path)
        self.assertEqual(restored.check(), [])
        self.assertEqual(restored.archive.messages, journal)
        self.assertEqual(restored.for_model(), Memory.from_dict(snapshot).for_model())
        restored.add_assistant({'content': 'continued after recovery'})
        self.assertEqual(restored._history[-1]['_seq'], len(journal) + 1)
        self.assertEqual(restored.archive.messages[:len(journal)], journal)
        conflict = copy.deepcopy(snapshot['archive_snapshot'])
        conflict['messages'][0]['content'] = 'conflicting task'
        with self.assertRaises(ValueError):
            restored.archive.import_snapshot(conflict)


if __name__ == '__main__':
    unittest.main()
