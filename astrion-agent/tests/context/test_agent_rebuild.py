import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.context_counter import CodepointCounter

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
    def setUp(self):
        token_patch = patch(
            "context_construction.load_token_counter", return_value=CodepointCounter()
        )
        token_patch.start()
        self.addCleanup(token_patch.stop)

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
                    "SELECT count(*) FROM agent_context_summaries_v2 WHERE level=?",
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


    def test_failed_rebuild_event_contains_current_error_not_stale_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory("System", budget_chars=2500, keep_recent=1,
                            archive=ContextStore(Path(tmp) / "history.sqlite"))
            memory.compaction_reason = "old_success"
            memory.needs_compaction = lambda: False
            empty = {"type": "object", "properties": {}, "additionalProperties": False}
            finish = next(s for s in agent.TOOL_SPECS if s.name == "finish")
            specs = [ToolSpec("work", "produce observation", empty), finish]
            events = []
            with patch("agent.ContextConstructor.rebuild", side_effect=ValueError("protected: 3000>2000")):
                with contextlib.redirect_stdout(io.StringIO()):
                    agent.run("task", Model(), memory, max_steps=5, tool_specs=specs,
                              tool_implementations={"work": lambda: "x" * 1100,
                                                    "finish": lambda answer: answer},
                              event_sink=events.append)
            failures = [e['payload'] for e in events
                        if e['type'] == 'memory.compaction.finished' and not e['payload']['compacted']]
            self.assertTrue(failures)
            self.assertEqual(failures[0]['reason'], 'step_context_rebuild_failed')
            self.assertEqual(failures[0]['rebuild_error']['reason'], 'protected: 3000>2000')

    def test_ss_uses_token_watermark_and_reuses_constructed_context(self):
        class QuarterCounter(CodepointCounter):
            identifier = "test-quarter"
            def count_text(self, text):
                return (len(text) + 3) // 4

        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory("System", budget_chars=1200, keep_recent=4,
                archive=ContextStore(Path(tmp) / "history.sqlite"))
            empty = {"type": "object", "properties": {}, "additionalProperties": False}
            finish = next(s for s in agent.TOOL_SPECS if s.name == "finish")
            specs = [ToolSpec("work", "produce observation", empty), finish]
            events = []
            counter = QuarterCounter()
            with patch.dict(os.environ, {"ASTRION_CONTEXT_INPUT_BUDGET_TOKENS": "2400"}), \
                 patch("agent.load_token_counter", return_value=counter), \
                 patch("context_construction.load_token_counter", return_value=counter), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = agent.run("task", Model(), memory, max_steps=12,
                    tool_specs=specs, tool_implementations={"work": lambda: "x" * 1400,
                    "finish": lambda answer: answer}, event_sink=events.append)
            self.assertEqual(result["status"], "no_tool_call")
            builds = [e["step"] for e in events if e["type"] == "memory.compaction.finished"
                      and e["payload"].get("rebuild")]
            self.assertTrue(builds)
            self.assertGreater(builds[0], 2)  # character threshold crossed earlier
            self.assertTrue(any(b - a > 1 for a, b in zip(builds, builds[1:]))
                            or builds[-1] < result["steps"])
            self.assertEqual(memory.check(), [])


if __name__ == "__main__":
    unittest.main()
