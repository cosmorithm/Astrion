"""Budget recovery keeps recent tool batches valid and their originals readable."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent
from tooling import ToolSpec

from context_construction import ContextConstructor
from context_history import AgentHistory
from context_store import ContextStore
from memory import Memory
from tests.context_counter import CodepointCounter


def model(purpose, messages):
    if purpose == 'context_plan':
        return {'content': json.dumps({'selections': []})}
    return {'content': json.dumps({'summary': 'A result is archived for later reading.'})}


class BudgetRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Memory('system', budget_chars=30000, keep_recent=1,
            archive=ContextStore(Path(self.tmp.name) / 'context.sqlite'))
        self.memory.add_user('Find the recorded result')
        self.history = AgentHistory(self.memory.archive, 'run')
        self.counter = CodepointCounter()
        self.history.begin_step(1)
        self.memory.add_assistant({'content': 'old history ' + 'x' * 6000})
        self.history.end_step()

    def add_batch(self, count=5, size=1800):
        self.history.begin_step(2)
        calls = [{'id': f'c{i}', 'type': 'function',
                  'function': {'name': 'read_step_history', 'arguments': '{}'}}
                 for i in range(count)]
        self.memory.add_assistant({'content': None, 'tool_calls': calls})
        for i in range(count):
            self.memory.add_tool(f'c{i}', f'result {i} ' + 'y' * size + f' final-fact-{i}')
        self.history.end_step()

    def build(self, limit):
        return ContextConstructor(self.memory, self.history, model,
            token_counter=self.counter).rebuild('Find the recorded result', input_budget_tokens=limit)

    def test_large_batch_retains_protocol_and_archived_originals(self):
        self.add_batch()
        budget = 8500
        original = list(self.memory.archive.messages)
        build = self.build(budget)
        self.assertLessEqual(build['local_input_tokens'], budget)
        self.assertGreater(build['target_tokens'], build['preferred_target_tokens'])
        self.assertGreater(build['protected_tokens_before_reduction'], build['target_tokens'])
        self.assertTrue(build['reduced_tool_results'])
        self.assertEqual(self.memory.check(), [])
        self.assertEqual(self.memory.archive.messages, original)
        recent = [m for m in self.memory._history if m['role'] == 'tool']
        self.assertEqual(len(recent), 5)
        receipts = [m for m in recent if m.get('_reduced') == 'context_budget']
        self.assertEqual(len(receipts), len(build['reduced_tool_results']))
        for receipt in receipts:
            value = json.loads(receipt['content'])
            self.assertTrue(value['preview_incomplete'])
            self.assertEqual(value['preview_chars'], len(value['preview']))
            full = json.loads(self.memory.read_evidence(value['ref'], limit=4000))
            self.assertIn('final-fact-', full['text'])
        # A further rebuild must not silently restore all large tool bodies.
        previous = {m['_seq'] for m in receipts}
        next_build = self.build(budget)
        now = {m['_seq'] for m in self.memory._history if m.get('_reduced') == 'context_budget'}
        self.assertTrue(previous <= now)
        self.assertLessEqual(next_build['local_input_tokens'], budget)
        self.assertEqual(self.memory.check(), [])
        self.memory.checkpoint()
        restored = Memory.from_archive(self.memory.archive.path, self.memory.archive.scope)
        restored_receipts = [m for m in restored._history if m.get('_reduced') == 'context_budget']
        self.assertEqual({m['_seq'] for m in restored_receipts}, now)
        self.assertIn('final-fact-', json.loads(restored.read_evidence(
            json.loads(restored_receipts[0]['content'])['ref'], limit=4000))['text'])
        self.assertEqual(restored.check(), [])

    def test_recent_unseen_results_are_kept_when_eighty_percent_is_enough(self):
        self.add_batch(count=2, size=1600)
        build = self.build(6000)
        self.assertGreater(build['protected_tokens_before_reduction'], build['preferred_target_tokens'])
        self.assertEqual(build['reduced_tool_results'], [])
        self.assertTrue(all(not m.get('_reduced') for m in self.memory._history if m['role'] == 'tool'))
        self.assertLessEqual(build['local_input_tokens'], 6000)

    def test_one_large_first_batch_recovers_and_agent_completes(self):
        class Model:
            model = 'fake'
            def __init__(self):
                self.requests = []
            def chat(self, messages, tools=None):
                self.requests.append((messages, tools))
                if len(self.requests) == 1:
                    return {'content': None, 'tool_calls': [
                        {'id': f'first-{i}', 'type': 'function',
                         'function': {'name': 'large', 'arguments': '{}'}} for i in range(5)]}
                return {'content': None, 'tool_calls': [
                    {'id': 'finish', 'type': 'function',
                     'function': {'name': 'finish', 'arguments': '{"answer":"done"}'}}]}
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory('system', budget_chars=100000, keep_recent=1,
                archive=ContextStore(Path(tmp) / 'loop.sqlite'))
            finish = next(s for s in agent.TOOL_SPECS if s.name == 'finish')
            work = ToolSpec('large', 'return evidence',
                            {'type': 'object', 'properties': {}, 'additionalProperties': False})
            fake = Model()
            events = []
            with patch.dict(os.environ, {'ASTRION_CONTEXT_INPUT_BUDGET_TOKENS': '8500'}):
                with patch('agent.load_token_counter', return_value=self.counter):
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = agent.run('task', fake, memory, max_steps=2,
                            tool_specs=[work, finish],
                            tool_implementations={'large': lambda: 'evidence ' + 'x' * 1800,
                                                  'finish': lambda answer: answer},
                            event_sink=events.append, capture_model_input=True)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(len(fake.requests), 2)  # no summary/planner model calls for a single step
            self.assertTrue(all(self.counter.count_request(msgs, tools) <= 8500
                                for msgs, tools in fake.requests))
            builds = [e['payload']['rebuild'] for e in events
                      if e['type'] == 'memory.compaction.finished' and e['payload'].get('rebuild')]
            self.assertTrue(builds)
            self.assertEqual(builds[0]['planner'], 'not_needed')
            self.assertTrue(builds[0]['reduced_tool_results'])
            self.assertEqual(memory.check(), [])

    def test_irreducible_context_fails_without_changing_view(self):
        self.memory.add_user('must preserve ' + 'q' * 7000)
        self.add_batch(count=1, size=100)
        before = self.memory.for_model()
        with self.assertRaisesRegex(ValueError, 'context_protected_content_exceeds_token_target'):
            self.build(3000)
        self.assertEqual(self.memory.for_model(), before)

if __name__ == '__main__':
    unittest.main()
