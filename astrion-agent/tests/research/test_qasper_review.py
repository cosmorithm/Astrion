import json
import tempfile
import unittest
from pathlib import Path
from evaluation.public_research import qasper, qasper_review
from research.store import Store


class HumanReviewTests(unittest.TestCase):
    def test_template_review_unknown_and_source_tampering(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);suite=root/'suite';suite.mkdir();work=suite/'case';work.mkdir()
            store=Store(work/'research.sqlite3');pid=store.create_project('test');store.add_document_text(pid,'p.txt','source evidence');store.close()
            case={'question_id':'q1','paper_id':'p','workspace':str(work),'answer':'answer [D1:L1]',
                  'citation_audit':{'checks':[{'citation':'[D1:L1-L1]','in_retrieved_range':True}]}}
            question={'question_id':'q1','question':'question','answers':[{'answer':{'unanswerable':False,'evidence':['source evidence']}}]}
            (suite/'gold.json').write_text(json.dumps({'p':{'qas':[question]}}))
            merged={'source_suites':[str(suite)],'cases':[case]};merged_path=root/'merged.json';merged_path.write_text(json.dumps(merged))
            analysis={'benchmark':'QASPER diagnostic','source_summary':str(merged_path),'source_summary_sha256':qasper.sha256(merged_path),
                      'cases':[{'question_id':'q1','paper_id':'p','support_review':{'status':'unknown'}}]}
            ap=root/'analysis.json';ap.write_text(json.dumps(analysis))
            form=qasper_review.template(ap);self.assertEqual(form['cases'][0]['citations'][0]['text'],'source evidence')
            form['reviewer']='test reviewer';rp=root/'review.json';rp.write_text(json.dumps(form))
            _,report=qasper_review.apply(ap,rp,root/'out');self.assertEqual(report['counts']['unknown'],1)
            self.assertIsNone(report['all_case_supported_rate'])
            form['cases'][0]['answer']='altered';rp.write_text(json.dumps(form))
            with self.assertRaisesRegex(ValueError,'review_source_fields_changed'):qasper_review.apply(ap,rp,root/'out')
            form['cases'][0]['answer']='answer [D1:L1]'
            form['cases'][0].update(verdict='supported',reason='checked source');rp.write_text(json.dumps(form))
            _,report=qasper_review.apply(ap,rp,root/'out');self.assertEqual(report['all_case_supported_rate'],1)
            ap.write_text(ap.read_text()+' ')
            with self.assertRaisesRegex(ValueError,'analysis_changed'):qasper_review.apply(ap,rp,root/'out')
            merged_path.write_text(merged_path.read_text()+' ')
            with self.assertRaisesRegex(ValueError,'source_summary_changed'):qasper_review.template(ap)

if __name__=='__main__': unittest.main()
