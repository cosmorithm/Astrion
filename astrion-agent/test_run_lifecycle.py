"""用脚本化模型响应验证主循环；不访问网络或修改沙箱文件。"""

import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import agent
from events import EventRecorder
from memory import Memory


def tool_call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def tool_message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []
        self.tools = []

    def chat(self, messages, tools=None):
        self.inputs.append(copy.deepcopy(messages))
        self.tools.append(copy.deepcopy(tools))
        return next(self.replies)


class RunLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.memory = Memory("测试规则")
        capture = redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def test_finish_returns_completed_and_pairs_the_tool_result(self):
        llm = ScriptedLLM([
            tool_message(tool_call("finish-1", "finish", '{"answer":"计算完成"}')),
        ])
        recorder = EventRecorder()

        result = agent.run("测试任务", llm, self.memory, event_sink=recorder)

        self.assertEqual(result, {"status": "completed", "answer": "计算完成", "steps": 1})
        self.assertEqual(self.memory.check(), [])
        self.assertEqual(self.memory.for_model()[-1], {
            "role": "tool", "tool_call_id": "finish-1", "content": "计算完成",
        })
        events = recorder.events
        self.assertEqual([event["type"] for event in events], [
            "run.started",
            "model.call.started",
            "model.call.finished",
            "tool.call.started",
            "tool.call.finished",
            "run.finished",
        ])
        self.assertEqual([event["sequence"] for event in events], list(range(1, 7)))
        self.assertEqual(len({event["run_id"] for event in events}), 1)
        self.assertNotIn("messages", events[1]["payload"]["input"])
        self.assertEqual(events[4]["payload"]["outcome"], "success")
        self.assertEqual(events[-1]["payload"]["status"], "completed")

    def test_same_memory_can_be_used_for_another_task_after_finish(self):
        llm = ScriptedLLM([
            tool_message(tool_call("finish-1", "finish", '{"answer":"第一次完成"}')),
            {"role": "assistant", "content": "第二次直接回答"},
        ])

        first = agent.run("第一次任务", llm, self.memory)
        second = agent.run("第二次任务", llm, self.memory)

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second, {"status": "no_tool_call", "answer": "第二次直接回答", "steps": 1})
        self.assertEqual(self.memory.check(), [])
        sent_roles = [message["role"] for message in llm.inputs[1]]
        self.assertEqual(sent_roles, ["system", "user", "assistant", "tool", "user"])

    def test_no_tool_call_is_reported_separately_from_finish(self):
        llm = ScriptedLLM([{"role": "assistant", "content": "直接回答"}])

        result = agent.run("测试任务", llm, self.memory)

        self.assertEqual(result, {"status": "no_tool_call", "answer": "直接回答", "steps": 1})
        self.assertEqual(self.memory.check(), [])

    def test_step_limit_reports_no_answer_and_keeps_the_last_tool_result(self):
        llm = ScriptedLLM([
            tool_message(tool_call("calc-1", "calculator", '{"expression":"2 + 3"}')),
        ])

        result = agent.run("计算", llm, self.memory, max_steps=1)

        self.assertEqual(result, {"status": "step_limit", "answer": None, "steps": 1})
        self.assertEqual(self.memory.check(), [])
        self.assertEqual(json.loads(self.memory.for_model()[-1]["content"])["result"], 5)

    def test_zero_step_limit_does_not_call_the_model(self):
        llm = ScriptedLLM([])

        result = agent.run("测试任务", llm, self.memory, max_steps=0)

        self.assertEqual(result, {"status": "step_limit", "answer": None, "steps": 0})
        self.assertEqual(llm.inputs, [])
        self.assertEqual(self.memory.check(), [])

    def test_reserved_last_step_closes_with_existing_evidence_within_budget(self):
        llm = ScriptedLLM([
            tool_message(tool_call('calc', 'calculator', '{"expression":"2+3"}')),
            {'role': 'assistant', 'content': '结果是 5'},
        ])
        result = agent.run('计算', llm, self.memory, max_steps=2, final_step_prompt='请根据已有结果收尾')
        self.assertEqual(result, {'status': 'no_tool_call', 'answer': '结果是 5', 'steps': 2})
        self.assertEqual(llm.tools[1], [])
        self.assertIn('5', llm.inputs[1][-1]['content'])
        self.assertNotIn('请根据已有结果收尾', self.memory.system_prompt)
        self.assertEqual(self.memory.check(), [])

    def test_last_step_rejects_more_actions_and_keeps_incomplete_status(self):
        llm = ScriptedLLM([tool_message(tool_call('extra', 'calculator', '{"expression":"2+3"}'))])
        recorder = EventRecorder()
        result = agent.run('计算', llm, self.memory, max_steps=1, event_sink=recorder,
                           final_step_prompt='请收尾')
        self.assertEqual(result['status'], 'step_limit')
        self.assertIsNone(result['answer'])
        self.assertFalse(any(e['type'] == 'tool.call.started' for e in recorder.events))
        self.assertEqual(self.memory.check(), [])

    def test_failed_final_answer_does_not_extend_budget_or_mark_complete(self):
        llm = ScriptedLLM([tool_message(tool_call('empty', 'finish', '{"answer":" "}'))])
        result = agent.run('研究', llm, self.memory, max_steps=1, final_step_prompt='请收尾')
        self.assertEqual(result, {'status': 'step_limit', 'answer': None, 'steps': 1})
        self.assertEqual(len(llm.inputs), 1)

    def test_empty_final_response_keeps_step_limit(self):
        llm = ScriptedLLM([{'role': 'assistant', 'content': ' '}])
        result = agent.run('研究', llm, self.memory, max_steps=1, final_step_prompt='请收尾')
        self.assertEqual(result, {'status': 'step_limit', 'answer': None, 'steps': 1})

    def test_invalid_finish_arguments_are_returned_to_the_model_for_correction(self):
        bad_arguments = [
            "{",              # 不是合法 JSON
            "[]",             # JSON 合法，但不是对象
            [],               # arguments 本身不是 JSON 字符串
            "{}",             # 缺少 answer
            '{"answer": 1}',  # answer 不是字符串
            '{"answer": "  "}',  # answer 为空白
            '{"answer": "完成", "extra": 1}',  # 多余参数导致函数调用失败
        ]
        for bad_args in bad_arguments:
            with self.subTest(arguments=bad_args):
                memory = Memory("测试规则")
                llm = ScriptedLLM([
                    tool_message(tool_call("bad-finish", "finish", bad_args)),
                    tool_message(tool_call("good-finish", "finish", '{"answer":"已修正"}')),
                ])

                result = agent.run("测试任务", llm, memory, max_steps=2)

                self.assertEqual(result, {"status": "completed", "answer": "已修正", "steps": 2})
                self.assertEqual(memory.check(), [])
                error = llm.inputs[1][-1]
                self.assertEqual(error["role"], "tool")
                self.assertEqual(error["tool_call_id"], "bad-finish")
                self.assertTrue(error["content"].startswith("ERROR:"))

    def test_invalid_regular_tool_arguments_are_also_returned_as_an_error(self):
        llm = ScriptedLLM([
            tool_message(tool_call("bad-calc", "calculator", "[1, 2]")),
            {"role": "assistant", "content": "参数需要修正"},
        ])
        recorder = EventRecorder()

        result = agent.run("测试任务", llm, self.memory, max_steps=2,
                           event_sink=recorder)

        self.assertEqual(result, {"status": "no_tool_call", "answer": "参数需要修正", "steps": 2})
        self.assertEqual(self.memory.check(), [])
        self.assertIn("工具参数必须是 JSON 对象", llm.inputs[1][-1]["content"])
        bad_events = [event for event in recorder.events
                      if event["payload"].get("tool_call_id") == "bad-calc"]
        self.assertEqual([event["type"] for event in bad_events], ["tool.call.finished"])
        self.assertEqual(bad_events[0]["payload"]["outcome"], "rejected")
        self.assertIsNone(bad_events[0]["payload"]["duration_seconds"])

    def test_calls_after_finish_are_skipped_but_all_tool_results_are_recorded(self):
        executions = []
        recorder = EventRecorder()

        def probe(value):
            executions.append(value)
            return f"执行了 {value}"

        llm = ScriptedLLM([
            tool_message(
                tool_call("before", "probe", '{"value":"结束之前"}'),
                tool_call("finish-1", "finish", '{"answer":"完成"}'),
                tool_call("after", "probe", '{"value":"结束之后"}'),
            ),
        ])
        probe_spec = agent.ToolSpec(
            name="probe", description="测试工具",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}},
                          "required": ["value"], "additionalProperties": False},
        )
        with patch.dict(agent.TOOL_IMPL, {"probe": probe}), patch.object(
            agent, "TOOL_SPECS", [*agent.TOOL_SPECS, probe_spec]
        ):
            result = agent.run("测试任务", llm, self.memory, event_sink=recorder)

        self.assertEqual(result, {"status": "completed", "answer": "完成", "steps": 1})
        self.assertEqual(executions, ["结束之前"])
        self.assertEqual(self.memory.check(), [])
        tools = [message for message in self.memory.for_model() if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tools], ["before", "finish-1", "after"])
        self.assertIn("此工具未执行", tools[-1]["content"])
        after_events = [event for event in recorder.events
                        if event["payload"].get("tool_call_id") == "after"]
        self.assertEqual([event["type"] for event in after_events], ["tool.call.finished"])
        self.assertEqual(after_events[0]["payload"]["outcome"], "skipped")

    def test_model_failure_is_recorded_without_a_false_run_finished_event(self):
        class FailingLLM:
            def chat(self, messages):
                raise RuntimeError("模型中断")

        recorder = EventRecorder()

        with self.assertRaisesRegex(RuntimeError, "模型中断"):
            agent.run("测试任务", FailingLLM(), self.memory, event_sink=recorder)

        events = recorder.events
        self.assertEqual([event["type"] for event in events], [
            "run.started", "model.call.started", "model.call.failed",
        ])
        self.assertEqual(events[-1]["payload"]["error"], {
            "type": "RuntimeError", "message": "模型中断",
        })

    def test_event_sink_failure_stops_before_the_model_is_called(self):
        llm = ScriptedLLM([{"role": "assistant", "content": "不应调用"}])

        def broken_sink(event):
            raise RuntimeError("记录器失败")

        with self.assertRaisesRegex(RuntimeError, "记录器失败"):
            agent.run("测试任务", llm, self.memory, event_sink=broken_sink)

        self.assertEqual(llm.inputs, [])


if __name__ == "__main__":
    unittest.main()
