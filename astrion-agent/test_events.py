"""Run/Step/Event v1 的离线测试。"""

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from events import EVENT_SCHEMA_VERSION, EventEmitter, EventRecorder


FIXED_TIME = datetime(2026, 10, 30, 12, 0, 0, 123000, tzinfo=timezone.utc)


class EventTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory(prefix="astrion-agent-events-")
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)

    def test_emitter_adds_version_identity_order_and_independent_payloads(self):
        received = []
        emitter = EventEmitter(received.append, run_id="run_test",
                               clock=lambda: FIXED_TIME)
        payload = {"task": "测试"}

        first = emitter.emit("run.started", payload=payload)
        payload["task"] = "后来修改"
        received[0]["payload"]["task"] = "sink 修改"
        second = emitter.emit("model.call.started", step=1, payload={
            "model_call_id": "model-1", "purpose": "decision",
        })

        self.assertEqual(first["schema_version"], EVENT_SCHEMA_VERSION)
        self.assertEqual(first["event_id"], "run_test:event:000001")
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(first["occurred_at"], "2026-10-30T12:00:00.123Z")
        self.assertEqual(first["payload"], {"task": "测试"})
        self.assertEqual(second["sequence"], 2)
        self.assertEqual(second["step"], 1)

    def test_recorder_requires_one_run_and_contiguous_sequences(self):
        recorder = EventRecorder()
        EventEmitter(recorder, run_id="run_a", clock=lambda: FIXED_TIME).emit(
            "run.started", payload={}
        )

        with self.assertRaisesRegex(ValueError, "只能记录一次 run"):
            EventEmitter(recorder, run_id="run_b", clock=lambda: FIXED_TIME).emit(
                "run.started", payload={}
            )

        gap = recorder.events[0]
        gap["event_id"] = "run_a:event:000003"
        gap["sequence"] = 3
        with self.assertRaisesRegex(ValueError, "sequence 不连续"):
            recorder(gap)

    def test_non_json_payload_is_rejected_before_reaching_sink(self):
        received = []
        emitter = EventEmitter(received.append, run_id="run_test",
                               clock=lambda: FIXED_TIME)

        with self.assertRaises(TypeError):
            emitter.emit("run.started", payload={"invalid": object()})

        self.assertEqual(received, [])

    def test_recorder_exports_an_atomic_private_json_document(self):
        recorder = EventRecorder()
        emitter = EventEmitter(recorder, run_id="run_export",
                               clock=lambda: FIXED_TIME)
        emitter.emit("run.started", payload={"task": "测试"})
        emitter.emit("run.finished", payload={
            "status": "completed", "steps": 1, "answer": "完成",
        })
        path = self.root / "nested" / "events.json"

        saved = recorder.save(path)

        self.assertEqual(saved, path.resolve())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), recorder.to_dict())
        self.assertEqual(recorder.to_dict()["run_id"], "run_export")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_failed_replace_keeps_previous_log_and_removes_temporary_file(self):
        recorder = EventRecorder()
        emitter = EventEmitter(recorder, run_id="run_export",
                               clock=lambda: FIXED_TIME)
        emitter.emit("run.started", payload={})
        path = recorder.save(self.root / "events.json")
        original = path.read_bytes()
        emitter.emit("run.finished", payload={
            "status": "completed", "steps": 0, "answer": None,
        })

        with patch("events.os.replace", side_effect=OSError("磁盘错误")):
            with self.assertRaisesRegex(OSError, "磁盘错误"):
                recorder.save(path)

        self.assertEqual(path.read_bytes(), original)
        self.assertEqual([item.name for item in self.root.iterdir()], ["events.json"])


if __name__ == "__main__":
    unittest.main()
