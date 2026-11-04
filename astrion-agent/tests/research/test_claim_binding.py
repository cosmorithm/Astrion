"""Regression cases for claim-specific citations and independently visible evidence."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_store import ContextStore
from memory import Memory
from research.claims import ClaimLedger, assess_claims, prefer_cited_sources
from research.claim_review import review_claims
from research.claim_autoregister import register_report_claims
from evaluation.research.replay_claim_audit import replay


class ClaimBindingTests(unittest.TestCase):
    def setUp(self):
        self.memory = Memory('system', archive=ContextStore())
        self.ledger = ClaimLedger(self.memory)

    def source(self, content, start, *, doc=19, sha='a', text_sha='b', truncated=False):
        lines = content.splitlines()
        return self.memory.archive.put('document', content, {
            'document_id': doc, 'name': 'paper.pdf', 'sha256': sha * 64,
            'text_sha256': text_sha * 64 if text_sha else None, 'start_line': start,
            'end_line': start + len(lines) - 1, 'truncated': truncated})

    def register(self, statement, ref, quote):
        return self.ledger.record(statement, [{'ref': ref, 'quote': quote}])

    def test_terminal_citation_inside_statement_and_unrelated_sentence(self):
        ref = self.source('three types\nsix variants', 120)
        statement = 'Three types and six variants [D19:L120-L121]：'
        self.register(statement, ref, 'three types\nsix variants')
        self.assertTrue(assess_claims(self.memory, statement)['claims'][0]['location_valid'])
        other = Memory('system', archive=ContextStore())
        ledger = ClaimLedger(other)
        ref2 = other.archive.put('document', 'three types', {'document_id': 19, 'name': 'p',
            'sha256': 'a'*64, 'start_line': 120, 'end_line': 120, 'truncated': False})
        text = 'Three types [D19:L120]. A separate unsupported assertion.'
        ledger.record(text, [{'ref': ref2, 'quote': 'three types'}])
        self.assertFalse(assess_claims(other, text)['claims'][0]['location_valid'])

    def test_repeated_claim_cannot_borrow_other_occurrences_citation(self):
        ref = self.source('three types', 120)
        self.register('Three types.', ref, 'three types')
        item = assess_claims(self.memory, 'Three types. [D19:L120]\nThree types.')['claims'][0]
        self.assertFalse(item['location_valid'])

    def test_cited_observation_is_independent_of_selected_source(self):
        conclusion = self.source('Same finding.\nMore detail.', 760)
        abstract = self.source('Abstract says:\nSame finding.', 11)
        statement = 'The abstract says the finding [D19:L11-L12].'
        self.register(statement, conclusion, 'Same finding.')
        item = assess_claims(self.memory, statement)['claims'][0]
        self.assertTrue(item['location_valid'])
        self.assertEqual(item['sources'][0]['ref'], conclusion)  # immutable claim record
        self.assertEqual(item['matched_sources'][0]['ref'], abstract)
        self.assertEqual(item['cited_observations'][0]['ref'], abstract)

    def test_different_or_truncated_version_cannot_supply_citation(self):
        conclusion = self.source('Same finding.', 760)
        self.source('Same finding.', 11, sha='c')
        self.source('Same finding.', 11, truncated=True)
        statement = 'A finding [D19:L11].'
        self.register(statement, conclusion, 'Same finding.')
        item = assess_claims(self.memory, statement)['claims'][0]
        self.assertFalse(item['location_valid'])
        self.assertEqual(item['cited_observations'], [])

    def test_different_cited_text_is_shown_to_judge_without_rebinding(self):
        conclusion = self.source('The method worked.', 760)
        abstract = self.source('The method did not work.', 11)
        statement = 'The abstract says the method worked [D19:L11].'
        self.register(statement, conclusion, 'The method worked.')
        class Judge:
            def chat(self, messages, tools=None):
                data = json.loads(messages[1]['content'])[0]
                assert data['sources'][0]['observed_context'] == 'The method worked.'
                assert data['cited_observations'][0]['observed_context'] == 'The method did not work.'
                return {'content': json.dumps({'claims': [{'claim_id': data['claim_id'],
                    'verdict': 'unsupported', 'reason': 'The cited text is negative.'}]})}
        result = review_claims(self.memory, statement, Judge())['claims'][0]
        self.assertEqual(result['semantic_support'], 'unsupported')
        self.assertFalse(result['location_valid'])
        self.assertEqual(result['cited_observations'][0]['ref'], abstract)

    def test_unobserved_citation_cannot_be_declared_supported(self):
        conclusion = self.source('The method worked.', 760)
        statement = 'The abstract says the method worked [D19:L11].'
        self.register(statement, conclusion, 'The method worked.')
        class Judge:
            def chat(self, messages, tools=None):
                data = json.loads(messages[1]['content'])[0]
                return {'content': json.dumps({'claims': [{'claim_id': data['claim_id'],
                    'verdict': 'supported', 'reason': 'The conclusion says so.'}]})}
        result = review_claims(self.memory, statement, Judge())['claims'][0]
        self.assertEqual(result['semantic_support'], 'unknown')
        self.assertFalse(result['location_valid'])

    def test_exact_remapping_is_logged_and_old_claim_is_unchanged(self):
        conclusion = self.source('Same finding.', 760)
        abstract = self.source('Same finding.', 11)
        statement = 'The finding [D19:L11].'
        source = {'ref': conclusion, 'quote': 'Same finding.'}
        old_id = self.ledger.record(statement, [source])['claim_id']
        updated, changes = prefer_cited_sources(self.memory, statement, statement, [source])
        self.assertEqual(updated[0]['ref'], abstract)
        self.assertEqual(changes[0]['from_ref'], conclusion)
        self.assertEqual(changes[0]['to_ref'], abstract)
        self.assertEqual(self.ledger.get(old_id)['sources'][0]['ref'], conclusion)

    def test_no_fuzzy_rebinding_or_cross_scope_evidence(self):
        conclusion = self.source('Same finding.', 760)
        self.source('A similar finding.', 11)
        statement = 'The finding [D19:L11].'
        source = {'ref': conclusion, 'quote': 'Same finding.'}
        updated, changes = prefer_cited_sources(self.memory, statement, statement, [source])
        self.assertEqual(updated, [source])
        self.assertEqual(changes, [])
        other = Memory('other', archive=ContextStore())
        other.archive.put('document', 'Same finding.', {'document_id': 19, 'name': 'paper.pdf',
            'sha256': 'a'*64, 'text_sha256': 'b'*64, 'start_line': 11, 'end_line': 11, 'truncated': False})
        self.register(statement, conclusion, 'Same finding.')
        self.assertFalse(assess_claims(self.memory, statement)['claims'][0]['location_valid'])

    def test_quote_bridge_is_a_candidate_not_automatic_proof(self):
        ref = self.source('Sample was 128.', 11)
        statement = 'The sample was 128.'
        self.register(statement, ref, 'Sample was 128.')
        answer = 'The sample was 128.原文摘要写道：\n\n> “Sample was 128.” [D19:L11]'
        item = assess_claims(self.memory, answer)['claims'][0]
        self.assertFalse(item['location_valid'])
        self.assertEqual(item['occurrences'][0]['binding'], 'quote_block_candidate')
        self.assertEqual(item['cited_observations'][0]['ref'], ref)

    def test_code_sample_and_unrelated_citation_are_not_evidence(self):
        ref = self.source('Sample was 128.', 11)
        self.register('The sample was 128.', ref, 'Sample was 128.')
        for answer in ('```\nThe sample was 128. [D19:L11]\n```',
                       '~~~\nThe sample was 128. [D19:L11]\n~~~',
                       '> ```\n> The sample was 128. [D19:L11]\n> ```',
                       '- ```\n  The sample was 128. [D19:L11]\n  ```',
                       '```\nexample\n```not_a_close\nThe sample was 128. [D19:L11]\n```',
                       '    The sample was 128. [D19:L11]',
                       'The sample was 128. Another result [D19:L11]'):
            with self.subTest(answer=answer):
                self.assertFalse(assess_claims(self.memory, answer)['claims'][0]['location_valid'])

    def test_multisentence_blockquote_and_unclosed_inline_code_do_not_bind(self):
        ref = self.source('A fact.', 11)
        statement = 'First. Second'
        self.register(statement, ref, 'A fact.')
        answer = '> First. Second\n> [D19:L11]'
        self.assertFalse(assess_claims(self.memory, answer)['claims'][0]['location_valid'])
        other = Memory('system', archive=ContextStore())
        source = other.archive.put('document', 'A fact.', {'document_id': 19, 'name': 'p',
            'sha256': 'a'*64, 'text_sha256': 'b'*64, 'start_line': 11, 'end_line': 11, 'truncated': False})
        statement = 'Format is `[D19:L11]'
        ClaimLedger(other).record(statement, [{'ref': source, 'quote': 'A fact.'}])
        self.assertFalse(assess_claims(other, statement + '`')['claims'][0]['location_valid'])

    def test_known_context_window_limit_is_exposed_and_keeps_unknown(self):
        selected = self.source('A fact.', 760)
        self.source('\n'.join(f'line {i}' for i in range(1, 201)), 1)
        statement = 'A fact [D19:L100].'
        self.register(statement, selected, 'A fact.')
        class Judge:
            def chat(self, messages, tools=None):
                data = json.loads(messages[1]['content'])[0]
                assert data['cited_observations'][0]['context_window_limited']
                return {'content': json.dumps({'claims': [{'claim_id': data['claim_id'],
                    'verdict': 'supported', 'reason': 'A fact.'}]})}
        result = review_claims(self.memory, statement, Judge())['claims'][0]
        self.assertEqual(result['semantic_support'], 'unknown')

    def test_whole_citation_range_must_be_observed(self):
        ref = self.source('Sample was 128.', 11)
        statement = 'The sample was 128 [D19:L1-L100].'
        self.register(statement, ref, 'Sample was 128.')
        item = assess_claims(self.memory, statement)['claims'][0]
        self.assertFalse(item['location_valid'])
        self.assertFalse(item['occurrences'][0]['cited_observations_complete'])

    def test_adjacent_observations_cover_range_without_bridging_a_gap(self):
        first = self.source('First line.', 10)
        second = self.source('Second line.', 11)
        statement = 'A finding [D19:L10-L11].'
        self.register(statement, first, 'First line.')
        item = assess_claims(self.memory, statement)['claims'][0]
        self.assertTrue(item['location_valid'])
        self.assertEqual(set(item['cited_observations'][0]['refs']), {first, second})
        self.assertEqual(item['cited_observations'][0]['cited_excerpt'], 'First line.\nSecond line.')
        other_statement = 'Another finding [D19:L10-L12].'
        self.register(other_statement, first, 'First line.')
        self.assertFalse(assess_claims(self.memory, other_statement)['claims'][1]['location_valid'])

    def test_multisentence_claim_does_not_borrow_last_sentences_citation(self):
        ref = self.source('A fact.', 11)
        for statement in ('First.\nSecond [D19:L11]', 'First.Second [D19:L11]',
                          'First.第二条 [D19:L11]', 'First；second [D19:L11]'):
            with self.subTest(statement=statement):
                memory = Memory('system', archive=ContextStore(scope=self.memory.archive.scope))
                memory.archive.import_snapshot(self.memory.archive.snapshot())
                ClaimLedger(memory).record(statement, [{'ref': ref, 'quote': 'A fact.'}])
                self.assertFalse(assess_claims(memory, statement)['claims'][0]['location_valid'])

    def test_review_keeps_negation_outside_cited_line(self):
        conclusion = self.source('treatment worked', 760)
        self.source('There is no evidence that\ntreatment worked', 10)
        statement = 'Treatment worked [D19:L11].'
        self.register(statement, conclusion, 'treatment worked')
        class Judge:
            def chat(self, messages, tools=None):
                data = json.loads(messages[1]['content'])[0]
                context = data['cited_observations'][0]
                assert context['cited_excerpt'] == 'treatment worked'
                assert 'no evidence' in context['observed_context']
                return {'content': json.dumps({'claims': [{'claim_id': data['claim_id'],
                    'verdict': 'unsupported', 'reason': 'The surrounding sentence contains negation.'}]})}
        result = review_claims(self.memory, statement, Judge())['claims'][0]
        self.assertEqual(result['semantic_support'], 'unsupported')

    def test_review_keeps_negation_in_separate_adjacent_observation(self):
        conclusion = self.source('treatment worked', 760)
        self.source('There is no evidence that', 10)
        self.source('treatment worked', 11)
        statement = 'Treatment worked [D19:L11].'
        self.register(statement, conclusion, 'treatment worked')
        class Judge:
            def chat(self, messages, tools=None):
                data = json.loads(messages[1]['content'])[0]
                obs = data['cited_observations'][0]
                assert obs['context_start_line'] == 10
                assert obs['context_end_line'] == 11
                assert 'no evidence' in obs['observed_context']
                return {'content': json.dumps({'claims': [{'claim_id': data['claim_id'],
                    'verdict': 'unsupported', 'reason': 'The adjacent line negates the claim.'}]})}
        self.assertEqual(review_claims(self.memory, statement, Judge())['claims'][0]['semantic_support'], 'unsupported')

    def test_missing_text_hash_cannot_stitch_different_observations(self):
        first = self.source('First line.', 10, text_sha=None)
        second = self.source('Second line.', 11, text_sha=None)
        statement = 'A finding [D19:L10-L11].'
        self.ledger.record(statement, [{'ref': first, 'quote': 'First line.'},
                                       {'ref': second, 'quote': 'Second line.'}])
        self.assertFalse(assess_claims(self.memory, statement)['claims'][0]['location_valid'])

    def test_conflicting_adjacent_context_forces_unknown(self):
        conclusion = self.source('treatment worked', 760)
        self.source('No evidence that', 10)
        self.source('Clear evidence that', 10)
        self.source('treatment worked', 11)
        statement = 'Treatment worked [D19:L11].'
        self.register(statement, conclusion, 'treatment worked')
        class Judge:
            def chat(self, messages, tools=None):
                data = json.loads(messages[1]['content'])[0]
                assert data['cited_observations'][0]['context_conflict']
                return {'content': json.dumps({'claims': [{'claim_id': data['claim_id'],
                    'verdict': 'supported', 'reason': 'The citation says so.'}]})}
        self.assertEqual(review_claims(self.memory, statement, Judge())['claims'][0]['semantic_support'], 'unknown')

    def test_unobserved_section_attribution_remains_unknown(self):
        ref = self.source('The method works.', 760)
        statement = 'The abstract says the method works.'
        self.register(statement, ref, 'The method works.')
        class Judge:
            def chat(self, messages, tools=None):
                data = json.loads(messages[1]['content'])[0]
                return {'content': json.dumps({'claims': [{'claim_id': data['claim_id'],
                    'verdict': 'supported', 'reason': 'The method works.'}]})}
        self.assertEqual(review_claims(self.memory, statement, Judge())['claims'][0]['semantic_support'], 'unknown')

    def test_conflicting_archived_citation_text_stays_unavailable(self):
        conclusion = self.source('A finding.', 760)
        self.source('A finding.', 11)
        self.source('Different finding.', 11)  # conflicting observations claim same hashes
        statement = 'A finding [D19:L11].'
        self.register(statement, conclusion, 'A finding.')
        item = assess_claims(self.memory, statement)['claims'][0]
        self.assertFalse(item['location_valid'])
        self.assertEqual(item['cited_observations'], [])

    def test_same_document_id_with_two_versions_is_not_silently_merged(self):
        first = self.source('First finding.', 11)
        second = self.source('Second finding.', 11, sha='c', text_sha='d')
        statement = 'Both findings [D19:L11].'
        self.ledger.record(statement, [{'ref': first, 'quote': 'First finding.'},
                                       {'ref': second, 'quote': 'Second finding.'}])
        item = assess_claims(self.memory, statement)['claims'][0]
        self.assertFalse(item['location_valid'])
        self.assertEqual(item['cited_observations'], [])

    def test_registration_rebinds_exact_quote_and_records_provenance(self):
        conclusion = self.source('Same finding.', 760)
        abstract = self.source('Same finding.', 11)
        statement = 'The finding [D19:L11].'
        class Counter:
            def count_request(self, messages, tools): return 500
        class Judge:
            def chat(self, messages, tools=None):
                data = json.loads(messages[1]['content'])
                assert conclusion in [x['ref'] for x in data['observations']]
                return {'content': json.dumps({'claims': [{'statement': statement,
                    'sources': [{'ref': conclusion, 'quote': 'Same finding.'}]}]})}
        with patch('research.claim_autoregister.load_token_counter', return_value=Counter()):
            result = register_report_claims(self.memory, statement, Judge())
        self.assertEqual(result['source_rebindings'][0]['from_ref'], conclusion)
        self.assertEqual(result['source_rebindings'][0]['to_ref'], abstract)
        stored = self.ledger.get(result['registered_claim_ids'][0])
        self.assertEqual(stored['sources'][0]['ref'], abstract)

    def test_replay_uses_copy_and_never_overwrites_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = Memory('system', archive=ContextStore(root / 'context.sqlite3'))
            ref = memory.archive.put('document', 'Same finding.', {'document_id': 19, 'name': 'p',
                'sha256': 'a'*64, 'text_sha256': 'b'*64, 'start_line': 11, 'end_line': 11, 'truncated': False})
            statement = 'A finding [D19:L11].'
            claim = ClaimLedger(memory).record(statement, [{'ref': ref, 'quote': 'Same finding.'}])
            memory.checkpoint()
            report = root / 'report.json'
            report.write_text(json.dumps({'run': {'context_archive': {'path': memory.archive.path,
                'scope': memory.archive.scope}, 'result': {'answer': statement}}}))
            before = report.read_bytes()
            result = replay(report, root / 'replay.json')
            self.assertTrue(result['audit']['claims'][0]['location_valid'])
            self.assertEqual(report.read_bytes(), before)
            self.assertEqual(Memory.from_archive(memory.archive.path, memory.archive.scope)._claim_ids,
                             [claim['claim_id']])
            with self.assertRaises(FileExistsError):
                replay(report, root / 'replay.json')
            with self.assertRaises(ValueError):
                replay(report, report)
