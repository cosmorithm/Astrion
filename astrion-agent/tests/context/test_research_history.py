import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from research import api
from research.runner import run_research
from research.store import Store
from test_research import ScriptedLLM, call


class ResearchHistoryTests(unittest.TestCase):
    def test_root_worker_persistence_api_and_project_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db"
            store = Store(db)
            project = store.create_project("project")
            other = store.create_project("other")
            model = ScriptedLLM(
                [
                    call(
                        "run_worker",
                        {"task": "check", "context": "only this"},
                        "delegate",
                    ),
                    {"role": "assistant", "content": "child result"},
                    {"role": "assistant", "content": "parent result"},
                ]
            )
            with contextlib.redirect_stdout(io.StringIO()):
                run = run_research(
                    store, project, "question", model, max_steps=3, worker_steps=2
                )
            store.close()
            with patch.object(
                api.app.state, "store_factory", lambda: Store(db)
            ), TestClient(api.app) as client:
                prefix = f'/api/projects/{project}/runs/{run["run_id"]}'
                response = client.get(prefix + "/agents")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    {x["agent_id"] for x in response.json()["agents"]},
                    {"root", "worker_1"},
                )
                root = client.get(prefix + "/agents/root/steps").json()["items"]
                worker = client.get(prefix + "/agents/worker_1/steps").json()["items"]
                self.assertEqual(len(root), 2)
                self.assertEqual(len(worker), 1)
                self.assertTrue(all(item["ss"] is None for item in root + worker))
                root_events = client.get(
                    prefix + "/agents/root/steps",
                    params={"step_id": root[0]["step_id"]},
                ).json()["items"]
                start = next(
                    e for e in root_events if e["type"] == "model.call.started"
                )
                self.assertEqual(
                    start["payload"]["request_snapshot"]["messages"], model.inputs[0][0]
                )
                denied = client.get(
                    prefix + "/agents/worker_1/steps",
                    params={"step_id": root[0]["step_id"]},
                )
                self.assertEqual(denied.status_code, 400)
                denied_project = client.get(
                    f'/api/projects/{other}/runs/{run["run_id"]}/agents'
                )
                self.assertEqual(denied_project.status_code, 400)

    def test_continuation_forks_parent_history_and_keeps_worker_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db")
            self.addCleanup(store.close)
            project = store.create_project("project")
            model = ScriptedLLM(
                [
                    call("run_worker", {"task": "check", "context": ""}, "delegate"),
                    {"role": "assistant", "content": "child result"},
                    {"role": "assistant", "content": "parent result"},
                    {"role": "assistant", "content": "continued"},
                ]
            )
            with contextlib.redirect_stdout(io.StringIO()):
                old = run_research(
                    store, project, "question", model, max_steps=3, worker_steps=2
                )
                new = run_research(
                    store, project, "follow up", model, continue_run_id=old["run_id"]
                )
            import sqlite3
            from contextlib import closing

            with closing(sqlite3.connect(Path(tmp) / "context.sqlite3")) as conn:

                def steps(scope):
                    return {
                        r[0]
                        for r in conn.execute(
                            "SELECT step_id FROM agent_context_steps WHERE scope=?",
                            (scope,),
                        )
                    }

                old_root = steps(old["context_archive"]["scope"])
                child = steps(old["workers"][0]["context_archive"]["scope"])
                new_root = steps(new["context_archive"]["scope"])
                self.assertTrue(old_root < new_root)
                self.assertFalse(child & new_root)
                self.assertFalse(child & old_root)


if __name__ == "__main__":
    unittest.main()
