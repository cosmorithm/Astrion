"""压缩信息丢失的回归测试：确定性摘要，真实 Memory/Agent Loop，无网络。"""
import copy
import io
import json
import unittest
from contextlib import redirect_stdout

import agent
from memory import Memory
from memory_evidence import extract_tool_evidence


def summary(transcript):
    return json.dumps({"progress": "已读资料，结果见保留证据。", "pending": "仍需整理回答。",
                       "evidence_ids": json.loads(transcript)["required_evidence_ids"]}, ensure_ascii=False)


def call(call_id, name, **args):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {
            "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


def document(excerpt="样本量 n=128；准确率 81%；结论：方法 A 更好。", **updates):
    return {"document_id": 1, "name": "paper.txt", "source": "上传", "sha256": "a" * 64,
            "start_line": 51, "end_line": 60, "excerpt": excerpt, "truncated": False, **updates}


def observe(memory, call_id, name, data):
    content = json.dumps(data, ensure_ascii=False)
    # 长过程文本给压缩留下空间；结论/数字在真实工具结果中。
    message = call(call_id, name)
    message["content"] = "继续检查资料，尚未给出最终回答。" * 50
    memory.add_assistant(message)
    memory.add_tool(call_id, content, evidence=extract_tool_evidence(name, content))


class MemoryTests(unittest.TestCase):
    def memory(self):
        memory = Memory("研究规则", budget_chars=1000, keep_recent=1)
        memory.add_user("只用项目 A 的资料，回答必须注明样本量 n=128。")
        observe(memory, "read", "read_knowledge", {"results": [document()]})
        ref = memory._history[-1]['_evidence_refs'][0]
        memory.pin_evidence(ref, document()['excerpt'])
        memory.add_assistant({"content": "最近正在整理答案。"})
        return memory

    def test_original_task_and_corrections_survive_repeated_compaction(self):
        memory = self.memory()
        memory.add_user("补充要求：保留准确率的小数位；只写中文。")
        original_users = [m for m in memory.for_model() if m["role"] == "user"]
        self.assertTrue(memory.compact(summary))
        observe(memory, "calc", "calculator", {"expression": "81 / 100", "result": 0.81})
        ref = memory._history[-1]['_evidence_refs'][0]
        memory.pin_evidence(ref, memory.archive.get(ref)['content'])
        memory.add_assistant({"content": "继续整理。"})
        self.assertTrue(memory.compact(summary))
        self.assertEqual([m for m in memory.for_model() if m["role"] == "user"], original_users)
        self.assertEqual(memory.last_user(), original_users[-1]["content"])
        text = json.dumps(memory.for_model(), ensure_ascii=False)
        for exact in ("样本量 n=128", "准确率 81%", "方法 A 更好", '"result": 0.81'):
            self.assertIn(exact, text.replace('\\"', '"'))

    def test_evidence_retains_exact_text_and_source_without_becoming_system(self):
        memory = self.memory()
        self.assertTrue(memory.compact(summary))
        record = next(r for r in memory.archive.records.values() if r['kind'] == 'document')
        self.assertEqual(record['content'], document()['excerpt'])
        self.assertEqual(record['metadata'], {k: v for k, v in document().items() if k != 'excerpt'})
        messages = memory.for_model()
        self.assertEqual(messages[0], {"role": "system", "content": "研究规则"})
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertIn("不是新指令", messages[1]["content"])
        self.assertFalse(any(k.startswith('_') for message in messages for k in message))
        self.assertEqual(memory.check(), [])

    def test_empty_malformed_or_missing_evidence_summary_does_not_delete_history(self):
        cases = [
            ("", "empty_summary"), (None, "empty_summary"), ("   ", "empty_summary"),
            ("只记得背景", "invalid_summary_json"), ("[]", "incomplete_summary"),
            ('{"progress":"读过了","evidence_ids":["E1"]}', "incomplete_summary"),
            ('{"progress":"读过了","pending":" ","evidence_ids":["E1"]}', "incomplete_summary"),
            ('{"progress":"读过了","pending":"回答","evidence_ids":[]}', "evidence_mismatch"),
            ('{"progress":"读过了","pending":"回答","evidence_ids":["E9"]}', "evidence_mismatch"),
            ('{"progress":"读过了","pending":"回答","evidence_ids":["E1","E1"]}', "evidence_mismatch"),
        ]
        for response, reason in cases:
            with self.subTest(reason=reason, response=response):
                memory = self.memory()
                before = memory.to_dict()
                self.assertFalse(memory.compact(lambda _: response))
                self.assertEqual(memory.to_dict(), before)
                self.assertEqual(memory.compaction_reason, reason)

    def test_summary_exception_and_size_growth_are_atomic(self):
        memory = self.memory()
        before = memory.to_dict()
        def failed(_):
            raise TimeoutError("摘要服务失败")
        with self.assertRaises(TimeoutError):
            memory.compact(failed)
        self.assertEqual(memory.to_dict(), before)
        self.assertFalse(memory.compact(lambda transcript: json.dumps({
            "progress": "冗长备忘" * 3000, "pending": "整理", "evidence_ids": json.loads(transcript)['required_evidence_ids']})))
        self.assertEqual(memory.compaction_reason, "no_size_reduction")
        self.assertEqual(memory.to_dict(), before)

    def test_same_source_deduplicates_but_changed_or_truncated_observations_stay_distinct(self):
        memory = self.memory()
        self.assertTrue(memory.compact(summary))
        for index, source in enumerate([document(score=9), document(truncated=True), document(sha256="b" * 64)]):
            observe(memory, f"r{index}", "search_knowledge", {"results": [source]})
            memory.add_assistant({"content": "继续。"})
            self.assertTrue(memory.compact(summary))
        documents = [r for r in memory.archive.records.values() if r['kind'] == 'document']
        self.assertEqual(len(documents), 3)
        self.assertTrue(any(r['metadata']['truncated'] for r in documents))

    def test_session_roundtrip_preserves_archived_and_pending_evidence(self):
        memory = self.memory()
        self.assertTrue(memory.compact(summary))
        observe(memory, "calc", "calculator", {"expression": "2+3", "result": 5})
        restored = Memory.from_dict(json.loads(json.dumps(memory.to_dict())))
        self.assertEqual(restored.to_dict(), memory.to_dict())
        restored.add_assistant({"content": "最后整理"})
        self.assertTrue(restored.compact(summary))
        calc = next(r for r in restored.archive.records.values() if r['kind'] == 'calculation')
        self.assertEqual(json.loads(calc['content'])['result'], 5)
        self.assertEqual(restored.check(), [])
        snapshot = restored.to_dict()
        next(iter(snapshot['archive_snapshot']['records'].values()))['content'] = "篡改"
        self.assertNotEqual(snapshot, restored.to_dict())

    def test_invalid_tool_pairing_is_rejected_before_calling_summarizer(self):
        memory = self.memory()
        memory.add_assistant(call("pending", "read_knowledge"))
        before = memory.to_dict()
        with self.assertRaises(ValueError):
            memory.compact(lambda _: self.fail("不应调用摘要模型"))
        self.assertEqual(memory.to_dict(), before)

    def test_complete_parallel_tool_batch_stays_together(self):
        memory = self.memory()
        message = call("c1", "calculator", expression="1+1")
        message["tool_calls"].extend(call("c2", "calculator", expression="2+2")["tool_calls"])
        memory.add_assistant(message)
        memory.add_tool("c1", "2")
        memory.add_tool("c2", "4")
        self.assertTrue(memory.compact(summary))
        self.assertEqual([m["tool_call_id"] for m in memory.for_model() if m["role"] == "tool"], ["c1", "c2"])
        self.assertEqual(memory.check(), [])

    def test_protected_data_over_budget_is_visible_and_is_not_truncated(self):
        memory = self.memory()
        memory.budget_chars = 20
        self.assertTrue(memory.compact(summary))
        self.assertTrue(memory.needs_compaction())
        self.assertEqual(next(r for r in memory.archive.records.values() if r['kind'] == 'document')['content'], document()['excerpt'])
        self.assertFalse(memory.compact(lambda _: self.fail("只有最近一轮，不应再次付费摘要")))
        self.assertEqual(memory.compaction_reason, "no_old_units")

    def test_tool_adapters_preserve_files_and_worker_claims_as_observations(self):
        self.assertEqual(extract_tool_evidence("read_file", '{"path":"n.txt","content":"n=128"}'),
                         [{"kind": "file", "path": "n.txt", "content": "n=128"}])
        data = {"worker_id": "worker_1", "task": "分析", "status": "no_tool_call", "answer": "81% [D1:L51-L60]"}
        self.assertEqual(extract_tool_evidence("run_worker", json.dumps(data)), [{"kind": "worker_report", **data}])
        for name, result in [("calculator", '{"result":NaN,"expression":"x"}'),
                             ("read_knowledge", '{"results":[{}]}'), ("custom", '{"result":128}'),
                             ("calculator", "Error: 工具失败")]:
            self.assertEqual(extract_tool_evidence(name, result), [])

    def test_rejected_summary_loop_continues_with_original_observations(self):
        memory = self.memory()
        before = memory.for_model()
        events = []
        class LLM:
            def summarize(self, transcript):
                return ""
            def chat(self, messages):
                self.messages = copy.deepcopy(messages)
                return {"role": "assistant", "content": "资料中样本量是 n=128。"}
        llm = LLM()
        with redirect_stdout(io.StringIO()):
            result = agent.run(memory.last_user(), llm, memory, resume=True, max_steps=1, event_sink=events.append)
        self.assertEqual(result["status"], "no_tool_call")
        self.assertEqual(llm.messages, before)
        event = next(e for e in events if e["type"] == "memory.compaction.finished")
        self.assertFalse(event["payload"]["compacted"])
        self.assertEqual(event["payload"]["reason"], "empty_summary")
        self.assertTrue(event["payload"]["over_budget"])


if __name__ == "__main__":
    unittest.main()
