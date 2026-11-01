"""Replay of leaked tool syntax; only native tool_calls may invoke tools."""

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

import agent
from memory import Memory
from response_contract import answer_error, finalization_messages, AnswerStream
from research.streaming import DeltaAssembler
from research.runner import run_research
from research.store import Store
from test_research import ScriptedLLM, call

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/regressions/protocol_as_report.json").read_text()
)
RAW = FIXTURE["response"]["content"]


class ResponseContractTests(unittest.TestCase):
    def test_actual_failure_is_error_not_answer_and_never_executes(self):
        for final_prompt in (None, "最后一轮，仅回答"):
            with self.subTest(final_prompt=final_prompt), contextlib.redirect_stdout(io.StringIO()):
                memory = Memory("test")
                events = []
                result = agent.run(
                    "写综述",
                    ScriptedLLM([FIXTURE["response"]]),
                    memory,
                    max_steps=1,
                    final_step_prompt=final_prompt,
                    event_sink=events.append,
                )
                self.assertEqual(result["status"], "error")
                self.assertIsNone(result["answer"])
                self.assertEqual(result["error"]["code"], FIXTURE["expected_error"])
                self.assertFalse(any(e["type"] == "tool.call.started" for e in events))
                self.assertEqual(events[-1]["payload"], result)
                self.assertEqual(memory.for_model()[-1]["content"], RAW)
                self.assertEqual(memory.check(), [])

    def test_failed_research_does_not_publish_a_markdown_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db")
            self.addCleanup(store.close)
            project = store.create_project("failure")
            with contextlib.redirect_stdout(io.StringIO()):
                result = run_research(
                    store, project, "综述", ScriptedLLM([FIXTURE["response"]]), max_steps=1
                )
            self.assertNotIn("report_artifact", result)
            self.assertEqual(store.db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 0)
            saved = json.loads(Path(result["event_log"]).with_name("result.json").read_text())
            self.assertEqual(saved["result"]["status"], "error")
            self.assertIn("DSML", Path(result["event_log"]).read_text())

    def test_finish_cannot_publish_protocol_and_can_correct_within_budget(self):
        llm = ScriptedLLM(
            [
                call("finish", {"answer": RAW}, "bad"),
                call("finish", {"answer": "资料不足，暂不能作出结论。"}, "good"),
            ]
        )
        memory = Memory("test")
        with contextlib.redirect_stdout(io.StringIO()):
            result = agent.run("综述", llm, memory, max_steps=2)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("DSML", result["answer"])
        self.assertIn("tool_protocol_in_answer", llm.inputs[1][0][-1]["content"])
        self.assertEqual(memory.check(), [])

    def test_normal_prose_and_quoted_protocol_examples_are_allowed(self):
        for text in [
            "使用 read_evidence 读取证据。",
            "讨论 DSML 的协议。",
            "示例：\n```xml\n" + RAW + "\n```",
            "`<｜｜DSML｜｜ calls>` 是标记",
        ]:
            self.assertIsNone(answer_error(text))
        self.assertIsNotNone(answer_error("继续查阅\n" + RAW))
        self.assertIsNotNone(answer_error(RAW.replace("｜", "|")))

    def test_question_terminal_cannot_deliver_protocol_as_an_answer(self):
        events = []
        with contextlib.redirect_stdout(io.StringIO()):
            result = agent.run(
                "综述",
                ScriptedLLM([call("ask_user", {"question": RAW}, "ask")]),
                Memory("test"),
                max_steps=1,
                event_sink=events.append,
            )
        self.assertEqual(result["status"], "error")
        self.assertEqual(events[-1]["payload"], result)
        self.assertIsNone(result["answer"])

    def test_final_request_has_no_native_calls_and_does_not_mutate_journal(self):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "任务原话"},
            call("read_file", {"path": "data.txt"}, "a"),
            {"role": "tool", "tool_call_id": "a", "content": "数字=128"},
            {"role": "user", "content": "补充原话"},
        ]
        before = copy.deepcopy(messages)
        view = finalization_messages(messages, "收尾")
        self.assertEqual(messages, before)
        self.assertEqual([m["content"] for m in view[1:3]], ["任务原话", "补充原话"])
        self.assertFalse(any(m.get("tool_calls") or m["role"] == "tool" for m in view))
        self.assertIn("数字=128", view[-1]["content"])
        self.assertIn("收尾", view[0]["content"])

    def test_every_protocol_split_is_hidden_from_stream_but_raw_is_preserved(self):
        for position in range(len("<｜｜DSML｜｜") + 2):
            with self.subTest(position=position):
                sent = []
                assembler = DeltaAssembler(lambda kind, text: sent.append(text))
                for text in (RAW[:position], RAW[position:]):
                    assembler.add({"choices": [{"delta": {"content": text}}]})
                assembler.add({"choices": [{"delta": {}, "finish_reason": "stop"}]})
                self.assertEqual(assembler.message()["content"], RAW)
                self.assertNotIn("DSML", "".join(sent))
                self.assertNotIn("ctx_fixture", "".join(sent))

    def test_normal_stream_retains_prefix_like_text_and_continues_streaming(self):
        output = []
        stream = AnswerStream(output.append)
        stream.add("第一段")
        self.assertEqual(output, ["第一段"])
        for text in ["x <", " y", " <||D", "ordinary", "<"]:
            stream.add(text)
        stream.finish()
        self.assertEqual("".join(output), "第一段x < y <||Dordinary<")

    def test_finish_argument_stream_does_not_leak_protocol(self):
        output = []
        assembler = DeltaAssembler(lambda kind, text: output.append((kind, text)))
        arguments = json.dumps({"answer": RAW}, ensure_ascii=False)
        for index, char in enumerate(arguments):
            function = {"arguments": char}
            if index == 0:
                function["name"] = "finish"
            assembler.add(
                {
                    "choices": [
                        {"delta": {"tool_calls": [{"index": 0, "id": "bad", "function": function}]}}
                    ]
                }
            )
        assembler.add({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        self.assertEqual(
            json.loads(assembler.message()["tool_calls"][0]["function"]["arguments"])["answer"], RAW
        )
        self.assertNotIn("DSML", "".join(text for _, text in output))


if __name__ == "__main__":
    unittest.main()
