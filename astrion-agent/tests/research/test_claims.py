"""Claim/source bindings are derived from an agent's archived observations."""
import json
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_store import ContextStore
from memory import Memory
from research.claims import ClaimLedger, assess_claims, validate_review
from research.claim_review import review_claims
from research.runner import run_research
from research.store import Store
from test_research import call


class ClaimLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Memory('system', budget_chars=12000,
            archive=ContextStore(Path(self.tmp.name) / 'context.sqlite'))
        self.evidence = self.memory.archive.put('document', 'first line\nSample was 128.\nlast line', {
            'document_id': 3, 'name': 'paper.pdf', 'sha256': 'a'*64, 'start_line': 5,
            'end_line': 7, 'truncated': False, 'pages': [{'page': 2, 'start_line': 5, 'end_line': 7}]})
        self.ledger = ClaimLedger(self.memory)

    def test_claim_location_is_derived_from_exact_archived_quote(self):
        result = self.ledger.record('The sample was 128.', [{'ref': self.evidence, 'quote': 'Sample was 128.'}])
        item = self.ledger.get(result['claim_id'])
        self.assertEqual(item['sources'][0]['start_line'], 6)
        self.assertEqual(item['sources'][0]['end_line'], 6)
        self.assertEqual(item['sources'][0]['pages'], [2])
        self.assertEqual(item['sources'][0]['sha256'], 'a'*64)
        self.assertIn(result['claim_id'], json.dumps(self.memory.for_model()))
        self.memory.checkpoint()
        restored = Memory.from_archive(self.memory.archive.path, self.memory.archive.scope)
        self.assertEqual(ClaimLedger(restored).get(result['claim_id']), item)

    def test_invalid_or_other_scope_reference_and_quote_fail_closed(self):
        with self.assertRaises(ValueError):
            self.ledger.record('wrong', [{'ref': self.evidence, 'quote': 'not in text'}])
        other = Memory('other', archive=ContextStore(self.memory.archive.path))
        with self.assertRaises(ValueError):
            ClaimLedger(other).record('wrong', [{'ref': self.evidence, 'quote': 'Sample was 128.'}])
        self.assertEqual(self.memory._claim_ids, [])

    def test_report_matching_and_semantics_are_distinct(self):
        result = self.ledger.record('The sample was 128.', [{'ref': self.evidence, 'quote': 'Sample was 128.'}])
        audited = assess_claims(self.memory, 'The sample was 128. [D3:L6-L6]')
        item = audited['claims'][0]
        self.assertTrue(item['in_answer'])
        self.assertTrue(item['location_valid'])
        self.assertEqual(item['semantic_support'], 'unknown')
        self.assertEqual(audited['unregistered_claim_coverage'], 'unknown')
        self.assertEqual(item['claim_id'], result['claim_id'])
        unrelated = assess_claims(self.memory, 'The sample was 128.\nAnother statement [D3:L6-L6]')
        self.assertFalse(unrelated['claims'][0]['location_valid'])
        unrelated = assess_claims(self.memory, 'The sample was 128. Another statement [D3:L6-L6]')
        self.assertFalse(unrelated['claims'][0]['location_valid'])
        quoted = assess_claims(self.memory, '> The sample was 128.  \n> [D3:L6-L6]')
        self.assertTrue(quoted['claims'][0]['location_valid'])
        unrelated = assess_claims(self.memory, '> The sample was 128.\n> Different claim [D3:L6-L6]')
        self.assertFalse(unrelated['claims'][0]['location_valid'])

    def test_review_includes_surrounding_negation_and_source_warning(self):
        evidence = self.memory.archive.put('document', 'No evidence that treatment worked.', {
            'document_id': 4, 'name': 'paper.txt', 'sha256': 'b'*64, 'start_line': 1,
            'end_line': 1, 'truncated': False, 'warnings': ['possible_extraction_error']})
        self.ledger.record('Treatment worked.', [{'ref': evidence, 'quote': 'treatment worked'}])

        class Judge:
            def chat(self, messages, tools=None):
                data = json.loads(messages[1]['content'])
                source = data[0]['sources'][0]
                assert data[0]['claim_id'] == 'C1'
                assert source['observed_context'] == 'No evidence that treatment worked.'
                assert source['warnings'] == ['possible_extraction_error']
                return {'content': json.dumps({'claims': [{'claim_id': data[0]['claim_id'],
                    'verdict': 'unsupported', 'reason': 'The source contains a negation.'}]})}
        reviewed = review_claims(self.memory, 'Treatment worked. [D4:L1-L1]', Judge())
        self.assertEqual(reviewed['claims'][0]['semantic_support'], 'unsupported')
        self.assertTrue(reviewed['claims'][0]['location_valid'])
        self.assertEqual(review_claims(self.memory, 'Unrelated answer', Judge())['review']['status'], 'not_reviewed')

    def test_truncated_review_context_cannot_be_marked_supported(self):
        evidence = self.memory.archive.put('document', 'x'*4100 + 'Sample was 128.', {
            'document_id': 4, 'name': 'paper.txt', 'sha256': 'b'*64, 'start_line': 1,
            'end_line': 1, 'truncated': False})
        self.ledger.record('The sample was 128.', [{'ref': evidence, 'quote': 'Sample was 128.'}])
        class Judge:
            def chat(self, messages, tools=None):
                data = json.loads(messages[1]['content'])
                self.assert_truncated = data[0]['sources'][0]['context_truncated']
                return {'content': json.dumps({'claims': [{'claim_id': data[0]['claim_id'],
                    'verdict': 'supported', 'reason': 'matching'}]})}
        judge = Judge()
        reviewed = review_claims(self.memory, 'The sample was 128. [D4:L1]', judge)
        self.assertTrue(judge.assert_truncated)
        self.assertEqual(reviewed['claims'][0]['semantic_support'], 'unknown')

    def test_five_layer_summary_keeps_immutable_claim_ids(self):
        result = self.ledger.record('The sample was 128.', [{'ref': self.evidence, 'quote': 'Sample was 128.'}])
        self.memory.add_user('question')
        for n in range(5):
            self.memory.add_assistant({'content': f'old {n} ' + 'x' * 2500})
        seen = []
        def summarize(transcript):
            data = json.loads(transcript)
            seen.append(data['required_evidence_ids'])
            return json.dumps({'progress': 'done', 'pending': 'answer',
                               'evidence_ids': data['required_evidence_ids']})
        self.assertTrue(self.memory.compact(summarize))
        self.assertIn(result['claim_id'], seen[0])
        self.assertIn(result['claim_id'], json.dumps(self.memory.for_model()))
        self.assertEqual(self.ledger.get(result['claim_id'])['sources'][0]['sha256'], 'a'*64)

    def test_model_verdict_allows_partial_and_unknown_but_not_fabricated_ids(self):
        good = {'claims': [{'claim_id': 'id1', 'verdict': 'partial', 'reason': 'only part'}]}
        self.assertEqual(validate_review(good, {'id1'})['id1']['verdict'], 'partial')
        with self.assertRaises(ValueError):
            validate_review({'claims': [{'claim_id': 'other', 'verdict': 'supported', 'reason': 'x'}]}, {'id1'})

    def test_research_run_registers_claim_reviews_and_continues(self):
        store = Store(Path(self.tmp.name) / 'research.sqlite3')
        self.addCleanup(store.close)
        project = store.create_project('research')
        doc = store.add_document_text(project, 'paper.txt', 'Sample was 128.\nMore detail.')

        class Model:
            model = 'fake'
            def __init__(self): self.decisions = 0
            def chat(self, messages, tools=None):
                if messages[0]['content'].startswith('Review each claim'):
                    claims = json.loads(messages[1]['content'])
                    return {'content': json.dumps({'claims': [
                        {'claim_id': x['claim_id'], 'verdict': 'supported', 'reason': '原文一致'} for x in claims]})}
                self.decisions += 1
                if self.decisions == 1:
                    return call('read_knowledge', {'document_id': doc, 'start_line': 1, 'end_line': 1}, 'read')
                if self.decisions == 2:
                    observation = next(json.loads(m['content']) for m in reversed(messages) if m['role'] == 'tool')
                    return call('record_claim', {'statement': 'The sample was 128.', 'sources': [
                        {'ref': observation['_context']['evidence_refs'][0], 'quote': 'Sample was 128.'}]}, 'claim')
                return call('finish', {'answer': f'The sample was 128. [D{doc}:L1-L1]'}, 'finish')

        with contextlib.redirect_stdout(io.StringIO()):
            run = run_research(store, project, 'question', Model(), max_steps=4, max_workers=0)
        audit = run['claim_audit']
        self.assertEqual(audit['review']['status'], 'completed')
        self.assertTrue(audit['claims'][0]['location_valid'])
        self.assertEqual(audit['claims'][0]['semantic_support'], 'supported')
        self.assertEqual(audit['unregistered_claim_coverage'], 'unknown')
        self.assertEqual(json.loads(Path(run['event_log']).with_name('result.json').read_text())['claim_audit'], audit)

        class ContinuedModel(Model):
            def chat(self, messages, tools=None):
                if messages[0]['content'].startswith('Review each claim'):
                    return super().chat(messages, tools)
                return call('finish', {'answer': f'The sample was 128. [D{doc}:L1-L1]'}, 'finish2')
        with contextlib.redirect_stdout(io.StringIO()):
            continued = run_research(store, project, 'continue', ContinuedModel(), max_steps=2,
                                     max_workers=0, continue_run_id=run['run_id'])
        self.assertEqual(continued['claim_audit']['claims'][0]['claim_id'], audit['claims'][0]['claim_id'])
        self.assertTrue(continued['claim_audit']['claims'][0]['location_valid'])

    def test_delivery_registration_is_checkpointed_and_old_toolset_is_unchanged(self):
        store = Store(Path(self.tmp.name) / 'delivery.sqlite3')
        self.addCleanup(store.close)
        project = store.create_project('research')
        doc = store.add_document_text(project, 'paper.txt', 'Sample was 128.')
        class Model:
            model = 'fake'
            def __init__(self): self.n = 0
            def chat(self, messages, tools=None):
                if messages[0]['content'].startswith('Review each claim'):
                    items = json.loads(messages[1]['content'])
                    return {'content': json.dumps({'claims': [{'claim_id': x['claim_id'],
                        'verdict': 'supported', 'reason': 'consistent'} for x in items]})}
                self.n += 1
                if self.n == 1:
                    return call('read_knowledge', {'document_id': doc, 'start_line': 1, 'end_line': 1}, 'r')
                return call('finish', {'answer': f'The sample was 128. [D{doc}:L1-L1]'}, 'f')
        def register(memory, answer, llm, **kwargs):
            ref = next(ref for ref, item in memory.archive.records.items() if item['kind'] == 'document')
            claim = ClaimLedger(memory).record('The sample was 128.', [{'ref': ref, 'quote': 'Sample was 128.'}])
            return {'registered_claim_ids': [claim['claim_id']], 'usage': {}, 'status': 'processed'}
        with contextlib.redirect_stdout(io.StringIO()), patch('research.runner.register_report_claims', side_effect=register) as mocked:
            result = run_research(store, project, 'question', Model(), max_steps=3, max_workers=0)
            self.assertEqual(mocked.call_count, 1)
            restored = Memory.from_archive(**result['context_archive'])
            self.assertEqual(restored._claim_ids, result['claim_audit']['registration']['registered_claim_ids'])
            self.assertTrue(result['claim_audit']['claims'][0]['location_valid'])
            mocked.reset_mock()
            run_research(store, project, 'question', Model(), max_steps=3, max_workers=0,
                         tool_allowlist={'read_knowledge', 'finish'})
            mocked.assert_not_called()


if __name__ == '__main__':
    unittest.main()
