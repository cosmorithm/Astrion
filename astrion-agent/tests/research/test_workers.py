"""Parallel overlap, ownership, bounds and usage; no network or simulated quality claims."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from agent import ChatLLM
from memory import Memory
from research.runner import run_research
from research.streaming import StreamingChatLLM
from research.store import Store
from research.storage.artifacts import Artifacts
from test_research import ScriptedLLM, call


class CountedModel(ScriptedLLM):
    def __init__(self, responses):
        super().__init__(responses)
        self.usage_totals = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
        self.usage_calls = self.usage_unknown = 0
        self.last_usage = None

    def chat(self, messages, tools=None):
        self.usage_calls += 1
        self.last_usage = {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}
        for k, v in self.last_usage.items(): self.usage_totals[k] += v
        return super().chat(messages, tools)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'db.sqlite3')
        self.addCleanup(self.store.close)
        self.project = self.store.create_project('Parallel')
        self.doc = self.store.add_document_text(self.project, 'data.txt', 'n=128')

    def make_model(self, *, count=5, failure=False, long_answer=False, extra=False):
        barrier = threading.Barrier(count, timeout=10)
        doc = self.doc
        children = []
        class Child(CountedModel):
            def chat(self, messages, tools=None):
                if not self.inputs:
                    self.thread = threading.get_ident()
                    barrier.wait()
                    if failure and self.role == 'worker_1': raise RuntimeError('do not leak provider details')
                return super().chat(messages, tools)
        tasks = [{'task': f'Read {i}', 'context': 'Read the sample size'} for i in range(count)]
        class Parent(CountedModel):
            def fork(self, role):
                child = Child([
                    call('read_knowledge', {'document_id': doc, 'start_line': 1, 'end_line': 1}, 'read'),
                    call('finish', {'answer': ('result ' * 800 if long_answer else 'n=128') + f' [D{doc}:L1]'}, 'finish'),
                ])
                child.role = role
                children.append(child)
                return child
        responses = [call('run_workers', {'tasks': tasks}, 'batch')]
        if extra: responses.append(call('run_worker', {'task': 'sixth', 'context': ''}, 'extra'))
        responses.append(call('finish', {'answer': f'Synthesis [D{doc}:L1]'}, 'finish'))
        return Parent(responses), children

    def run_model(self, model, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_research(self.store, self.project, 'research', model, **kwargs)

    def test_five_overlap_with_separate_connections_scopes_and_correct_usage(self):
        model, children = self.make_model(extra=True)
        result = self.run_model(model)
        self.assertEqual(len(result['workers']), 5)
        self.assertEqual(len({c.thread for c in children}), 5)  # barrier would fail for serial execution
        self.assertTrue(all(e['status'] == 'completed' for e in result['workers']))
        self.assertEqual(len({e['context_archive']['scope'] for e in result['workers']}), 5)
        self.assertEqual([e['worker_id'] for e in result['workers']], [f'worker_{i}' for i in range(1, 6)])
        # The child registration pass is separately reported and included in total usage.
        registration = [e['claim_audit']['registration']['usage'] for e in result['workers']]
        self.assertEqual(result['usage']['totals']['total_tokens'],
                         130 + sum(x['totals'].get('total_tokens', 0) for x in registration))
        self.assertEqual(result['usage']['calls'], 13 + sum(x['calls'] or 0 for x in registration))
        self.assertEqual(model.usage_calls, 3)
        starts = [e for e in result['trace'] if e['event']['type'] == 'run.started']
        self.assertEqual(len(starts), 6)
        self.assertTrue(all(e['event']['payload']['context_strategy'] == 'legacy' for e in starts))
        for child in children:
            names = {t['function']['name'] for t in child.inputs[0][1]}
            self.assertNotIn('run_workers', names); self.assertNotIn('run_worker', names)
        rejected = [e['event']['payload'] for e in result['trace'] if e['event']['type'] == 'tool.call.finished' and e['event']['payload']['name'] == 'run_worker']
        self.assertNotEqual(rejected[0]['outcome'], 'success')
        self.assertEqual(result['citation_audit']['status'], 'locations_found')
        persisted = [json.loads(line) for line in Path(result['event_log']).read_text().splitlines()]
        self.assertEqual(persisted, result['trace'])
        memory = Memory.from_archive(**result['context_archive'])
        self.assertEqual(len([r for r in memory.archive.records.values() if r['kind'] == 'worker_report']), 5)

    def test_explicit_ss_strategy_is_propagated_to_workers(self):
        model, _ = self.make_model(count=2)
        result = self.run_model(model, context_strategy='ss')
        starts = [e for e in result['trace'] if e['event']['type'] == 'run.started']
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(e['event']['payload']['context_strategy'] == 'ss' for e in starts))

    def test_one_failure_does_not_discard_siblings_and_long_reports_are_rereadable(self):
        model, _ = self.make_model(count=2, failure=True, long_answer=True)
        result = self.run_model(model)
        self.assertEqual([w['status'] for w in result['workers']], ['error', 'completed'])
        batch = next(e['event']['payload']['content'] for e in result['trace']
                     if e['event']['type'] == 'tool.call.finished' and e['event']['payload']['name'] == 'run_workers')
        summaries = json.loads(batch)['workers']
        self.assertLess(len(batch), 24000)
        self.assertTrue(summaries[1]['truncated'])
        full = Artifacts(self.store).bytes(self.project, summaries[1]['report_artifact_id']).decode()
        self.assertGreater(len(full), 2500)
        self.assertIn('L1]', full)
        self.assertNotIn('do not leak', batch)

    def test_invalid_batch_is_rejected_atomically_without_spawning(self):
        model, children = self.make_model()
        tasks = [{'task': 'read', 'context': ''}] * 6
        model.responses = iter([call('run_workers', {'tasks': tasks}, 'bad'), call('finish', {'answer': 'done'}, 'f')])
        result = self.run_model(model)
        self.assertEqual(children, [])
        self.assertEqual(result['workers'], [])

    def test_clients_fork_with_independent_counters_and_stream_roles(self):
        plain = ChatLLM('fake', 'https://example.invalid', 'test')
        self.assertIsNot(plain.fork('worker_1').usage_totals, plain.usage_totals)
        events = []
        stream = StreamingChatLLM('fake', 'https://example.invalid', 'test', events.append)
        child = stream.fork('worker_1')
        self.assertIsNot(child.usage_totals, stream.usage_totals)
        self.assertEqual(stream.stream_role, 'root')
        self.assertEqual(child.stream_role, 'worker_1')

    def test_concurrent_artifact_writes_cannot_exceed_project_quota(self):
        Artifacts(self.store)
        with self.store.db:
            self.store.db.executemany(
                'INSERT INTO artifacts(id,project_id,name,size,sha256,media_type,provenance) VALUES (?,?,?,?,?,?,?)',
                [(f'{i:032x}', self.project, 'dummy.txt', 0, '', 'text/plain', '{}') for i in range(1999)])
        barrier = threading.Barrier(2, timeout=10)
        def write_one():
            store = Store(self.store.path)
            try:
                repository = Artifacts(store)
                barrier.wait()
                try:
                    repository.put(self.project, 'new.txt', b'test')
                    return 'saved'
                except ValueError as exc:
                    return str(exc).split(':')[0]
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write_one) for _ in range(2)]
            results = [f.result() for f in futures]
        self.assertCountEqual(results, ['saved', 'project_artifact_quota'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0], 2000)
