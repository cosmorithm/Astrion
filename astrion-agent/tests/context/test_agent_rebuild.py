import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import agent
from context_store import ContextStore
from memory import Memory
from tooling import ToolSpec


class Model:
    def __init__(self):
        self.decisions = 0
        self.context_calls = []

    def chat(self, messages, tools=None):
        system = messages[0]["content"]
        if system.startswith("You are summarizing an agent execution record"):
            data = json.loads(messages[1]["content"])
            self.context_calls.append(("summary", data["step_id"]))
            return {"content": json.dumps({"summary": "Fact from " + data["step_id"]})}
        if system.startswith("You select past agent steps"):
            self.context_calls.append(("plan", None))
            return {"content": json.dumps({"selections": []})}
        self.decisions += 1
        if self.decisions == 10:
            return {"role": "assistant", "content": "done"}
        return {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [
                {
                    "id": "call" + str(self.decisions),
                    "type": "function",
                    "function": {"name": "work", "arguments": "{}"},
                }
            ],
        }

    def summarize(self, transcript):
        raise AssertionError("legacy model compression was not expected")


class AgentRebuildTests(unittest.TestCase):
    def test_live_loop_records_steps_and_rebuilds_without_spending_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(
                "System",
                budget_chars=14000,
                keep_recent=1,
                archive=ContextStore(Path(tmp) / "history.sqlite"),
            )
            empty = {"type": "object", "properties": {}, "additionalProperties": False}
            finish = next(s for s in agent.TOOL_SPECS if s.name == "finish")
            specs = [ToolSpec("work", "produce observation", empty), finish]
            impl = {
                "work": lambda: "observation " + "x" * 1400,
                "finish": lambda answer: answer,
            }
            model = Model()
            events = []
            with contextlib.redirect_stdout(io.StringIO()):
                result = agent.run(
                    "task",
                    model,
                    memory,
                    max_steps=12,
                    tool_specs=specs,
                    tool_implementations=impl,
                    event_sink=events.append,
                )
            self.assertEqual(result["status"], "no_tool_call")
            self.assertEqual(result["steps"], 10)
            self.assertTrue(any(call[0] == "plan" for call in model.context_calls))
            self.assertEqual(len(memory.agent_history.steps()), 10)
            with memory.archive.connect() as db:
                builds = [
                    json.loads(row[0])
                    for row in db.execute("SELECT payload FROM agent_context_builds")
                ]
                summaries = db.execute(
                    "SELECT count(*) FROM agent_context_summaries WHERE level=?",
                    ("SS",),
                ).fetchone()[0]
            self.assertGreater(summaries, 0)
            self.assertTrue(
                any(build["status"] == "applied" for build in builds), builds
            )
            self.assertEqual(memory.check(), [])
            self.assertTrue(
                any(
                    e["payload"].get("purpose") == "context_plan"
                    for e in events
                    if e["type"] == "model.call.started"
                )
            )


if __name__ == "__main__":
    unittest.main()
