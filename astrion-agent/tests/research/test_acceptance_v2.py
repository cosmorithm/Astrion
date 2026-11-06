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

    def test_structured_flag_and_reader_are_forwarded_without_changing_default(self):
        data = dict(self.data)
        data['cases'] = [{'id': 'R01', 'task': 'task', 'paper_ids': ['P25'], 'checks': [{'id': 'R01-01'}]}]
        (self.dir / 'cases.json').write_text(json.dumps(data))
        legacy = {'task': 'task', 'paper_ids': ['P25']}
        with patch('evaluation.research.acceptance_v2.run_case.HERE', self.dir), \
             patch('evaluation.research.acceptance_v2.run_case.validate', return_value={'valid': True}), \
             patch('evaluation.research.acceptance_v2.run_case.case_by_id', return_value=legacy), \
             patch('evaluation.research.acceptance_v2.run_case.evaluate',
                   return_value={'report_path': 'original.json', 'case_outcome': 'pending'}) as evaluate:
            report = run_case('R01', object, self.root / 'out', source=self.root, base=self.db,
                              mode='multi', delegation_mode='structured', include_worker_reader=True)
        self.assertEqual(evaluate.call_args.kwargs['delegation_mode'], 'structured')
        self.assertIn('read_worker_report', evaluate.call_args.kwargs['tool_allowlist'])
        self.assertEqual(report['acceptance_v2']['delegation_mode'], 'structured')
        self.assertEqual(report['acceptance_v2']['semantic_review'], 'pending')

    def test_routing_pair_runs_both_with_same_tool_options_and_no_semantic_pass(self):
        from evaluation.research.acceptance_v2.routing_compare import compare_routing
        seen = []
        def fake_case(case_id, factory, output, **kwargs):
            seen.append(kwargs)
            return {'report_path': kwargs['delegation_mode'] + '.json', 'semantic_assessment': 'pending',
                    'settings': {'allowed_tools': ['finish', 'read_worker_report']},
                    'run': {'run_id': kwargs['delegation_mode'], 'result': {'status': 'no_tool_call', 'answer': 'done'},
                            'duration_seconds': 2.0, 'workers': []}}
        with patch('evaluation.research.acceptance_v2.routing_compare.run_case', side_effect=fake_case):
            pair = compare_routing('R01', object, self.root / 'out', source=self.root, base=self.db)
        self.assertEqual([x['delegation_mode'] for x in seen], ['baseline', 'structured'])
        self.assertTrue(all(x['include_worker_reader'] for x in seen))
        self.assertTrue(pair['same_allowed_tools'])
        self.assertEqual(pair['semantic_assessment'], 'pending')
        self.assertEqual(pair['modes']['baseline']['token_completeness'], 'unknown_or_partial')
        self.assertTrue(Path(pair['report_path']).exists())

    def test_continuation_pair_counts_workers_from_both_stages(self):
        from evaluation.research.acceptance_v2.routing_compare import _observation
        run = {'result': {'status': 'completed', 'answer': 'done'},
               'workers': [{'status': 'completed'}],
               'continuation_stages': [
                   {'workers': [{'status': 'no_tool_call'}, {'status': 'error'}],
                    'delegation_routing': {'mode': 'structured', 'status': 'dispatched'}},
                   {'workers': [{'status': 'completed'}],
                    'delegation_routing': {'mode': 'structured', 'status': 'skipped'}},
               ]}
        item = _observation({'run': run})
        self.assertEqual(item['worker_count'], 3)
        self.assertEqual(item['worker_statuses'], ['no_tool_call', 'error', 'completed'])
        self.assertEqual([x['status'] for x in item['delegation_routing']], ['dispatched', 'skipped'])

    def test_routing_pair_preserves_a_failed_side(self):
        from evaluation.research.acceptance_v2.routing_compare import compare_routing
        def fake_case(case_id, factory, output, **kwargs):
            if kwargs['delegation_mode'] == 'baseline':
                raise ValueError('potentially sensitive diagnostic')
            return {'report_path': 'structured.json', 'run': {'result': {'status': 'error'}}}
        with patch('evaluation.research.acceptance_v2.routing_compare.run_case', side_effect=fake_case):
            pair = compare_routing('R01', object, self.root / 'out', source=self.root, base=self.db)
        self.assertEqual(pair['modes']['baseline']['status'], 'experiment_error')
        self.assertEqual(pair['modes']['baseline']['error_type'], 'ValueError')
        self.assertNotIn('potentially sensitive', json.dumps(pair))
        self.assertIsNone(pair['same_allowed_tools'])

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
