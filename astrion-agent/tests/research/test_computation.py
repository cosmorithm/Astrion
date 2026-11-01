"""Contract and storage tests. Fake adapters validate plumbing, not isolation."""

import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from research.store import Store
from research.storage.artifacts import Artifacts
from research.execution.docker import DockerExecutor, capture, client_env
from research.execution.service import Computation
from tools.research import build_tools
from tooling import ToolRegistry, ToolCall
from memory_evidence import extract_tool_evidence


class FakeExecutor:
    def execute(self, directory, timeout):
        assert (directory / "analysis.py").is_file()
        assert (directory / "input.csv").read_text() == "value\n2\n4\n"
        return {
            "status": "completed",
            "exit_code": 0,
            "stdout": "3",
            "stderr": "",
            "files": [{"name": "result.json", "base64": base64.b64encode(b'{"mean":3}').decode()}],
        }


class ComputationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "db")
        self.addCleanup(self.store.close)
        self.a = self.store.create_project("A")
        self.b = self.store.create_project("B")
        self.repository = Artifacts(self.store)
        self.input = self.repository.put(self.a, "input.csv", b"value\n2\n4\n")

    def test_immutable_files_project_isolation_and_read_pagination(self):
        second = self.repository.put(self.a, "input.csv", b"changed")
        self.assertNotEqual(second["id"], self.input["id"])
        self.assertEqual(self.repository.read(self.a, self.input["id"], limit=5)["next_offset"], 5)
        self.assertEqual(self.repository.listing(self.a, limit=1)["next_offset"], 1)
        for name in ["../bad.py", "/absolute.py", "a\\b.py", "evil.html"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.repository.put(self.a, name, b"bad")
        with self.assertRaises(ValueError):
            self.repository.bytes(self.b, self.input["id"])
        with self.assertRaises(ValueError):
            Computation(self.store, self.b, FakeExecutor()).execute("print(1)", [self.input["id"]])

    def test_tampering_and_symlink_are_rejected(self):
        path = self.repository._path(self.a, self.input["id"], self.input["name"])
        path.write_text("changed")
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.repository.bytes(self.a, self.input["id"])
        path.unlink()
        path.symlink_to("/etc/hosts")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.repository.bytes(self.a, self.input["id"])

    def test_adapter_wiring_preserves_script_inputs_provenance_and_result(self):
        result = Computation(self.store, self.a, FakeExecutor()).execute(
            "print(3)", [self.input["id"]]
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["inputs"][0]["sha256"], self.input["sha256"])
        self.assertEqual({a["name"] for a in result["artifacts"]}, {"analysis.py", "result.json"})
        persisted = json.loads(self.repository.bytes(self.a, result["record_artifact"]))
        self.assertEqual(persisted["execution_id"], result["execution_id"])
        self.assertEqual(
            extract_tool_evidence("execute_python", json.dumps(result))[0]["kind"], "execution"
        )

    def test_unavailable_is_not_success_and_script_remains_downloadable(self):
        with patch("research.execution.docker.shutil.which", return_value=None):
            result = Computation(self.store, self.a).execute("print(3)")
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["error"]["code"], "sandbox_unavailable")
        self.assertEqual(self.repository.bytes(self.a, result["script"]), b"print(3)")

    def test_invalid_adapter_output_cannot_escape_project_or_fake_success(self):
        class Bad:
            def execute(self, *args):
                return {
                    "status": "completed",
                    "exit_code": 0,
                    "files": [{"name": "../escape.py", "base64": "eA=="}],
                }

        result = Computation(self.store, self.a, Bad()).execute("print(1)")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "invalid_execution_result")
        with self.assertRaises(ValueError):
            Computation._validated({"status": "completed", "exit_code": None})

    def test_roles_share_execution_tools_and_worker_cannot_delegate(self):
        for worker in (True, False):
            specs, impl = build_tools(self.store, self.a, worker=worker, delegate=lambda **_: "{}")
            registry = ToolRegistry(specs, impl | {"update_task_plan": lambda **_: "{}"})
            self.assertEqual("run_worker" in registry.specs, not worker)
            for name in (
                "list_artifacts",
                "read_project_file",
                "write_project_file",
                "execute_python",
            ):
                self.assertIn(name, registry.specs)
            result = registry.execute(
                ToolCall("x", "read_project_file", json.dumps({"artifact_id": self.input["id"]}))
            )
            self.assertEqual(result.outcome, "success")
            denied = registry.execute(
                ToolCall("denied", "read_project_file", json.dumps({"artifact_id": "missing"}))
            )
            self.assertEqual(denied.error["code"], "artifact_not_found_in_project")
            self.assertEqual(
                extract_tool_evidence("read_project_file", result.content)[0]["sha256"],
                self.input["sha256"],
            )

    def test_docker_flags_and_host_client_environment(self):
        executor = DockerExecutor()
        executor.binary = "/bin/docker"
        command = executor.command("/tmp/readonly", 2, "test", "sha256:test")
        for required in (
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory=512m",
            "--memory-swap=512m",
            "--pids-limit=64",
            "--pull=never",
        ):
            self.assertIn(required, command)
        self.assertIn("type=bind,src=/tmp/readonly,dst=/inputs,readonly", command)
        with patch.dict(os.environ, {"AGENT_API_KEY": "secret", "AWS_SECRET_ACCESS_KEY": "secret"}):
            self.assertNotIn("AGENT_API_KEY", client_env())
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", client_env())
        self.assertNotIn("--env-file", command)

    def test_host_capture_deadline_and_output_cap(self):
        # This tests the host pipe monitor only; no claim of container isolation.
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            capture([sys.executable, "-c", "import time; time.sleep(5)"], 0.15)
        self.assertLess(time.monotonic() - start, 2)
        with self.assertRaisesRegex(ValueError, "output_limit"):
            capture([sys.executable, "-c", 'print("x"*100000)'], 2, 1024)

    def test_timeout_adapter_result_is_preserved_and_journaled(self):
        class TimedOut:
            def execute(self, *args):
                return {
                    "status": "timeout",
                    "exit_code": None,
                    "error": {"code": "execution_timeout"},
                    "files": [],
                }

        result = Computation(self.store, self.a, TimedOut()).execute("while True: pass", timeout=1)
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["record_artifact"])


class ArtifactApiTests(unittest.TestCase):
    def test_upload_preview_download_and_project_scope(self):
        from fastapi.testclient import TestClient
        from research import api

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            api, "DB_PATH", Path(tmp) / "db"
        ), TestClient(api.app) as client:
            a = client.post("/api/projects", json={"name": "A"}).json()["id"]
            b = client.post("/api/projects", json={"name": "B"}).json()["id"]
            response = client.post(
                f"/api/projects/{a}/artifacts", files={"file": ("data.csv", b"value\n1\n")}
            )
            self.assertEqual(response.status_code, 200)
            identifier = response.json()["id"]
            self.assertEqual(
                client.get(f"/api/projects/{a}/artifacts/{identifier}/read").json()["content"],
                "value\n1\n",
            )
            download = client.get(f"/api/projects/{a}/artifacts/{identifier}/download")
            self.assertEqual(download.content, b"value\n1\n")
            self.assertIn("attachment", download.headers["content-disposition"])
            self.assertEqual(download.headers["x-content-type-options"], "nosniff")
            for path in ("read", "download", "image"):
                self.assertEqual(
                    client.get(f"/api/projects/{b}/artifacts/{identifier}/{path}").status_code, 400
                )
            self.assertEqual(
                client.get(f"/api/projects/{a}/artifacts/{identifier}/image").status_code, 400
            )
            self.assertEqual(
                client.post(
                    f"/api/projects/{a}/artifacts", files={"file": ("../escape.py", b"no")}
                ).status_code,
                400,
            )
            with patch("research.execution.docker.shutil.which", return_value=None):
                result = client.post(f"/api/projects/{a}/execute", json={"code": "print(1)"}).json()
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(
                client.get(f'/api/projects/{a}/artifacts/{result["script"]}/download').content,
                b"print(1)",
            )
