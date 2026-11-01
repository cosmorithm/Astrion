"""Opt-in real isolation checks. Skips are not evidence of passing isolation."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.execution.service import Computation
from research.store import Store
from research.acceptance import stage3


@unittest.skipUnless(
    os.environ.get("ASTRION_TEST_DOCKER") == "1", "requires opt-in real Docker runtime"
)
class LiveDockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "db")
        self.addCleanup(self.store.close)
        self.project = self.store.create_project("isolation")
        self.service = Computation(self.store, self.project)
        availability = self.service.executor.availability()
        self.assertTrue(availability["available"], availability)

    def test_real_statistics_chart_script_report(self):
        result = stage3(self.store, self.project)
        self.assertTrue(result["task_completed"], result)

    def test_real_network_secret_filesystem_isolation(self):
        with patch.dict(os.environ, {"AGENT_API_KEY": "host-secret-sentinel"}):
            result = self.service.execute(
                """import os, socket, json
checks = {'secret_absent': 'AGENT_API_KEY' not in os.environ, 'host_absent': not os.path.exists('/Users/apple')}
try:
    open('/inputs/escape.txt','w').write('x'); checks['readonly_inputs'] = False
except OSError: checks['readonly_inputs'] = True
try:
    open('/etc/escape.txt','w').write('x'); checks['readonly_root'] = False
except OSError: checks['readonly_root'] = True
try:
    socket.create_connection(('1.1.1.1',443),timeout=1); checks['network_denied'] = False
except OSError: checks['network_denied'] = True
print(json.dumps(checks))""",
                timeout=10,
            )
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(all(json.loads(result["stdout"]).values()), result)

    def test_real_timeout_and_output_limits(self):
        result = self.service.execute("while True: pass", timeout=1)
        self.assertEqual(result["status"], "timeout", result)
        result = self.service.execute("print('x'*50000)")
        self.assertTrue(result["stdout_truncated"], result)
        self.assertLessEqual(len(result["stdout"]), 6000)
        result = self.service.execute("open('large.txt','wb').write(b'x'*5000000)")
        self.assertNotEqual(result["status"], "completed", result)

    def test_real_memory_limit(self):
        result = self.service.execute("x = bytearray(900_000_000)", timeout=10)
        self.assertNotEqual(result["status"], "completed", result)
