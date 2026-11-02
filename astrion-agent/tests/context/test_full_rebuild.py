"""A rebuild selects across the entire agent history, within a hard budget."""
import json
from pathlib import Path
import tempfile
import unittest

from context_construction import ContextConstructor
from context_history import AgentHistory
from context_store import ContextStore
from memory import Memory
from tests.context_counter import CodepointCounter


class FullRebuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Memory("system", budget_chars=50000, keep_recent=4,
            archive=ContextStore(Path(self.tmp.name) / "history.sqlite"))
        self.memory.add_user("Find the early evidence and compare it")
        self.history = AgentHistory(self.memory.archive, "run")
        self.counter = CodepointCounter()
        self.plan = []
        self.requests = []

    def step(self, n):
        self.history.begin_step(n)
        text = f"original fact {n} " + "x" * 300
        self.history.record({"type": "model.call.finished", "event_id": f"e{n}",
            "sequence": n, "payload": {"response": {"content": text}}})
        self.memory.add_assistant({"content": text})
        self.history.end_step()

    def batch(self, n, size):
        self.history.begin_step(n)
        self.memory.add_assistant({"content": None, "tool_calls": [{"id": "call",
            "type": "function", "function": {"name": "read", "arguments": "{}"}}]})
        self.memory.add_tool("call", "observation " + "y" * size + " END-FACT")
        self.history.end_step()

    def model(self, purpose, messages):
        data = json.loads(messages[1]["content"])
        self.requests.append((purpose, data))
        if purpose == "context_plan":
            return {"content": json.dumps({"selections": self.plan})}
        return {"content": json.dumps({"summary": "indexed " + data["step_id"]})}

    def rebuild(self, budget):
        return ContextConstructor(self.memory, self.history, self.model,
            token_counter=self.counter).rebuild("Compare evidence", input_budget_tokens=budget)

    def test_whole_history_is_selectable_and_latest_feedback_stays_paired(self):
        for n in range(1, 6):
            self.step(n)
        self.batch(6, 350)
        original = list(self.memory.archive.messages)
        self.plan = [{"step_id": "run:step:1", "level": "RAW"},
                     {"step_id": "run:step:4", "level": "SS"}]
        build = self.rebuild(6000)
        plan = next(data for purpose, data in self.requests if purpose == "context_plan")
        self.assertEqual(len(plan["directory"]), 6)
        self.assertEqual(plan["already_preserved"], ["run:step:6"])
        self.assertEqual(build["selections"], {"run:step:1": "RAW", "run:step:4": "SS"})
        visible = json.dumps(self.memory.for_model())
        self.assertIn("original fact 1", visible)
        self.assertIn("indexed run:step:4", visible)
        self.assertNotIn("original fact 5", visible)
        self.assertIn("END-FACT", visible)
        self.assertEqual(self.memory.check(), [])
        self.assertEqual(original, self.memory.archive.messages)
        self.assertLessEqual(build["local_input_tokens"], 6000)

    def test_selected_history_can_use_hard_budget_above_soft_target(self):
        self.step(1)
        self.step(2)
        self.batch(3, 2200)
        self.plan = [{"step_id": "run:step:1", "level": "SS"},
                     {"step_id": "run:step:2", "level": "SS"}]
        build = self.rebuild(3800)
        self.assertIn("run:step:1", build["selections"])
        self.assertIn("run:step:2", build["selections"])
        self.assertGreater(build["local_input_tokens"], build["preferred_target_tokens"])
        self.assertLessEqual(build["local_input_tokens"], 3800)
        self.assertEqual(build["reduced_tool_results"], [])

    def test_selected_history_makes_room_by_externalizing_active_observation(self):
        self.step(1)
        self.batch(2, 2500)
        self.plan = [{"step_id": "run:step:1", "level": "SS"}]
        before = self.counter.count_request(self.memory.for_model(), [])
        budget = before - 200  # removing old raw makes active fit, but not selected SS
        build = self.rebuild(budget)
        self.assertIn("run:step:1", build["selections"])
        self.assertTrue(build["reduced_tool_results"])
        result = next(m for m in self.memory._history if m["role"] == "tool")
        receipt = json.loads(result["content"])
        self.assertGreaterEqual(receipt["preview_chars"], 240)
        self.assertIn("END-FACT", self.memory.archive.get(receipt["ref"])["content"])
        self.assertEqual(self.memory.check(), [])
        self.assertLessEqual(build["local_input_tokens"], budget)

    def test_low_priority_history_is_omitted_if_feedback_floor_cannot_fit(self):
        self.memory.system_prompt = "system " + "z" * 1100
        self.step(1)
        self.batch(2, 280)
        visible = self.memory.for_model()
        active_budget = self.counter.count_request([visible[0], visible[1], *visible[-2:]], [])
        self.plan = [{"step_id": "run:step:1", "level": "SS"}]
        build = self.rebuild(active_budget + 5)
        self.assertNotIn("run:step:1", build["selections"])
        self.assertEqual(build["omitted"][0]["step_id"], "run:step:1")
        self.assertEqual(build["reduced_tool_results"], [])
        self.assertIn("END-FACT", json.dumps(self.memory.for_model()))
        self.assertEqual(self.memory.check(), [])
        self.assertLessEqual(build["local_input_tokens"], active_budget + 5)

    def test_failed_empty_step_does_not_hide_unprocessed_tool_feedback(self):
        self.step(1)
        self.batch(2, 350)
        self.history.begin_step(3)
        self.history.record({"type": "model.call.failed", "event_id": "failed",
            "sequence": 3, "payload": {"error": {"type": "TimeoutError"}}})
        self.history.end_step("failed")
        self.plan = [{"step_id": "run:step:1", "level": "SS"}]
        build = self.rebuild(6000)
        self.assertEqual(build["protected_active_steps"], ["run:step:2"])
        self.assertIn("END-FACT", json.dumps(self.memory.for_model()))
        self.assertEqual(self.memory.check(), [])


if __name__ == "__main__":
    unittest.main()
