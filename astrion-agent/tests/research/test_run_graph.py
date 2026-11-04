"""Version graph reflects explicit parent edges, never invents dependencies."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from context_store import ContextStore
from memory import Memory
from research import api
from research.run_graph import version_graph
from research.runs import RunRepository
from research.store import Store


def run_id(n):
    return 'research_' + format(n, '032x')


class VersionGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'research.sqlite3'
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.project = self.store.create_project('A')
        self.other = self.store.create_project('B')
        self.repo = RunRepository(self.store, self.project)

    def put(self, n, parent=None, *, project=None, **kwargs):
        project = self.project if project is None else project
        path = self.db.parent / 'runs' / str(project) / run_id(n)
        path.mkdir(parents=True, exist_ok=True)
        row = {'run_id':run_id(n), 'parent_run_id':parent,
               'project':self.store.project(project), 'database_path':str(self.db.resolve()),
               'task':f'task {n}', 'recorded_at':f'2026-11-{n:04}',
               'result':{'status':'completed', 'answer':f'report {n}'},
               'context_archive':{'path':'not-displayed'}}
        row.update(kwargs)
        (path / 'result.json').write_text(json.dumps(row))
        return run_id(n)

    def test_branches_have_only_explicit_parent_edges_and_isolation(self):
        root = self.put(1)
        a = self.put(2, root)
        b = self.put(3, root)
        self.put(4, project=self.other)
        graph = version_graph(self.repo)
        self.assertEqual(graph['total'], 3)
        self.assertEqual({(e['parent_run_id'], e['run_id']) for e in graph['edges']}, {(root,a),(root,b)})
        self.assertFalse(graph['missing_parents'])
        self.assertFalse(graph['cycles'])
        self.assertTrue(all(n['can_continue'] is False and n['has_report'] for n in graph['nodes']))
        self.assertTrue(all(n['continuation_status'] == 'unavailable' for n in graph['nodes']))
        self.assertNotIn('context_archive', graph['nodes'][0])

    def test_limit_retrieves_beyond_fifty_and_classifies_invisible_parent(self):
        root = self.put(1)
        for n in range(2, 62): self.put(n, root)
        graph = version_graph(self.repo, 60)
        self.assertEqual((graph['total'],len(graph['nodes']),graph['truncated']), (61,60,True))
        self.assertEqual(len(graph['edges']),0)
        self.assertTrue(all(x['reason']=='outside_limit' for x in graph['missing_parents']))
        self.assertEqual(len(version_graph(self.repo, 500)['edges']), 60)
        for invalid in (0, 501, True):
            with self.assertRaises(ValueError): version_graph(self.repo, invalid)

    def test_missing_invalid_cycle_and_legacy(self):
        a,b,c=run_id(1),run_id(2),run_id(3)
        self.put(1,b); self.put(2,a); self.put(3,c)
        self.put(4,run_id(999)); self.put(5,'../invalid'); self.put(7,123)
        self.put(6,None, recorded_at=None, result={}, context_archive=None)
        graph = version_graph(self.repo)
        self.assertEqual(graph['cycles'], [[a,b],[c]])
        self.assertEqual(graph['edges'], [])
        self.assertEqual({item['reason'] for item in graph['missing_parents']},{'not_found','invalid_parent'})
        self.assertTrue(next(node for node in graph['nodes'] if node['run_id']==run_id(7))['parent_invalid'])
        legacy = next(node for node in graph['nodes'] if node['run_id']==run_id(6))
        self.assertFalse(legacy['has_report']); self.assertFalse(legacy['can_continue'])

    def test_unreadable_or_foreign_run_is_counted_without_being_exposed(self):
        self.put(1)
        self.put(2, database_path='/another/project.sqlite3')
        path = self.db.parent / 'runs' / str(self.project) / run_id(3)
        path.mkdir(parents=True)
        (path / 'result.json').write_text('{broken')
        graph = version_graph(self.repo)
        self.assertEqual(graph['total'], 1)
        self.assertEqual(graph['skipped_records'], 2)
        self.assertEqual([node['run_id'] for node in graph['nodes']], [run_id(1)])

    def test_checkpoint_presence_is_not_claimed_as_verified_restoration(self):
        context = ContextStore(self.db.parent / 'context.sqlite3')
        memory = Memory('system', archive=context)
        memory.checkpoint()
        good = self.put(1, context_archive={'path': context.path, 'scope': context.scope})
        missing = self.put(2, context_archive={'path': context.path, 'scope': 'f'*32})
        wrong_path = self.put(3, context_archive={'path': str(self.db), 'scope': context.scope})
        nodes = {n['run_id']:n for n in version_graph(self.repo)['nodes']}
        self.assertIsNone(nodes[good]['can_continue'])
        self.assertEqual(nodes[good]['continuation_status'], 'checkpoint_present')
        self.assertFalse(nodes[missing]['can_continue'])
        self.assertFalse(nodes[wrong_path]['can_continue'])

    def test_real_http_route_and_project_check(self):
        self.put(1)
        with patch.object(api.app.state, 'store_factory', lambda: Store(self.db)), TestClient(api.app) as client:
            url=f'/api/projects/{self.project}/runs/graph'
            response=client.get(url)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['nodes'][0]['run_id'],run_id(1))
            self.assertEqual(client.get(url,params={'limit':501}).status_code,400)
            self.assertEqual(client.get('/api/projects/999999/runs/graph').status_code,400)
            self.assertEqual(client.get(f'/api/projects/{self.other}/runs/graph').json()['nodes'],[])


if __name__=='__main__': unittest.main()
