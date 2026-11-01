import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from evaluation.public_research import qasper_compare


class ComparisonTests(unittest.TestCase):
    def fixture(self,root,steps,error=False):
        root=Path(root);suite=root/f'suite{steps}';suite.mkdir()
        (suite/'summary.json').write_text(json.dumps({'code_version':{'git_commit':'fixture','worktree_dirty':False}}))
        cases=[]
        for n in (1,2):
            work=root/f'work{steps}-{n}';logs=work/'runs'/'1'/'r';logs.mkdir(parents=True)
            ev={'role':'root','event':{'type':'run.finished','payload':{'steps':steps-2}}}
            (logs/'events.jsonl').write_text(json.dumps(ev)+'\n')
            status='error' if error and n==2 else 'completed'
            cases.append({'question_id':f'q{n}','paper_id':f'p{n}','workspace':str(work),'status':status,
                'predicted_answer':'answer' if status=='completed' else '',
                'official_metrics':{'Answer F1':n/2,'Evidence F1':n/4},
                'workers':0,'duration_seconds':steps,'usage':{'totals':{'total_tokens':steps*100}}})
        report={'benchmark':'QASPER','version':'0.3','split':'dev','model':'m','dataset_sha256':'data',
          'official_evaluator_sha256':'eval','settings':{'steps':steps,'workers':3,'worker_steps':6,'budget_chars':12000},
          'cases':cases,'metrics':{'Answer F1':0.5},'source_suites':[str(suite)],'duration_seconds':steps*2,
          'known_total_tokens':steps*200,'unknown_token_runs':1 if error else 0}
        p=root/f'merged{steps}.json';p.write_text(json.dumps(report));return p

    def test_comparison_pairs_only_successful_cases_and_keeps_failures(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);a=self.fixture(root,10);b=self.fixture(root,15,error=True)
            with patch.object(qasper_compare.qasper_analysis,'analyze',return_value=(root,{'statistics':{'retrieval_measured':2},'cases':[{'question_id':q,'retrieval':{'status':'measured','first_search_recall_at_k':.5,'first_search_partial_recall_at_k':.75,'all_observed_recall':1.}} for q in ('q1','q2')]})):
                path,result=qasper_compare.compare([a,b],root/'out')
            self.assertEqual(result['paired_successful_cases'],1)
            self.assertEqual(result['paired_retrieval_cases'],1)
            self.assertEqual(result['groups'][0]['paired_first_search_recall_at_k'],.5)
            self.assertEqual(result['groups'][0]['paired_answer_f1'],.5)
            self.assertEqual(result['groups'][1]['case_statuses']['error'],1)
            self.assertEqual(result['groups'][1]['mean_root_steps'],13)
            self.assertEqual(result['groups'][0]['mean_answer_words'],1)
            self.assertTrue((path/'summary.json').is_file())
            self.assertIn('配对题数：1',(path/'REPORT.md').read_text())

    def test_retry_groups_require_explicit_opt_in_and_are_labelled(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);a=self.fixture(root,10);b=self.fixture(root,15)
            report=json.loads(b.read_text());report['retry_suites']=[report['source_suites'][0]]
            report['retry_attempts']=[{'question_id':'q1'}];report['replaced_question_ids']=['q1'];b.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'retry_report_not_allowed'):
                qasper_compare.compare([a,b],root/'out')
            fake={'statistics':{},'cases':[{'question_id':q,'retrieval':{'status':'measured','first_search_recall_at_k':.5,
                         'first_search_partial_recall_at_k':.75,'all_observed_recall':1.}} for q in ('q1','q2')]}
            with patch.object(qasper_compare.qasper_analysis,'analyze',return_value=(root,fake)):
                path,result=qasper_compare.compare([a,b],root/'out',allow_retries=True)
            self.assertTrue(result['includes_retries']);self.assertEqual(result['groups'][1]['retry_count'],1)
            self.assertIn('重试', (path/'REPORT.md').read_text())

    def test_failure_classification_uses_recorded_error(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);report_path=self.fixture(root,15,error=True)
            report=json.loads(report_path.read_text());case=report['cases'][1]
            f=next((Path(case['workspace'])/'runs').rglob('events.jsonl'))
            failure={'role':'root','event':{'type':'model.call.failed','payload':{'error':{'message':'HTTP 400: reasoning_content required'}}}}
            with f.open('a') as stream:stream.write(json.dumps(failure)+'\n')
            self.assertEqual(qasper_compare.failure_kind(case),'deepseek_reasoning_protocol_400')
            case['status']='completed';self.assertIsNone(qasper_compare.failure_kind(case))

    def test_incompatible_conditions_or_samples_are_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);a=self.fixture(root,10);b=self.fixture(root,15)
            r=json.loads(b.read_text());r['settings']['workers']=2;b.write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError,'incompatible_conditions'):
                qasper_compare.compare([a,b],root/'out')
            r['settings']['workers']=3;r['cases'][0]['question_id']='different';b.write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError,'case_sets_differ'):
                qasper_compare.compare([a,b],root/'out')

if __name__=='__main__':unittest.main()
