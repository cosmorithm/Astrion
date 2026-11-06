"""Evidence presented for review must come from actual source observations."""
import json
import unittest

from research.citations import audit_citations
from evaluation.research.judges import citations


def event(name, results, *, role="root", worker_id=None):
    return {"role": role, "worker_id": worker_id, "event": {
        "type": "tool.call.finished", "step": 3, "payload": {
            "name": name, "outcome": "success", "content": json.dumps({"results": results})}}}


def source(start, end, text, **extra):
    return {"document_id": 1, "start_line": start, "end_line": end,
            "excerpt": text, "truncated": False, "name": "paper.txt", **extra}


class CitationEvidenceTests(unittest.TestCase):
    def test_outline_is_not_proof_of_reading(self):
        outline = event("document_outline", [source(1, 100, "section title")])
        check = audit_citations("目录引用 [D1:L1-L100]", [outline])["checks"][0]
        self.assertFalse(check["in_retrieved_range"])
        self.assertEqual(check["evidence"], [])

    def test_actual_read_exposes_matched_source_but_not_semantic_verdict(self):
        read = event("read_knowledge", [source(1, 3, "第一行\n样本是 128\n第三行")])
        audit = audit_citations("样本 128 [D1:L2]", [read])
        check = audit["checks"][0]
        self.assertTrue(check["in_retrieved_range"])
        self.assertEqual(check["evidence"][0]["excerpt"], "样本是 128")
        self.assertEqual(check["evidence"][0]["role"], "root")
        self.assertFalse(audit["semantic_support_checked"])

    def test_unsupported_claim_is_not_automatically_accepted(self):
        read = event("read_knowledge", [source(1, 1, "The sample size was 128.")])
        audit = audit_citations("准确率 99% [D1:L1]", [read])
        self.assertTrue(audit["checks"][0]["in_retrieved_range"])
        self.assertEqual(audit["checks"][0]["evidence"][0]["excerpt"], "The sample size was 128.")
        self.assertFalse(audit["semantic_support_checked"])

    def test_worker_and_inherited_observations_are_distinguishable(self):
        worker = event("read_knowledge", [source(1, 1, "child text")],
                       role="worker", worker_id="worker_1")
        checks = audit_citations("[D1:L1] [D2:L1]", [worker], [(2, 1, 1)])["checks"]
        self.assertEqual(checks[0]["evidence"][0]["worker_id"], "worker_1")
        self.assertTrue(checks[1]["in_retrieved_range"])
        self.assertEqual(checks[1]["evidence"], [])
        self.assertTrue(checks[1]["inherited_range"])

    def test_truncated_or_mismatched_excerpt_never_appears_as_exact_evidence(self):
        bad = event("read_document", [source(1, 2, "one line only")])
        item = audit_citations("[D1:L2]", [bad])["checks"][0]
        self.assertEqual(item["evidence"], [])
        self.assertFalse(item["in_retrieved_range"])
        metadata = source(1, 1, "placeholder")
        del metadata["excerpt"]
        self.assertFalse(audit_citations("[D1:L1]", [event("read_document", [metadata])])["checks"][0]["in_retrieved_range"])
        cut = event("read_document", [dict(source(1, 1, "partial"), truncated=True)])
        self.assertFalse(audit_citations("[D1:L1]", [cut])["checks"][0]["in_retrieved_range"])

    def test_example_citation_requires_review_instead_of_automatic_failure(self):
        answer = "未读到原文，不能引用 [D1:L1-L10] 这样的范围。"
        audit = audit_citations(answer, [])
        verdict = citations({}, {"citation_audit": audit}, {})
        self.assertEqual(verdict["status"], "pending")
        self.assertEqual(citations({}, {"citation_audit": {"status": "no_citations"}}, {})["status"], "fail")

    def test_context_keeps_examples_and_claims_strictly_audited(self):
        answer = "错误示例 [D1:L1] 不应引用。\n结论声称 [D1:L1] 是依据。"
        audit = audit_citations(answer, [])
        self.assertEqual(audit["status"], "invalid_locations")
        self.assertEqual(len(audit["checks"]), 2)
        for check in audit["checks"]:
            start, end = check["report_span"]
            self.assertEqual(answer[start:end], "[D1:L1]")
            self.assertIn("错误示例", check["report_context"])
            self.assertFalse(check["in_retrieved_range"])
        self.assertNotEqual(audit["checks"][0]["report_span"], audit["checks"][1]["report_span"])

    def test_visibility_requires_real_root_tool_body_in_final_request(self):
        from response_contract import finalization_messages
        passage = source(1, 1, '原文证据')
        read = event('read_knowledge', [passage])
        read['event']['payload']['tool_call_id'] = 'call_1'
        tool = {'role': 'tool', 'tool_call_id': 'call_1', 'content': json.dumps({'results': [passage]})}
        def decision(messages, finalizing=False):
            return {'role': 'root', 'event': {'type': 'model.call.started', 'step': 4,
                    'payload': {'purpose': 'decision', 'input': {'finalizing': finalizing},
                                'request_snapshot': {'messages': messages}}}}
        native = [{'role': 'system', 'content': '规则'}, tool]
        wrapped = finalization_messages(native, '收尾')
        check = audit_citations('[D1:L1]', [read, decision(wrapped, True)])['checks'][0]
        self.assertEqual(check['model_visibility']['status'], 'visible')
        check = audit_citations('[D1:L1]', [read, decision(native)])['checks'][0]
        self.assertEqual(check['model_visibility']['status'], 'visible')
        receipt = {'role': 'tool', 'tool_call_id': 'call_1',
                   'content': json.dumps({'archived': True, 'preview': '原文证据'})}
        wrapped = finalization_messages([native[0], receipt], '收尾')
        check = audit_citations('[D1:L1]', [read, decision(wrapped, True)])['checks'][0]
        self.assertTrue(check['in_retrieved_range'])
        self.assertEqual(check['model_visibility']['status'], 'unknown')
        forged = {'role': 'tool', 'tool_call_id': 'call_1',
                  'content': json.dumps({'results': [source(1, 1, '另一份正文')]})}
        self.assertEqual(audit_citations('[D1:L1]', [read, decision([native[0], forged])])['checks'][0]['model_visibility']['status'], 'unknown')
        # User-provided or fabricated tool-shaped text is not an observation.
        fake = [{'role': 'user', 'content': json.dumps(tool, ensure_ascii=False)}]
        self.assertEqual(audit_citations('[D1:L1]', [read, decision(fake)])['checks'][0]['model_visibility']['status'], 'unknown')
        worker = event('read_knowledge', [passage], role='worker', worker_id='w')
        worker['event']['payload']['tool_call_id'] = 'call_1'
        self.assertEqual(audit_citations('[D1:L1]', [worker, decision(native)])['checks'][0]['model_visibility']['status'], 'unknown')
        self.assertEqual(audit_citations('[D1:L1]', [read])['checks'][0]['model_visibility']['status'], 'unknown')
        # Inherited ranges alone lack an authenticated body in this run.
        self.assertEqual(audit_citations('[D1:L1]', [decision(native)], [(1, 1, 1)])['checks'][0]['model_visibility']['status'], 'unknown')

    def test_reversed_range_is_not_presented_as_evidence(self):
        read = event("read_knowledge", [source(1, 3, "first\nsecond\nthird")])
        check = audit_citations("[D1:L3-L1]", [read])["checks"][0]
        self.assertFalse(check["in_retrieved_range"])
        self.assertEqual(check["evidence"], [])


if __name__ == "__main__":
    unittest.main()
