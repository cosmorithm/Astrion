"""Project-aware execution orchestration; adapter details stay outside tools/API."""

from __future__ import annotations
from ..errors import ResearchError

import base64
import json
from pathlib import Path
import tempfile
from time import perf_counter
from uuid import uuid4

from ..storage.artifacts import Artifacts, safe_name
from .docker import DockerExecutor, failure


class Computation:
    def __init__(self, store, project, executor=None):
        store.project(project)
        self.store, self.project = store, project
        self.artifacts = Artifacts(store)
        self.executor = executor if executor is not None else DockerExecutor()

    def write(self, name, content, source_artifact_ids=(), source_document_ids=()):
        if not isinstance(content, str) or len(content.encode("utf-8")) > 100_000:
            raise ResearchError("text_output_limit: maximum 100 KB")
        if Path(name).suffix.lower() not in {".py", ".csv", ".tsv", ".json", ".txt", ".md"}:
            raise ResearchError("write_project_file_requires_text")
        if len(source_artifact_ids) + len(source_document_ids) > 30:
            raise ResearchError("source_count_limit")
        inputs = [self.artifacts.get(self.project, item) for item in source_artifact_ids]
        for identifier in source_document_ids:
            self.store.document_store.get(self.project, identifier)
        return self.artifacts.put(
            self.project,
            name,
            content.encode("utf-8"),
            provenance={
                "kind": "agent_written_unverified",
                "source_artifacts": [r["id"] for r in inputs],
                "source_documents": list(source_document_ids),
            },
        )

    def execute(self, code, input_artifact_ids=(), timeout=20):
        if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > 100_000:
            raise ResearchError("invalid_code: nonempty Python, maximum 100 KB")
        if type(timeout) is not int or not 1 <= timeout <= 60:
            raise ResearchError("invalid_timeout: choose 1–60 seconds")
        if len(input_artifact_ids) > 10 or len(set(input_artifact_ids)) != len(input_artifact_ids):
            raise ResearchError("invalid_inputs: at most 10 distinct artifact IDs")
        inputs = [self.artifacts.get(self.project, item) for item in input_artifact_ids]
        names = [item["name"] for item in inputs]
        if len(names) != len(set(names)) or "analysis.py" in names:
            raise ResearchError("duplicate_input_name: analysis.py is reserved")
        if sum(item["size"] for item in inputs) > 16_000_000:
            raise ResearchError("execution_input_limit: maximum 16 MB")
        execution_id = uuid4().hex
        provenance = {
            "kind": "execution",
            "execution_id": execution_id,
            "inputs": [{"id": i["id"], "name": i["name"], "sha256": i["sha256"]} for i in inputs],
        }
        script = self.artifacts.put(
            self.project,
            "analysis.py",
            code.encode("utf-8"),
            provenance={**provenance, "kind": "submitted_script"},
        )
        started = perf_counter()
        with tempfile.TemporaryDirectory(prefix="astrion-input-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o755)
            for item in inputs:
                path = directory / item["name"]
                path.write_bytes(self.artifacts.bytes(self.project, item["id"]))
                path.chmod(0o444)
            (directory / "analysis.py").write_text(code, encoding="utf-8")
            (directory / "analysis.py").chmod(0o444)
            raw = self.executor.execute(directory, timeout)
        try:
            result, outputs = self._validated(raw)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            result, outputs = failure("failed", "invalid_execution_result", str(exc)[:300]), []
            result.pop("files", None)
        metadata = {**provenance, "status": result["status"], "image_id": result.get("image_id")}
        artifacts = [script]
        for name, data in outputs:
            artifacts.append(self.artifacts.put(self.project, name, data, provenance=metadata))
        result.update(
            execution_id=execution_id,
            script=script["id"],
            artifacts=artifacts,
            inputs=provenance["inputs"],
            duration_seconds=perf_counter() - started,
            evidence_support="not_assessed: inspect outputs and verify claims independently",
        )
        record = self.artifacts.put(
            self.project,
            "execution.json",
            json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"),
            provenance=metadata,
        )
        result["record_artifact"] = record["id"]
        return result

    @staticmethod
    def _validated(raw):
        if not isinstance(raw, dict) or raw.get("status") not in {
            "completed",
            "failed",
            "timeout",
            "unavailable",
        }:
            raise ResearchError("unknown_execution_status")
        if raw["status"] == "completed" and (
            type(raw.get("exit_code")) is not int or raw["exit_code"] != 0
        ):
            raise ResearchError("completed_requires_zero_exit")
        files, outputs, names, total = raw.get("files", []), [], set(), 0
        if not isinstance(files, list) or len(files) > 20:
            raise ResearchError("too_many_output_files")
        for item in files:
            name = safe_name(item["name"])
            if name in names or len(item["base64"]) > 5_333_336:
                raise ResearchError("invalid_output_file")
            data = base64.b64decode(item["base64"], validate=True)
            total += len(data)
            if len(data) > 4_000_000 or total > 12_000_000:
                raise ResearchError("output_file_limit")
            names.add(name)
            outputs.append((name, data))
        result = {key: raw.get(key) for key in ("status", "exit_code", "image_id", "error")}
        if result["error"] is not None:
            result["error"] = {
                "code": str(result["error"].get("code", "execution_failed"))[:100],
                "message": str(result["error"].get("message", ""))[:1000],
            }
        for key in ("stdout", "stderr"):
            value = str(raw.get(key, ""))
            result[key] = value[:6000]
            result[key + "_truncated"] = bool(raw.get(key + "_truncated")) or len(value) > 6000
        result["omitted_files"] = [str(n)[:180] for n in raw.get("omitted_files", [])[:20]]
        return result, outputs
