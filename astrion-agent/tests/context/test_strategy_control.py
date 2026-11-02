"""Verify strict strategy isolation and mid-run user updates in the real loop."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent
from context_store import ContextStore
from memory import Memory
from tests.context_counter import CodepointCounter
from tooling import ToolSpec


class Scripted:
    def __init__(self):
        self.inputs = []
    def chat(self, messages, tools=None):
        self.inputs.append(messages)
        if len(self.inputs) == 5:
            return {'content': 'done'}
        return {'content': None, 'tool_calls': [{'id': str(len(self.inputs)), 'type': 'function',
                 'function': {'name': 'work', 'arguments': '{}'}}]}
    def summarize(self, transcript):
        return json.dumps({'progress': 'done', 'pending': 'continue', 'evidence_ids': []})


class StrategyControlTests(unittest.TestCase):
    def run_case(self, strategy):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memory = Memory('system', budget_chars=2700, keep_recent=1,
                        archive=ContextStore(Path(tmp.name) / 'context.sqlite'))
        finish = next(s for s in agent.TOOL_SPECS if s.name == 'finish')
        work = ToolSpec('work', 'observe', {'type': 'object', 'properties': {}, 'additionalProperties': False})
        model, events = Scripted(), []
        with patch('agent.ContextConstructor.rebuild', side_effect=AssertionError('SS forbidden')) as ss:
            with contextlib.redirect_stdout(io.StringIO()):
                result = agent.run('initial', model, memory, max_steps=6, context_strategy=strategy,
                    scheduled_user_updates={2: 'replace original requirement'},
                    tool_specs=[work, finish], tool_implementations={'work': lambda: 'x' * 900,
                                                                    'finish': lambda answer: answer},
                    event_sink=events.append)
        return result, model, events, ss

    def test_legacy_never_calls_ss_and_records_user_update(self):
        result, model, events, ss = self.run_case('legacy')
        self.assertEqual(result['status'], 'no_tool_call')
        self.assertTrue(any(e['type'] == 'memory.compaction.started' for e in events))
        ss.assert_not_called()
        updates = [e for e in events if e['type'] == 'user.message.added']
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['step'], 2)
        self.assertTrue(any(m.get('content') == 'replace original requirement' for m in model.inputs[1]))
        self.assertFalse(any(m.get('content') == 'replace original requirement' for m in model.inputs[0]))

    def test_invalid_schedules_rejected_before_task_mutation(self):
        memory = Memory('system')
        with self.assertRaises(ValueError):
            agent.run('task', Scripted(), memory, max_steps=2, scheduled_user_updates={3: 'late'})
        self.assertEqual(len(memory), 0)

if __name__ == '__main__':
    unittest.main()
