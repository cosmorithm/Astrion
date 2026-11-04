"""Offline tests for acceptance case provenance and update isolation."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.research.acceptance_v2 import __main__ as cli
from evaluation.research.acceptance_v2.run_update import run_update
from evaluation.research.acceptance_v2.run_case import run_case
from research.store import Store


class AcceptanceV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'base.sqlite3'
        store = Store(self.db)
        self.project = store.create_project('corpus')
        self.doc = store.add_document_text(self.project, 'P25.txt', 'first\nsecond')
        self.other = store.add_document_text(self.project, 'hidden.txt', 'secret')
        row = store.document_store.get(self.project, self.doc)
        meta = json.loads(row['metadata'])
        meta['pages'] = [{'page': 1, 'start_line': 1, 'end_line': 2}]
        with store.db:
            store.db.execute('UPDATE documents SET metadata=? WHERE id=?', (json.dumps(meta), self.doc))
        store.close()
        (self.root / 'P25.txt').write_bytes(row['original'])
        self.data = {'status': 'assistant_reviewed_human_pending',
                     'papers': {'P25': {'document_id': self.doc, 'name': 'P25.txt',
                                        'sha256': row['sha256'], 'text_sha256': meta['text_sha256']}},
                     'cases': [{'id': 'R21', 'task': 'initial', 'paper_ids': ['P25'],
                                'scheduled_user_updates': {'3': 'updated'},
                                'checks': [{'id': 'R21-01', 'expected': 'SECRET GOLD',
                                            'evidence': [{'paper_id': 'P25', 'pdf_page': 1,
                                                          'start_line': 1, 'end_line': 1}]}]}]}
        self.dir = self.root / 'cases'
        self.dir.mkdir()
        (self.dir / 'cases.json').write_text(json.dumps(self.data))

    def test_validate_detects_pdf_hash_and_page_mapping(self):
        with patch.object(cli, 'HERE', self.dir):
            self.assertTrue(cli.validate(self.root, self.db)['valid'])
            (self.root / 'P25.txt').write_text('tampered')
            self.assertFalse(cli.validate(self.root, self.db)['valid'])

    def test_validate_detects_changed_original_in_database(self):
        import sqlite3
        with sqlite3.connect(self.db) as db:
            db.execute('UPDATE documents SET original=? WHERE id=?', (b'tampered', self.doc))
        with patch.object(cli, 'HERE', self.dir):
            self.assertFalse(cli.validate(self.root, self.db)['valid'])

    def test_update_not_delivered_is_not_counted_as_violation_and_gold_not_leaked(self):
        observed = {}
        def fake_run(store, project, task, llm, **kwargs):
            observed.update(task=task, kwargs=kwargs, documents=store.documents(project))
            return {'run_id': 'test', 'result': {'status': 'no_tool_call', 'answer': 'done'},
                    'trace': [], 'duration_seconds': 1, 'usage': {}}
        with patch('evaluation.research.acceptance_v2.run_update.HERE', self.dir), \
             patch('evaluation.research.acceptance_v2.run_update.validate', return_value={'valid': True}), \
             patch('evaluation.research.acceptance_v2.run_update.check_base', return_value=(self.project, {'P25': self.doc})), \
             patch('evaluation.research.acceptance_v2.run_update.run_research', side_effect=fake_run):
            report = run_update(object, self.root / 'out', source=self.root, base=self.db, steps=3)
        self.assertFalse(report['update_delivery']['all_delivered'])
        self.assertEqual(report['case_outcome'], 'pending')
        self.assertEqual(observed['kwargs']['scheduled_user_updates'], {3: 'updated'})
        self.assertEqual([d['id'] for d in observed['documents']], [self.doc])
        self.assertNotIn('SECRET GOLD', observed['task'])
        self.assertTrue(Path(report['report_path']).exists())
        self.assertEqual(report['settings']['added_to_v1_allowlist'], ['list_claims', 'record_claim'])

    def test_case_records_claim_toolset_without_touching_v1_default(self):
        data = dict(self.data)
        data['cases'] = [{'id': 'R01', 'task': 'task', 'paper_ids': ['P25'],
                          'checks': [{'id': 'R01-01'}]}]
        (self.dir / 'cases.json').write_text(json.dumps(data))
        legacy = {'task': 'task', 'paper_ids': ['P25']}
        with patch('evaluation.research.acceptance_v2.run_case.HERE', self.dir), \
             patch('evaluation.research.acceptance_v2.run_case.validate', return_value={'valid': True}), \
             patch('evaluation.research.acceptance_v2.run_case.case_by_id', return_value=legacy), \
             patch('evaluation.research.acceptance_v2.run_case.evaluate',
                   return_value={'report_path': 'original.json', 'case_outcome': 'pending'}) as evaluate:
            report = run_case('R01', object, self.root / 'out', source=self.root, base=self.db)
        self.assertIn('record_claim', evaluate.call_args.kwargs['tool_allowlist'])
        self.assertEqual(report['acceptance_v2']['semantic_review'], 'pending')
        self.assertTrue(Path(report['report_path']).exists())

    def test_scheduled_update_reaches_real_loop(self):
        from test_research import ScriptedLLM, call
        model = ScriptedLLM([call('list_documents', {}, 'c1'),
                             call('list_documents', {}, 'c2'),
                             {'role': 'assistant', 'content': 'done'}])
        with patch('evaluation.research.acceptance_v2.run_update.HERE', self.dir), \
             patch('evaluation.research.acceptance_v2.run_update.validate', return_value={'valid': True}), \
             patch('evaluation.research.acceptance_v2.run_update.check_base', return_value=(self.project, {'P25': self.doc})):
            report = run_update(lambda: model, self.root / 'out', source=self.root, base=self.db, steps=3)
        self.assertTrue(report['update_delivery']['all_delivered'])
        self.assertEqual(report['update_delivery']['delivered_steps'], [3])
        third_messages = model.inputs[2][0]
        self.assertTrue(any(m.get('role') == 'user' and m.get('content') == 'updated' for m in third_messages))
        self.assertFalse(any('SECRET GOLD' in str(m) for m in third_messages))
        self.assertEqual(report['case_outcome'], 'pending')


if __name__ == '__main__':
    unittest.main()
