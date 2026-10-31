"""模型流分块的离线测试，不访问网络。"""
import json
import unittest
from unittest.mock import patch
from research.streaming import DeltaAssembler, StreamingChatLLM, partial_answer


class StreamTests(unittest.TestCase):
    def test_partial_answer_waits_for_incomplete_escapes(self):
        self.assertEqual(partial_answer('{"answer":"hello\\'), 'hello')
        self.assertEqual(partial_answer('{"answer":"hello\\n世界'), 'hello\n世界')
        self.assertEqual(partial_answer('{"answer":"emoji \\ud83d'), 'emoji ')
        self.assertEqual(partial_answer('{"answer":"emoji \\ud83d\\ude03'), 'emoji 😃')
        self.assertEqual(partial_answer('{"other":"no"}'), '')

    def test_assembled_tool_call_and_finish_answer_stream(self):
        output = []
        a = DeltaAssembler(lambda kind, text: output.append((kind, text)))
        for chunk in [
            {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'call1', 'type': 'function', 'function': {'name': 'finish', 'arguments': '{"answer":"你'}}]}}]},
            {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '好\\n世'}}]}}]},
            {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '界"}'}}]}, 'finish_reason': 'tool_calls'}]},
            {'choices': [], 'usage': {'prompt_tokens': 4, 'completion_tokens': 5, 'total_tokens': 9}},
        ]: a.add(chunk)
        message = a.message()
        self.assertEqual(json.loads(message['tool_calls'][0]['function']['arguments']), {'answer': '你好\n世界'})
        self.assertEqual(''.join(text for kind, text in output if kind == 'answer'), '你好\n世界')
        self.assertEqual(a.usage['total_tokens'], 9)

    def test_two_interleaved_tool_calls_and_regular_content(self):
        output = []
        a = DeltaAssembler(lambda kind, text: output.append((kind, text)))
        a.add({'choices': [{'index': 0, 'delta': {'content': '先检索', 'tool_calls': [
            {'index': 0, 'id': 'a', 'function': {'name': 'search_knowledge', 'arguments': '{"query":"'}},
            {'index': 1, 'id': 'b', 'function': {'name': 'search_memory', 'arguments': '{"query":"'}},
        ]}}]})
        a.add({'choices': [{'index': 0, 'delta': {'tool_calls': [
            {'index': 1, 'function': {'arguments': '偏好"}'}},
            {'index': 0, 'function': {'arguments': '研究"}'}},
        ]}, 'finish_reason': 'tool_calls'}]})
        message = a.message()
        self.assertEqual(message['content'], '先检索')
        self.assertEqual([json.loads(c['function']['arguments'])['query'] for c in message['tool_calls']], ['研究', '偏好'])
        self.assertEqual(output, [('content', '先检索')])

    def test_incomplete_model_response_is_not_executable(self):
        a = DeltaAssembler(lambda *_: None)
        a.add({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'length'}]})
        with self.assertRaises(RuntimeError): a.message()

    def test_client_emits_before_response_is_finished(self):
        emitted = []
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def __iter__(self):
                yield b'data: {"choices":[{"index":0,"delta":{"content":"first"}}]}\n'
                self_assert.assertEqual(emitted[0]['text'], 'first')
                yield b'data: {"choices":[{"index":0,"delta":{"content":" second"},"finish_reason":"stop"}]}\n'
                yield b'data: [DONE]\n'
        self_assert = self
        model = StreamingChatLLM('fake', 'https://example.invalid', 'fake-model', emitted.append)
        with patch('urllib.request.urlopen', return_value=FakeResponse()):
            message = model._post({'messages': []})
        self.assertEqual(message['content'], 'first second')
        self.assertEqual(model.usage_unknown, 1)

    def test_missing_done_marker_reports_failure(self):
        class Incomplete:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def __iter__(self):
                yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n'
        model = StreamingChatLLM('fake', 'https://example.invalid', 'fake-model', lambda _: None)
        with patch('urllib.request.urlopen', return_value=Incomplete()):
            with self.assertRaisesRegex(RuntimeError, '中断'): model._post({'messages': []})

if __name__ == '__main__': unittest.main()
