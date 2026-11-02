import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent
from context_construction import ContextConstructor, LEVEL_TARGETS
from context_history import AgentHistory, copy_history
from context_store import ContextStore
from context_tokenizer import TokenCounterUnavailable, load_token_counter
from context_tools import context_implementations
from memory import Memory
from tests.context_counter import CodepointCounter


class StrictBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.counter = CodepointCounter()
        self.memory = Memory(
            "system",
            budget_chars=9000,
            keep_recent=1,
            archive=ContextStore(Path(self.tmp.name) / "context.sqlite"),
        )
        self.memory.add_user("Find sample count")
        self.history = AgentHistory(self.memory.archive, "strict")
        self.requests = []

    def add_steps(self, count, text_size=100):
        for n in range(1, count + 1):
            self.history.begin_step(n)
            text = f"sample {n}: " + ("x" * text_size)
            self.history.record(
                {
                    "type": "model.call.finished",
                    "event_id": f"e{n}",
                    "sequence": n,
                    "payload": {"purpose": "decision", "response": {"content": text}},
                }
            )
            self.memory.add_assistant({"content": text})
            self.history.end_step()

    def test_oversize_retried_then_degraded_and_cached_with_hard_limit(self):
        self.add_steps(1)
        step = self.history.steps()[0]
        # Old, non-contract summaries must not bypass the new bound.
        self.history.save_summary(
            step["step_id"], step["source_version"], "SS", "old " * 100
        )

        def model(purpose, messages):
            self.requests.append((purpose, messages))
            return {"content": json.dumps({"summary": "z" * 300})}

        constructor = ContextConstructor(
            self.memory, self.history, model, token_counter=self.counter
        )
        constructor.request_limit = 3000
        result = constructor.summary(step, "SS")
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(result["degraded"])
        self.assertLessEqual(
            self.counter.count_text(result["content"]), LEVEL_TARGETS["SS"]
        )
        self.assertEqual(
            constructor.summary(step, "SS")["summary_id"], result["summary_id"]
        )
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(
            self.history.step(step["step_id"])["events"][0]["payload"]["response"][
                "content"
            ][:6],
            "sample",
        )
        self.memory.agent_history = self.history
        result = json.loads(
            context_implementations(self.memory)["search_step_history"]("sample 1:")
        )
        self.assertEqual(result["items"][0]["step_id"], step["step_id"])
        self.assertTrue(result["items"][0]["matched_raw"])

    def test_retry_accepts_only_valid_short_model_summary(self):
        self.add_steps(1)

        def model(purpose, messages):
            self.requests.append((purpose, messages))
            return {
                "content": json.dumps(
                    {
                        "summary": (
                            "x" * 200 if len(self.requests) == 1 else "sample 1: ok"
                        )
                    }
                )
            }

        constructor = ContextConstructor(
            self.memory, self.history, model, token_counter=self.counter
        )
        constructor.request_limit = 3000
        summary = constructor.summary(self.history.steps()[0], "SS")
        self.assertFalse(summary["degraded"])
        self.assertEqual(summary["content"], "sample 1: ok")
        self.assertEqual(
            summary["token_count"], self.counter.count_text(summary["content"])
        )
        self.assertEqual(len(self.requests), 2)

    def test_hierarchical_directory_and_greedy_selection_stay_inside_budget(self):
        self.add_steps(25, text_size=250)

        def model(purpose, messages):
            self.requests.append((purpose, messages))
            self.assertLessEqual(self.counter.count_request(messages, []), 1600)
            data = json.loads(messages[1]["content"])
            if purpose == "context_plan_phase":
                return {
                    "content": json.dumps(
                        {"phases": [p["phase_id"] for p in data["phases"]]}
                    )
                }
            if purpose == "context_plan":
                return {
                    "content": json.dumps(
                        {
                            "selections": [
                                {"step_id": x["step_id"], "level": "RAW"}
                                for x in data["directory"]
                            ]
                        }
                    )
                }
            return {
                "content": json.dumps({"summary": "sample result " + data["step_id"]})
            }

        constructor = ContextConstructor(
            self.memory, self.history, model, token_counter=self.counter
        )
        build = constructor.rebuild("Find sample count", input_budget_tokens=1600)
        self.assertEqual(build["directory_detail"]["mode"], "hierarchical")
        self.assertLess(build["directory_detail"]["expanded_steps"], 25)
        expanded = [
            int(entry["step_id"].rsplit(":", 1)[1]) for entry in build["directory"]
        ]
        self.assertTrue(any(n <= 8 for n in expanded))
        self.assertTrue(any(n > 8 for n in expanded))
        self.assertLessEqual(build["local_input_tokens"], 1600)
        self.assertLessEqual(
            self.counter.count_request(self.memory.for_model(), []), 1600
        )
        self.assertTrue(build["selections"])
        self.assertEqual(self.memory.check(), [])
        self.assertEqual(len(self.memory.archive.messages), 26)

    def test_legacy_summary_and_collapsed_record_are_preserved(self):
        self.add_steps(3, text_size=600)
        ref = self.memory.archive.put("history_segment", "original archived material")
        self.memory._summary = "Legacy protected constraint: use only local data."
        self.memory._history[1] = {
            "role": "assistant",
            "content": "Legacy aggregated record",
            "_collapse": ref,
            "_seq": 2,
        }

        def model(purpose, messages):
            data = json.loads(messages[1]["content"])
            if purpose == "context_plan":
                return {"content": json.dumps({"selections": []})}
            return {"content": json.dumps({"summary": "short"})}

        constructor = ContextConstructor(
            self.memory, self.history, model, token_counter=self.counter
        )
        build = constructor.rebuild("Find sample count", input_budget_tokens=5000)
        serialized = json.dumps(self.memory.for_model())
        self.assertIn("Legacy protected constraint", serialized)
        self.assertIn("Legacy aggregated record", serialized)
        self.assertLessEqual(build["local_input_tokens"], build["target_tokens"])

    def test_raw_downgrades_to_cached_medium_before_ss(self):
        self.add_steps(3, text_size=2300)

        def model(purpose, messages):
            data = json.loads(messages[1]["content"])
            if purpose == "context_plan":
                return {
                    "content": json.dumps(
                        {"selections": [{"step_id": "strict:step:1", "level": "RAW"}]}
                    )
                }
            return {"content": json.dumps({"summary": "short"})}

        constructor = ContextConstructor(
            self.memory, self.history, model, token_counter=self.counter
        )
        step = self.history.steps()[0]
        self.history.save_contract_summary(
            step["step_id"],
            step["source_version"],
            "M",
            constructor.contract_id,
            "cached medium",
            token_count=len("cached medium"),
        )
        build = constructor.rebuild("Find sample count", input_budget_tokens=2200)
        self.assertEqual(build["selections"]["strict:step:1"], "M")
        self.assertEqual(build["downgraded"][0]["to"], "M")
        self.assertLessEqual(build["local_input_tokens"], build["target_tokens"])

    def test_oversize_source_is_chunked_without_losing_characters(self):
        self.add_steps(1, text_size=4000)
        records = []

        def model(purpose, messages):
            self.assertLessEqual(self.counter.count_request(messages, []), 1400)
            data = json.loads(messages[1]["content"])
            if data.get("parts", 1) > 1 and isinstance(data["record"], str):
                records.append(data["record"])
            return {"content": json.dumps({"summary": "short"})}

        constructor = ContextConstructor(
            self.memory, self.history, model, token_counter=self.counter
        )
        constructor.request_limit = 1400
        step = self.history.steps()[0]
        summary = constructor.summary(step, "SS")
        self.assertFalse(summary["degraded"])
        self.assertEqual(
            "".join(records),
            json.dumps(
                constructor._source(step),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            ),
        )
        self.assertLessEqual(summary["token_count"], 80)

    def test_strict_summaries_are_forked_but_remain_scoped(self):
        self.add_steps(1)
        step = self.history.steps()[0]
        summary = self.history.save_contract_summary(
            step["step_id"],
            step["source_version"],
            "SS",
            "test-contract",
            "short",
            token_count=5,
        )
        unrelated = AgentHistory(ContextStore(self.memory.archive.path), "other")
        self.assertIsNone(
            unrelated.get_contract_summary(
                step["step_id"], step["source_version"], "SS", "test-contract"
            )
        )
        target = ContextStore(self.memory.archive.path)
        copy_history(self.memory.archive, target)
        fork = AgentHistory(target, "continued")
        self.assertEqual(
            fork.get_contract_summary(
                step["step_id"], step["source_version"], "SS", "test-contract"
            )["summary_id"],
            summary["summary_id"],
        )
        self.assertIsNone(
            unrelated.get_contract_summary(
                step["step_id"], step["source_version"], "SS", "test-contract"
            )
        )

    def test_missing_tokenizer_fails_closed(self):
        with patch.dict(
            os.environ,
            {"ASTRION_CONTEXT_TOKENIZER_PATH": "/nonexistent/tokenizer.json"},
        ):
            with self.assertRaises(TokenCounterUnavailable):
                load_token_counter("deepseek-flash")

    def test_phase_planning_events_are_not_task_step_events(self):
        self.history.begin_step(1)
        self.history.record(
            {
                "type": "model.call.started",
                "event_id": "phase-event",
                "sequence": 1,
                "occurred_at": "2026-01-01",
                "payload": {
                    "purpose": "context_plan_phase",
                    "model_call_id": "phase-model",
                    "request_snapshot": {"messages": [], "tools": []},
                },
            }
        )
        with self.memory.archive.connect() as db:
            row = db.execute(
                "SELECT step_id FROM agent_context_events WHERE event_id=?",
                ("phase-event",),
            ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(self.history.step("strict:step:1")["events"], [])

    def test_durable_model_requires_tokenizer_before_call(self):
        class Provider:
            model = "unknown-provider"
            base_url = "https://provider.invalid"
            calls = 0

            def _post(self, payload):
                pass

            def chat(self, messages, tools=None):
                self.calls += 1

        model = Provider()
        with patch.dict(os.environ, {"ASTRION_CONTEXT_INPUT_BUDGET_TOKENS": ""}), patch(
            "agent.load_token_counter", side_effect=TokenCounterUnavailable("missing")
        ):
            with self.assertRaises(TokenCounterUnavailable):
                agent.run("question", model, self.memory, max_steps=1)
        self.assertEqual(model.calls, 0)

    def test_tokenizer_installer_pins_hash_and_never_overwrites(self):
        import hashlib, io, zipfile
        from context_tokenizer import install_deepseek_tokenizer

        content = b"fixture-tokenizer"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("deepseek_v4_tokenizer/tokenizer.json", content)
        destination = Path(self.tmp.name) / "installed.json"
        with patch(
            "urllib.request.urlopen", return_value=io.BytesIO(buffer.getvalue())
        ), patch(
            "context_tokenizer.DEEPSEEK_SHA256", hashlib.sha256(content).hexdigest()
        ):
            self.assertEqual(
                install_deepseek_tokenizer(destination).read_bytes(), content
            )
            self.assertEqual(install_deepseek_tokenizer(destination), destination)
        with self.assertRaisesRegex(ValueError, "refuse_to_overwrite"):
            install_deepseek_tokenizer(destination)
        self.assertEqual(destination.read_bytes(), content)

    def test_input_budget_stops_model_call_before_request(self):
        class Model:
            calls = 0

            def chat(self, messages, tools=None):
                self.calls += 1
                return {"content": "answer"}

        model = Model()
        events = []
        with patch.dict(
            os.environ, {"ASTRION_CONTEXT_INPUT_BUDGET_TOKENS": "100"}
        ), patch("agent.load_token_counter", return_value=self.counter):
            with self.assertRaisesRegex(
                ValueError, "context_input_token_budget_exceeded"
            ):
                agent.run(
                    "question",
                    model,
                    Memory("x" * 200),
                    max_steps=1,
                    event_sink=events.append,
                )
        self.assertEqual(model.calls, 0)
        self.assertTrue(any(e["type"] == "model.call.failed" for e in events))


if __name__ == "__main__":
    unittest.main()
