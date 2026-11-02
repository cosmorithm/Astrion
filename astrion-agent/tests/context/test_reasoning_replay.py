"""Provider-specific thinking fields survive real loops and storage without cross-provider replay."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import agent
from context_store import ContextStore
from memory import Memory
from tooling import ToolSpec
from tests.context_counter import CodepointCounter


class ReasoningReplayTests(unittest.TestCase):
    def test_provider_matching_and_snapshot_restoration(self):
        memory = Memory('system')
        memory.reasoning_origin = 'provider-a'
        memory.add_user('task')
        memory.add_assistant({'content': 'answer', 'reasoning_content': 'observed'})
        memory.add_assistant({'content': 'empty', 'reasoning_content': ''})
        memory.add_assistant({'content': 'missing'})
        restored = Memory.from_dict(memory.to_dict())
        self.assertNotIn('reasoning_content', restored.for_model()[2])
        restored.reasoning_origin = 'provider-b'
        self.assertNotIn('reasoning_content', restored.for_model()[2])
        restored.reasoning_origin = 'provider-a'
        self.assertEqual(restored.for_model()[2]['reasoning_content'], 'observed')
        self.assertEqual(restored.for_model()[3]['reasoning_content'], '')
        self.assertNotIn('reasoning_content', restored.for_model()[4])
        enabled_chars = restored.chars()
        enabled_tokens = CodepointCounter().count_request(restored.for_model(), [])
        restored.reasoning_origin = None
        self.assertGreater(enabled_chars, restored.chars())
        self.assertGreater(enabled_tokens, CodepointCounter().count_request(restored.for_model(), []))

    def test_loop_replays_observed_tool_call_and_restores_archive(self):
        class Model:
            reasoning_origin = 'deepseek:api.deepseek.com'
            def __init__(self):
                self.requests = []
            def chat(self, messages, tools=None):
                self.requests.append(messages)
                if len(self.requests) == 1:
                    return {'content': None, 'reasoning_content': 'actual provider response',
                            'tool_calls': [{'id': 'c1', 'type': 'function',
                                            'function': {'name': 'work', 'arguments': '{}'}}]}
                self.assert_replayed(messages)
                return {'content': 'done', 'reasoning_content': 'actual final response'}
            def assert_replayed(self, messages):
                previous = next(m for m in messages if m.get('tool_calls'))
                if previous.get('reasoning_content') != 'actual provider response':
                    raise AssertionError(previous)
        with tempfile.TemporaryDirectory() as tmp:
            archive = ContextStore(Path(tmp) / 'context.sqlite')
            memory = Memory('system', archive=archive)
            finish = next(s for s in agent.TOOL_SPECS if s.name == 'finish')
            work = ToolSpec('work', 'work', {'type': 'object', 'properties': {}, 'additionalProperties': False})
            model = Model()
            with contextlib.redirect_stdout(io.StringIO()):
                result = agent.run('task', model, memory, max_steps=2,
                    tool_specs=[work, finish], tool_implementations={'work': lambda: 'observation', 'finish': lambda answer: answer})
            self.assertEqual(result['status'], 'no_tool_call')
            memory.checkpoint()
            restored = Memory.from_archive(archive.path, archive.scope)
            restored.reasoning_origin = model.reasoning_origin
            messages = restored.for_model()
            self.assertEqual(next(m for m in messages if m.get('tool_calls'))['reasoning_content'], 'actual provider response')
            self.assertEqual(messages[-1]['reasoning_content'], 'actual final response')
            restored.reasoning_origin = None
            self.assertTrue(all('reasoning_content' not in m for m in restored.for_model()))

    def test_program_summaries_have_empty_field_only_when_required(self):
        memory = Memory('system')
        memory._summary = 'summary'
        memory._history = [{'role': 'assistant', 'content': 'summary data', '_collapse': 'ref'}]
        self.assertTrue(all('reasoning_content' not in m for m in memory.for_model()))
        memory.reasoning_synthetic_empty = True
        messages = memory.for_model()
        self.assertEqual(messages[1]['reasoning_content'], '')
        self.assertEqual(messages[2]['reasoning_content'], '')
        memory._history.append({'role': 'assistant', 'content': 'old model message'})
        self.assertNotIn('reasoning_content', memory.for_model()[-1])

    def test_only_official_hostname_enables_automatic_replay(self):
        self.assertIsNotNone(agent.ChatLLM('key', 'https://api.deepseek.com/v1', 'model').reasoning_origin)
        self.assertIsNone(agent.ChatLLM('key', 'https://api.deepseek.com.example.org', 'model').reasoning_origin)
        self.assertIsNone(agent.ChatLLM('key', 'https://example.org', 'model').reasoning_origin)
        self.assertIsNone(agent.ChatLLM('key', 'http://api.deepseek.com', 'model').reasoning_origin)

if __name__ == '__main__':
    unittest.main()
