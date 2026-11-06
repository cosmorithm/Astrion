"""模型流分块的离线测试，不访问网络。"""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch
from research.streaming import DeltaAssembler, ModelRequestError, StreamingChatLLM, partial_answer


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


class RetryTests(unittest.TestCase):
    """网络失败重试：只重试还没产生可见输出的请求，并保留失败原因。"""

    @staticmethod
    def response(*lines):
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def __iter__(self): return iter(lines)
        return FakeResponse()

    def model(self, emitted):
        model = StreamingChatLLM('fake', 'https://example.invalid', 'fake-model', emitted.append)
        model.retry_delays = (0.0, 0.0)
        model.sleeps = []
        model._sleep = model.sleeps.append
        return model

    def test_transient_network_failure_is_retried_and_streams_once(self):
        emitted = []
        model = self.model(emitted)
        network_error = urllib.error.URLError(OSError(8, 'nodename nor servname provided'))
        with patch('urllib.request.urlopen',
                   side_effect=[network_error, self.response(
                       b'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n',
                       b'data: [DONE]\n')]) as opened:
            message = model._post({'messages': []})
        self.assertEqual(message['content'], 'ok')
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(model.sleeps, [0.0])
        self.assertEqual(''.join(e['text'] for e in emitted if e['type'] == 'model_delta'), 'ok')
        retry = [e for e in emitted if e['type'] == 'model_retry'][0]
        self.assertEqual((retry['attempt'], retry['attempts']), (2, 3))

    def test_exhausted_retries_keep_reason_and_attempt_count(self):
        model = self.model([])
        network_error = urllib.error.URLError(OSError(8, 'nodename nor servname provided'))
        with patch('urllib.request.urlopen', side_effect=network_error) as opened:
            with self.assertRaises(ModelRequestError) as raised:
                model._post({'messages': []})
        error = raised.exception
        self.assertEqual(opened.call_count, 3)
        self.assertTrue(error.retriable)
        self.assertEqual(error.attempts, 3)
        self.assertEqual(error.code, 'model_network_error')
        self.assertIn('nodename nor servname provided', str(error))
        self.assertIn('nodename nor servname provided', error.reason)

    def test_break_after_visible_output_is_not_retried(self):
        emitted = []
        model = self.model(emitted)
        with patch('urllib.request.urlopen', return_value=self.response(
                b'data: {"choices":[{"index":0,"delta":{"content":"half"}}]}\n')) as opened:
            with self.assertRaises(ModelRequestError) as raised:
                model._post({'messages': []})
        self.assertEqual(opened.call_count, 1)
        self.assertFalse(raised.exception.retriable)
        self.assertEqual(raised.exception.code, 'model_stream_incomplete')

    def test_http_client_error_is_not_retried(self):
        model = self.model([])
        error = urllib.error.HTTPError('https://example.invalid', 400, 'Bad Request',
                                        {'Content-Type': 'application/json'},
                                        io.BytesIO(b'{"error":"Model Not Exist"}'))
        with patch('urllib.request.urlopen', side_effect=error) as opened:
            with self.assertRaises(ModelRequestError) as raised:
                model._post({'messages': []})
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(raised.exception.status, 400)
        self.assertFalse(raised.exception.retriable)
        self.assertIn('Model Not Exist', raised.exception.reason)

    def test_rate_limit_honours_retry_after(self):
        emitted = []
        model = self.model(emitted)
        limited = urllib.error.HTTPError('https://example.invalid', 429, 'Too Many Requests',
                                         {'Retry-After': '0'}, io.BytesIO(b''))
        with patch('urllib.request.urlopen', side_effect=[limited, self.response(
                b'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n',
                b'data: [DONE]\n')]) as opened:
            message = model._post({'messages': []})
        self.assertEqual(message['content'], 'ok')
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(model.sleeps, [0.0])


if __name__ == '__main__': unittest.main()
