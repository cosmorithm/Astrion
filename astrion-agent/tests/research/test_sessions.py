"""Continuation restores real checkpoint evidence and preserves report versions."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from memory import Memory
from research.runner import run_research
from research.runs import RunRepository
from research.store import Store
from research.storage.artifacts import Artifacts
from test_research import ScriptedLLM, call


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'db.sqlite3')
        self.addCleanup(self.store.close)
        self.project = self.store.create_project('Study')
        self.doc = self.store.add_document_text(self.project, 'study.md', 'Sample n=128.\nAccuracy=81%.')

    def run_model(self, messages, task='研究', **kwargs):
        model = ScriptedLLM(messages)
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_research(self.store, self.project, task, model, budget_chars=20000, **kwargs)
        return result, model

    def test_followups_fork_context_restore_plan_evidence_and_keep_original_report(self):
        plan = [{'goal': '核对样本', 'status': 'completed'}, {'goal': '补读结论', 'status': 'pending'}]
        first, _ = self.run_model([
            call('read_knowledge', {'document_id': self.doc, 'start_line': 1, 'end_line': 1}, 'read'),
            call('update_task_plan', {'items': plan}, 'plan'),
            {'role': 'assistant', 'content': f'初稿，样本128 [D{self.doc}:L1]；结论待读。'},
        ])
        path = Path(first['event_log']).with_name('result.json')
        original = path.read_bytes()
        info = first['context_archive']
        before = Memory.from_archive(info['path'], info['scope']).to_dict()
        second, llm = self.run_model([
            call('read_knowledge', {'document_id': self.doc, 'start_line': 2, 'end_line': 2}, 'read2'),
            call('update_task_plan', {'items': [{**p, 'status': 'completed'} for p in plan]}, 'plan2'),
            call('finish', {'answer': f'第二版，样本128，准确率81% [D{self.doc}:L1-L2]'}, 'finish2'),
        ], '继续读结论并完善报告', continue_run_id=first['run_id'])
        sent = json.dumps(llm.inputs[0][0], ensure_ascii=False)
        self.assertIn('初稿', sent)
        self.assertIn('继续读结论并完善报告', sent)
        self.assertIn('补读结论', sent)
        self.assertEqual(second['parent_run_id'], first['run_id'])
        self.assertNotEqual(second['context_archive']['scope'], info['scope'])
        self.assertEqual(second['citation_audit']['status'], 'locations_found')
        self.assertFalse(second['citation_audit']['semantic_support_checked'])
        self.assertEqual(second['result']['steps'], 3)  # new 50-round allowance, per-run measurement
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(Memory.from_archive(info['path'], info['scope']).to_dict(), before)
        a = Artifacts(self.store)
        self.assertIn('初稿', a.bytes(self.project, first['report_artifact']['id']).decode())
        self.assertIn('第二版', a.bytes(self.project, second['report_artifact']['id']).decode())
        self.assertNotEqual(first['report_artifact']['id'], second['report_artifact']['id'])
        third, _ = self.run_model([{'role': 'assistant', 'content': f'第三版 [D{self.doc}:L1-L2]'}],
                                  '再改一下措辞', continue_run_id=second['run_id'])
        self.assertEqual(third['citation_audit']['status'], 'locations_found')
        self.assertEqual(RunRepository(self.store, self.project).listing(limit=2)['total'], 3)
        self.assertEqual(RunRepository(self.store, self.project).listing(limit=2)['next_offset'], 2)

    def test_wrong_project_database_traversal_and_missing_archive_rejected(self):
        first, _ = self.run_model([{'role': 'assistant', 'content': 'Draft'}])
        other_project = self.store.create_project('Other')
        with self.assertRaises(ValueError):
            RunRepository(self.store, other_project).get(first['run_id'])
        other = Store(Path(self.tmp.name) / 'other.sqlite3')
        self.addCleanup(other.close)
        same_id = other.create_project('Study')
        with self.assertRaises(ValueError):
            RunRepository(other, same_id).get(first['run_id'])
        for identifier in ('../context.sqlite3', 'research_' + 'f' * 32):
            with self.assertRaises(ValueError):
                self.run_model([], continue_run_id=identifier)
        Path(first['context_archive']['path']).unlink()
        with self.assertRaisesRegex(ValueError, 'checkpoint_unavailable'):
            self.run_model([], continue_run_id=first['run_id'])

    def test_legacy_run_uses_actual_old_reads_not_report_citations(self):
        first, _ = self.run_model([
            call('read_knowledge', {'document_id': self.doc, 'start_line': 1, 'end_line': 1}, 'read'),
            {'role': 'assistant', 'content': f'旧报告自称读过第二行 [D{self.doc}:L2]'},
        ])
        path = Path(first['event_log']).with_name('result.json')
        old = json.loads(path.read_text())
        del old['observed_ranges']; del old['database_path']
        path.write_text(json.dumps(old))
        continued, _ = self.run_model([{'role': 'assistant', 'content': f'[D{self.doc}:L1] [D{self.doc}:L2]'}],
                                      continue_run_id=first['run_id'])
        self.assertEqual([i['in_retrieved_range'] for i in continued['citation_audit']['checks']], [True, False])

    def test_default_allows_more_than_ten_rounds_and_finishes_early(self):
        responses = [call('search_memory', {'query': 'test'}, f'm{i}') for i in range(12)]
        responses += [call('finish', {'answer': 'done'}, 'finish')]
        result, _ = self.run_model(responses)
        self.assertEqual(result['result']['steps'], 13)
        self.assertEqual(result['budgets'], {'max_steps': 50, 'worker_steps': 50, 'max_workers': 5})
        for invalid in ({'max_workers': 6}, {'max_steps': 51}, {'worker_steps': 51}):
            with self.assertRaises(ValueError): self.run_model([], **invalid)

    def test_failed_run_exposes_last_checkpoint_and_resumes_without_losing_reads(self):
        class FailsAfterRead(ScriptedLLM):
            def chat(self, messages, tools=None):
                if self.inputs:
                    raise RuntimeError('simulated network interruption')
                return super().chat(messages, tools)
        llm = FailsAfterRead([call('read_knowledge', {'document_id': self.doc, 'start_line': 1, 'end_line': 1}, 'read')])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            run_research(self.store, self.project, '研究', llm)
        repo = RunRepository(self.store, self.project)
        failed = repo.listing()['runs'][0]
        self.assertTrue(failed['can_continue'])
        saved = repo.get(failed['run_id'])
        self.assertEqual(saved['status'], 'error')
        restored = Memory.from_archive(**saved['context_archive'])
        self.assertEqual(restored.runtime['steps'], 1)
        self.assertEqual(saved['observed_ranges'], [[self.doc, 1, 1]])
        resumed, model = self.run_model(
            [call('finish', {'answer': f'已恢复 [D{self.doc}:L1]'}, 'finish')],
            '从失败前的进度继续', continue_run_id=failed['run_id'])
        self.assertEqual(resumed['parent_run_id'], failed['run_id'])
        self.assertEqual(resumed['citation_audit']['status'], 'locations_found')
        self.assertIn('Sample n=128', json.dumps(model.inputs[0][0], ensure_ascii=False))

    def test_legacy_recovery_sidecar_is_project_and_database_scoped(self):
        first, _ = self.run_model([{'role': 'assistant', 'content': 'Draft'}])
        repo = RunRepository(self.store, self.project)
        path = repo.directory(first['run_id']) / 'result.json'
        saved = json.loads(path.read_text())
        info = saved.pop('context_archive')
        path.write_text(json.dumps(saved))
        before = path.read_bytes()
        sidecar = path.with_name('recovery.json')
        sidecar.write_text(json.dumps({'run_id': first['run_id'], 'database_path': '/wrong', 'context_archive': info}))
        self.assertFalse(repo.listing()['runs'][0]['can_continue'])
        sidecar.write_text(json.dumps({'run_id': first['run_id'], 'database_path': str(self.store.path.resolve()), 'context_archive': info,
                                       'workers': [{'worker_id': 'worker_1', 'status': 'completed'}]}))
        self.assertTrue(repo.listing()['runs'][0]['can_continue'])
        self.assertEqual(repo.get(first['run_id'])['context_archive'], info)
        self.assertEqual(repo.get(first['run_id'])['workers'][0]['worker_id'], 'worker_1')
        self.assertEqual(path.read_bytes(), before)
