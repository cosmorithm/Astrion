"""Delegation hints do not add a model call or change frozen toolset prompts."""
import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from research.prompts import run_prompt
from research.runner import run_research
from research.store import Store
from test_research import ScriptedLLM, call


class DelegationGuidanceTests(unittest.TestCase):
    def test_frozen_explicit_toolset_prompt_stays_unchanged(self):
        prompt = run_prompt(20, 6, 3, claims_enabled=False, delegation_guidance=False)
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(),
                         'd9162c439c2354e15697b0f23daec44eac549abe26d76124c275b4230e10564c')

    def test_budget_aware_guidance_and_single_model_decision(self):
        for workers in (0, 1, 3):
            with self.subTest(workers=workers), tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / 'research.sqlite3')
                self.addCleanup(store.close)
                project = store.create_project('project')
                llm = ScriptedLLM([call('finish', {'answer': 'done'}, 'finish')])
                with contextlib.redirect_stdout(io.StringIO()):
                    result = run_research(store, project, 'question', llm, max_steps=2, max_workers=workers)
                self.assertEqual(len(llm.inputs), 1)
                self.assertEqual(result['delegation_guidance'], 'budget_aware_v1')
                prompt = llm.inputs[0][0][0]['content']
                if workers == 0:
                    self.assertIn('未分配子 Agent', prompt)
                elif workers == 1:
                    self.assertIn('最多委派一个子 Agent', prompt)
                else:
                    self.assertIn('两个或更多独立子任务', prompt)
                    self.assertIn('如果你是有委派工具的主 Agent', prompt)
                    self.assertIn('简单任务可直接完成', prompt)

    def test_explicit_toolset_uses_legacy_prompt_in_live_research_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'research.sqlite3')
            try:
                project = store.create_project('project')
                llm = ScriptedLLM([call('finish', {'answer': 'done'}, 'finish')])
                with contextlib.redirect_stdout(io.StringIO()):
                    result = run_research(store, project, 'question', llm, max_steps=20,
                                          worker_steps=6, max_workers=3, tool_allowlist={'finish'})
                self.assertEqual(result['delegation_guidance'], 'legacy')
                self.assertEqual(llm.inputs[0][0][0]['content'],
                                 run_prompt(20, 6, 3, claims_enabled=False, delegation_guidance=False))
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
