"""Deleting failed run journals must not erase sources or break continuations."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from research.runs import RunLog, RunRepository, run_reference_lock
from research.store import Store


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'db.sqlite3')
        self.addCleanup(self.store.close)
        self.project = self.store.create_project('A')
        self.other = self.store.create_project('B')
        self.repo = RunRepository(self.store, self.project)

    def run_record(self, status, answer=None, parent=None, top=None):
        log = RunLog(self.store, self.project)
        row = {'run_id': log.run_id, 'project': self.store.project(self.project),
               'result': {'status': status, 'answer': answer}, 'parent_run_id': parent}
        if top:
            row['status'] = top
        log.finish(row)
        log.append({'some': 'event'})
        return log

    def test_delete_and_project_isolation(self):
        log = self.run_record('error')
        self.assertTrue(self.repo.get(log.run_id)['can_delete'])
        self.assertTrue(self.repo.listing()['runs'][0]['can_delete'])
        with self.assertRaises(ValueError):
            RunRepository(self.store, self.other).delete_failed(log.run_id)
        self.assertTrue(log.directory.exists())
        self.assertEqual(self.repo.delete_failed(log.run_id), {'deleted': True, 'run_id': log.run_id})
        self.assertFalse(log.directory.exists())

    def test_legacy_failure_without_nested_result_can_be_deleted(self):
        log = RunLog(self.store, self.project)
        log.finish({'run_id': log.run_id, 'project': self.store.project(self.project),
                    'status': 'error', 'result': None})
        self.assertTrue(self.repo.listing()['runs'][0]['can_delete'])
        self.assertTrue(self.repo.delete_failed(log.run_id)['deleted'])

    def test_only_failed_or_empty_step_limit(self):
        for status, answer in [('completed', 'ok'), ('no_tool_call', 'ok'),
                               ('incomplete_plan', 'partial'), ('step_limit', 'partial'), ('running', None)]:
            with self.subTest(status=status):
                log = self.run_record(status, answer)
                self.assertFalse(self.repo.get(log.run_id)['can_delete'])
                with self.assertRaisesRegex(ValueError, 'not_deletable'):
                    self.repo.delete_failed(log.run_id)
        log = self.run_record('step_limit')
        self.assertTrue(self.repo.delete_failed(log.run_id)['deleted'])
        conflict = self.run_record('error', top='completed')
        self.assertFalse(self.repo.get(conflict.run_id)['can_delete'])

    def test_dependency_and_active_run_cannot_be_deleted(self):
        log = self.run_record('error')
        self.run_record('completed', parent=log.run_id)
        with self.assertRaisesRegex(ValueError, 'has_children'):
            self.repo.delete_failed(log.run_id)
        active = self.run_record('error')
        with run_reference_lock(self.repo, active.run_id):
            with self.assertRaisesRegex(ValueError, 'in_use'):
                self.repo.delete_failed(active.run_id)
        self.assertTrue(self.repo.delete_failed(active.run_id)['deleted'])
        live = RunLog(self.store, self.project)
        with self.assertRaises(ValueError):
            self.repo.delete_failed(live.run_id)
        self.assertTrue(live.directory.exists())

    def test_runner_locks_parent_before_fork_and_releases_on_failure(self):
        from research.runner import run_research
        from test_research import ScriptedLLM
        log = self.run_record('error')
        entered, release = threading.Event(), threading.Event()
        failures = []

        def blocked_fork(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test timed out')
            raise ValueError('simulated fork failure')

        def run_child():
            store = Store(self.store.path)
            try:
                run_research(store, self.project, 'continue', ScriptedLLM([]), continue_run_id=log.run_id)
            except ValueError as exc:
                failures.append(str(exc))
            finally:
                store.close()

        with patch('research.runner.fork_parent', side_effect=blocked_fork):
            thread = threading.Thread(target=run_child)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(ValueError, 'in_use'):
                    self.repo.delete_failed(log.run_id)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, ['simulated fork failure'])
        self.assertTrue(self.repo.delete_failed(log.run_id)['deleted'])

    def test_unknown_file_and_symlink_are_preserved(self):
        log = self.run_record('error')
        extra = log.directory / 'personal.txt'
        extra.write_text('keep')
        with self.assertRaisesRegex(ValueError, 'unknown_files'):
            self.repo.delete_failed(log.run_id)
        self.assertEqual(extra.read_text(), 'keep')
        extra.unlink()
        target = Path(self.tmp.name) / 'important.txt'
        target.write_text('keep target')
        (log.directory / 'recovery.json').symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'unknown_files'):
            self.repo.delete_failed(log.run_id)
        self.assertEqual(target.read_text(), 'keep target')

    def test_delete_api_preserves_project_documents(self):
        from fastapi.testclient import TestClient
        from research import api
        log = self.run_record('error')
        doc = self.store.add_document_text(self.project, 'paper.txt', 'original source')
        with patch.object(api, 'DB_PATH', self.store.path), TestClient(api.app) as client:
            url = f'/api/projects/{self.project}/runs/{log.run_id}'
            self.assertTrue(client.get(url).json()['can_delete'])
            self.assertEqual(client.delete(f'/api/projects/{self.other}/runs/{log.run_id}').status_code, 400)
            self.assertTrue(client.delete(url).json()['deleted'])
            self.assertEqual(client.get(url).status_code, 400)
            self.assertEqual(client.get(f'/api/projects/{self.project}/documents').json()[0]['id'], doc)

    def test_lock_blocks_other_process(self):
        log = self.run_record('error')
        command = ('from research.store import Store; from research.runs import RunRepository; '
                   'from pathlib import Path; import sys; s=Store(Path(sys.argv[1])); '
                   'RunRepository(s,int(sys.argv[2])).delete_failed(sys.argv[3])')
        with run_reference_lock(self.repo, log.run_id):
            child = subprocess.run([sys.executable, '-c', command, str(self.store.path),
                                    str(self.project), log.run_id], capture_output=True, text=True, env=os.environ.copy())
            self.assertNotEqual(child.returncode, 0)
            self.assertIn('research_run_in_use', child.stderr)
        self.assertTrue(self.repo.get(log.run_id)['can_delete'])

    def test_sibling_with_unknown_identity_cannot_be_assumed_unrelated(self):
        log = self.run_record('error')
        bad = RunLog(self.store, self.project)
        (bad.directory / 'result.json').write_text(json.dumps({'parent_run_id': log.run_id}))
        with self.assertRaisesRegex(ValueError, 'history_unreadable'):
            self.repo.delete_failed(log.run_id)
        self.assertTrue(log.directory.exists())

    def test_explicit_other_project_record_does_not_block(self):
        log = self.run_record('error')
        foreign = RunLog(self.store, self.project)
        foreign.finish({'run_id': foreign.run_id, 'project': self.store.project(self.other),
                        'result': {'status': 'completed'}})
        self.assertTrue(self.repo.delete_failed(log.run_id)['deleted'])

    def test_malformed_sibling_blocks_delete(self):
        log = self.run_record('error')
        bad = RunLog(self.store, self.project)
        (bad.directory / 'result.json').write_text('{broken')
        with self.assertRaisesRegex(ValueError, 'history_unreadable'):
            self.repo.delete_failed(log.run_id)
        self.assertTrue(log.directory.exists())


if __name__ == '__main__':
    unittest.main()
