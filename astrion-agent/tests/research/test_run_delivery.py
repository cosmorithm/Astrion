import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_store import ContextStore
from memory import Memory
from memory_evidence import extract_tool_evidence
from research.citations import audit_citations
from research.runner import run_research
from research.storage.artifacts import Artifacts
from research.store import Store
from test_research import ScriptedLLM, call


class DeliveryTests(unittest.TestCase):
    def test_failed_execution_reaches_loop_archive_report_and_event_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db")
            self.addCleanup(store.close)
            project = store.create_project("delivery")
            llm = ScriptedLLM(
                [
                    call("execute_python", {"code": "print(3)"}, "exec1"),
                    {"role": "assistant", "content": "运行环境不可用，尚未得到执行结果。"},
                ]
            )
            with patch(
                "research.execution.docker.shutil.which", return_value=None
            ), contextlib.redirect_stdout(io.StringIO()):
                result = run_research(store, project, "分析数据", llm, max_steps=2)
            observed = next(
                i["event"]["payload"]
                for i in result["trace"]
                if i["event"]["type"] == "tool.call.finished"
            )
            self.assertEqual(json.loads(observed["content"])["status"], "unavailable")
            report = Artifacts(store).bytes(project, result["report_artifact"]["id"]).decode()
            self.assertIn("尚未得到", report)
            journal = Path(result["event_log"])
            self.assertTrue(journal.is_file())
            saved = json.loads(journal.with_name("result.json").read_text())
            self.assertEqual(saved["report_artifact"]["id"], result["report_artifact"]["id"])

    def test_model_exception_leaves_failed_run_record(self):
        class Broken:
            def chat(self, *args, **kwargs):
                raise RuntimeError("upstream failed")

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db")
            self.addCleanup(store.close)
            project = store.create_project("failed")
            with self.assertRaises(RuntimeError), contextlib.redirect_stdout(io.StringIO()):
                run_research(store, project, "question", Broken())
            result = json.loads(next(Path(tmp).glob("runs/*/*/result.json")).read_text())
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error_type"], "RuntimeError")

    def test_single_line_citation_is_checked_not_ignored(self):
        trace = [
            {
                "event": {
                    "type": "tool.call.finished",
                    "payload": {
                        "name": "read_document",
                        "outcome": "success",
                        "content": json.dumps(
                            {
                                "results": [
                                    {
                                        "document_id": 1,
                                        "start_line": 3,
                                        "end_line": 3,
                                        "truncated": False,
                                    }
                                ]
                            }
                        ),
                    },
                }
            }
        ]
        result = audit_citations("[D1:L3] [D1:L4]", trace)
        self.assertEqual([i["in_retrieved_range"] for i in result["checks"]], [True, False])
        self.assertFalse(result["semantic_support_checked"])

    def test_execution_failure_survives_archive_reread(self):
        payload = {
            "execution_id": "x",
            "status": "timeout",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": {"code": "execution_timeout"},
            "artifacts": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory("test", archive=ContextStore(Path(tmp) / "archive.sqlite3"))
            memory.add_assistant(call("execute_python", {"code": "while True: pass"}, "e1"))
            memory.add_tool(
                "e1",
                json.dumps(payload),
                evidence=extract_tool_evidence("execute_python", json.dumps(payload)),
            )
            matches = json.loads(memory.search_evidence("execution_timeout"))
            execution = next(item for item in matches["results"] if item["kind"] == "execution")
            restored = json.loads(memory.read_evidence(execution["ref"]))
            self.assertEqual(json.loads(restored["text"])["status"], "timeout")

    def test_artifact_archive_retains_exact_multiline_text(self):
        payload = {"id": "artifact1", "sha256": "test", "content": "group,value\nA,2\nB,4\n"}
        memory = Memory("test")
        memory.add_assistant(call("read_project_file", {"artifact_id": "artifact1"}, "r1"))
        memory.add_tool(
            "r1",
            json.dumps(payload),
            evidence=extract_tool_evidence("read_project_file", json.dumps(payload)),
        )
        matches = json.loads(memory.search_evidence("artifact1"))["results"]
        item = next(item for item in matches if item["kind"] == "artifact")
        restored = json.loads(memory.read_evidence(item["ref"]))
        self.assertEqual(restored["text"], payload["content"])
