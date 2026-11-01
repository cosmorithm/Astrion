import json
import tempfile
import unittest
from pathlib import Path

from context_construction import ContextConstructor
from context_history import AgentHistory, copy_history
from context_store import ContextStore
from context_tools import context_implementations
from memory import Memory


class StepHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "context.sqlite3"
        self.memory = Memory(
            "System", budget_chars=6000, keep_recent=1, archive=ContextStore(self.path)
        )
        self.memory.add_user("Find the result")
        self.history = AgentHistory(self.memory.archive, "run1", {"agent_id": "root"})
        self.memory.agent_history = self.history
        self.counter = 0
        self.calls = []
        self.selection = []

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, typ, payload):
        self.counter += 1
        self.history.record(
            {
                "type": typ,
                "event_id": "e" + str(self.counter),
                "sequence": self.counter,
                "occurred_at": "2026-01-01",
                "payload": payload,
            }
        )

    def step(self, n):
        self.history.begin_step(n)
        self.event(
            "model.call.started",
            {
                "model_call_id": f"call{n}",
                "purpose": "decision",
                "request_snapshot": {"messages": self.memory.for_model(), "tools": []},
            },
        )
        text = f"fact-{n} " + "x" * 1800
        self.event(
            "model.call.finished",
            {"purpose": "decision", "response": {"content": text}},
        )
        self.memory.add_assistant({"content": text})
        self.history.end_step()

    def model(self, purpose, messages):
        self.calls.append((purpose, messages))
        data = json.loads(messages[1]["content"])
        if purpose == "context_plan":
            return {"content": json.dumps({"selections": self.selection})}
        return {"content": json.dumps({"summary": "Summary of " + data["step_id"]})}

    def test_trigger_catalog_cache_rebuild_and_restore_omitted_step(self):
        for n in range(1, 4):
            self.step(n)
        self.assertIsNone(
            self.history.get_summary(
                "run1:step:1", self.history.steps()[0]["source_version"], "SS"
            )
        )
        constructor = ContextConstructor(self.memory, self.history, self.model)
        build = constructor.rebuild("Find the result")
        self.assertEqual(build["status"], "applied")
        self.assertEqual([c[0] for c in self.calls].count("context_ss"), 3)
        self.assertNotIn("fact-1", json.dumps(self.memory.for_model()))
        self.assertEqual(self.memory.check(), [])
        self.step(4)
        self.selection = [{"step_id": "run1:step:1", "level": "SS"}]
        constructor.rebuild("Find the result")
        self.assertEqual([c[0] for c in self.calls].count("context_ss"), 4)
        self.assertIn("Summary of run1:step:1", json.dumps(self.memory.for_model()))
        self.assertEqual(len(self.memory.archive.messages), 5)

    def test_longer_summary_is_generated_only_when_selected(self):
        for n in range(1, 4):
            self.step(n)
        self.selection = [{"step_id": "run1:step:1", "level": "M"}]
        ContextConstructor(self.memory, self.history, self.model).rebuild("task")
        self.assertEqual(
            [purpose for purpose, _ in self.calls].count("context_summary"), 1
        )
        step = self.history.steps()[0]
        self.assertIsNotNone(
            self.history.get_summary(step["step_id"], step["source_version"], "M")
        )
        self.assertIsNone(
            self.history.get_summary(step["step_id"], step["source_version"], "L")
        )

    def test_token_estimate_budget_refuses_oversize_before_model_call(self):
        for n in range(1, 4):
            self.step(n)
        before = self.memory.for_model()
        with self.assertRaisesRegex(
            ValueError, "context_protected_content_exceeds_token_target"
        ):
            ContextConstructor(self.memory, self.history, self.model).rebuild(
                "task", input_budget_tokens=100
            )
        self.assertEqual(before, self.memory.for_model())
        self.assertEqual(self.calls, [])

    def test_invalid_plan_never_replaces_current_context(self):
        for n in range(1, 4):
            self.step(n)
        before = self.memory.for_model()
        self.selection = [{"step_id": "other-agent-step", "level": "RAW"}]
        with self.assertRaisesRegex(ValueError, "context_invalid_plan_reference"):
            ContextConstructor(self.memory, self.history, self.model).rebuild("task")
        self.assertEqual(before, self.memory.for_model())

    def test_isolation_snapshots_raw_pagination_and_fork(self):
        self.step(1)
        other = AgentHistory(ContextStore(self.path), "run2", {"agent_id": "worker"})
        with self.assertRaisesRegex(ValueError, "step_not_in_agent_context"):
            other.step("run1:step:1")
        with self.memory.archive.connect() as db:
            snapshot = json.loads(
                db.execute(
                    "SELECT snapshot FROM agent_context_snapshots WHERE scope=?",
                    (self.memory.archive.scope,),
                ).fetchone()[0]
            )
        self.assertEqual(snapshot["messages"][1]["content"], "Find the result")
        tools = context_implementations(self.memory)
        page = json.loads(tools["read_step_history"]("run1:step:1", limit=100))
        self.assertEqual(page["next_offset"], 100)
        self.assertNotIn("request_snapshot", page["content"])
        target = ContextStore(self.path)
        copy_history(self.memory.archive, target)
        cloned = AgentHistory(target, "run3", {"agent_id": "continued-root"})
        self.assertEqual(cloned.step("run1:step:1")["status"], "completed")
        with self.assertRaisesRegex(ValueError, "step_not_in_agent_context"):
            other.step("run1:step:1")

    def test_failed_step_is_preserved_without_summary(self):
        self.history.begin_step(1)
        self.event(
            "model.call.failed",
            {"purpose": "decision", "error": {"type": "RuntimeError"}},
        )
        self.assertEqual(self.history.step("run1:step:1")["status"], "failed")
        self.assertIsNone(
            self.history.get_summary(
                "run1:step:1", self.history.steps()[0]["source_version"], "SS"
            )
        )


if __name__ == "__main__":
    unittest.main()
