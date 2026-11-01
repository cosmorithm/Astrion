import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from evaluation.public_research import qasper
from test_research import ScriptedLLM, call


class PublicQasperTests(unittest.TestCase):
    def test_selection_uses_stable_paper_order_and_offset(self):
        data={'a':{'qas':[{'question_id':'qa'},{'question_id':'extra'}]},
              'b':{'qas':[{'question_id':'qb'}]}}
        selected=qasper.select(data,1,offset=1)
        self.assertEqual((selected[0][0],selected[0][2]['question_id']),('b','qb'))
        with self.assertRaises(ValueError): qasper.select(data,2,offset=1)
        with self.assertRaises(ValueError): qasper.select(data,1,offset=-1)

    def test_prediction_uses_only_cited_and_observed_paragraphs(self):
        paras = {4:'first', 5:'second', 6:'third'}
        answer = 'ANSWER: five\nEVIDENCE: [D1:L4-L5] [D1:L6] [D2:L4]'
        clean, evidence = qasper.prediction(answer, 1, [(1,4,5)], paras)
        self.assertEqual(clean, 'five')
        self.assertEqual(evidence, ['first','second'])
        self.assertEqual(qasper.prediction('ANSWER: five [D1:L4]',1,[],paras)[1], [])

    def test_paper_text_keeps_gold_paragraph_mapping_not_answers(self):
        paper={'title':'Title','abstract':'Abstract','full_text':[{'section_name':'Results','paragraphs':['paragraph a','paragraph b']}],
               'figures_and_tables':[{'caption':'A table'}], 'qas':[{'answer':'secret answer'}]}
        text, mapping=qasper.paper_text(paper)
        self.assertEqual(mapping,{4:'paragraph a',5:'paragraph b'})
        self.assertNotIn('secret answer',text)
        self.assertIn('Figure/table caption',text)

    def test_merge_requires_matching_settings_and_unique_questions(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); evaluator=root/'official.py'
            evaluator.write_text("def get_answers_and_evidence(data, flag):\n return {qa['question_id']:[{}] for p in data.values() for qa in p['qas']}\ndef evaluate(gold,pred):\n return {'Answer F1':len(pred)/len(gold),'Evidence F1':0.0}\n")
            suites=[]
            for i in (1,2):
                suite=root/f'suite{i}';suite.mkdir();workspace=suite/'case';events=workspace/'runs';events.mkdir(parents=True)
                (events/'events.jsonl').write_text(json.dumps({'event':{'type':'model.call.started','payload':{'model':'example'}}})+'\n')
                qid=f'q{i}';paper=f'p{i}'
                data={'benchmark':'QASPER','version':'0.3','split':'dev','dataset_sha256':'a',
                      'official_evaluator_sha256':qasper.sha256(evaluator),'settings':{'steps':10},
                      'cases':[{'question_id':qid,'status':'completed' if i==1 else 'error','workspace':str(workspace),'usage':{'totals':{'total_tokens':10},'unknown_calls':0},'duration_seconds':1}]}
                (suite/'summary.json').write_text(json.dumps(data))
                (suite/'gold.json').write_text(json.dumps({paper:{'qas':[{'question_id':qid}]}}))
                (suite/'predictions.jsonl').write_text(json.dumps({'question_id':qid,'predicted_answer':'x','predicted_evidence':[]})+'\n')
                suites.append(suite)
            path,result=qasper.merge(suites,evaluator,root/'merged')
            self.assertEqual(result['sample_count'],2)
            self.assertEqual(result['metrics']['Answer F1'],1.0)
            self.assertEqual(result['model'],'example')
            self.assertEqual(result['successful_cases'],1)
            self.assertEqual(result['successful_only_metrics']['Answer F1'],1.0)
            self.assertEqual(result['known_total_tokens'],20)
            with self.assertRaisesRegex(ValueError,'duplicate_paper'):
                qasper.merge([suites[0],suites[0]],evaluator,root/'merged')

    def test_retries_replace_only_failed_cases_without_mutating_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);evaluator=root/'official.py'
            evaluator.write_text("def get_answers_and_evidence(data, flag):\n return {q['question_id']:[{}] for p in data.values() for q in p['qas']}\ndef evaluate(gold,pred):\n return {'Answer F1':len(pred)/len(gold),'Evidence F1':0.0}\n")
            settings={'steps':10};digest=qasper.sha256(evaluator)
            def mk_suite(name,pid,qid,status):
                path=root/name;path.mkdir();work=path/'case';logs=work/'runs'/'1'/'run';logs.mkdir(parents=True)
                (logs/'events.jsonl').write_text(json.dumps({'event':{'type':'model.call.started','payload':{'model':'example'}}})+'\n')
                case={'question_id':qid,'paper_id':pid,'status':status,'workspace':str(work),'usage':{'totals':{'total_tokens':10},'unknown_calls':0},'duration_seconds':1}
                gold={pid:{'qas':[{'question_id':qid}]}};(path/'gold.json').write_text(json.dumps(gold))
                pred={'question_id':qid,'predicted_answer':'answer','predicted_evidence':[]}
                (path/'predictions.jsonl').write_text(json.dumps(pred)+'\n' if status!='error' else '')
                report={'benchmark':'QASPER','dataset_sha256':'data','official_evaluator_sha256':digest,'settings':settings,'split':'dev','cases':[case]}
                (path/'summary.json').write_text(json.dumps(report));return path,case
            a,failed=mk_suite('a','p1','q1','error');b,ok=mk_suite('b','p2','q2','completed');r,_=mk_suite('retry','p1','q1','completed')
            original={'benchmark':'QASPER','dataset_sha256':'data','official_evaluator_sha256':digest,'settings':settings,
                      'split':'dev','model':'example','source_suites':[str(a),str(b)],'cases':[failed,ok],
                      'known_total_tokens':20,'unknown_token_runs':0,'duration_seconds':2}
            original_path=root/'original.json';original_path.write_text(json.dumps(original));before=original_path.read_bytes()
            _,result=qasper.replace_failed(original_path,[r],evaluator,root/'out')
            self.assertEqual(result['successful_cases'],2);self.assertEqual(result['replaced_question_ids'],['q1'])
            self.assertEqual(result['known_total_tokens'],30);self.assertEqual(original_path.read_bytes(),before)
            with self.assertRaisesRegex(ValueError,'not_unique_failed_case'):
                qasper.replace_failed(original_path,[r,r],evaluator,root/'out')

    def test_failed_usage_is_never_assumed_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); self.assertIsNone(qasper.partial_usage(root)['unknown_calls'])
            logs=root/'runs'/'1'/'run';logs.mkdir(parents=True)
            events=[{'event':{'type':'model.call.finished','payload':{'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}}}},
                    {'event':{'type':'model.call.failed','payload':{}}}]
            (logs/'events.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in events))
            usage=qasper.partial_usage(root)
            self.assertEqual(usage['totals']['total_tokens'],5);self.assertEqual(usage['unknown_calls'],1)

    def test_system_exit_isolated_to_case_and_partial_files_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);data=root/'data';data.mkdir()
            def paper(n):
                return {'title':'Title','abstract':'Abstract','full_text':[{'section_name':'s','paragraphs':['value']}],
                        'figures_and_tables':[],'qas':[{'question_id':f'q{n}','question':'What?','answers':[]}]}
            (data/'qasper-dev-v0.3.json').write_text(json.dumps({'p1':paper(1),'p2':paper(2)}))
            (data/'qasper_evaluator.py').write_text('import json\nprint(json.dumps({"Answer F1": 0.5}))\n')
            record={'result':{'status':'completed','answer':'ANSWER: value'},'observed_ranges':[],
                    'citation_audit':{},'duration_seconds':1,'usage':{},'workers':[],'run_id':'r'}
            with patch.object(qasper,'run_research',side_effect=[SystemExit('api failure'),record]), contextlib.redirect_stdout(io.StringIO()):
                path,result=qasper.run(data,root/'out',lambda:object(),count=2,steps=4,workers=0)
            self.assertEqual([x['status'] for x in result['cases']],['error','completed'])
            self.assertEqual(len(json.loads((path/'gold.json').read_text())),2)
            self.assertEqual(len((path/'predictions.jsonl').read_text().splitlines()),1)
            self.assertTrue((path/'cases.json').exists())

    def test_recover_partial_validates_immutable_paper_and_predictions(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);data=root/'data';data.mkdir();partial=root/'partial';partial.mkdir();work=partial/'case-001';work.mkdir()
            paper={'title':'Title','abstract':'Abstract','full_text':[{'section_name':'s','paragraphs':['value']}],
                   'figures_and_tables':[],'qas':[{'question_id':'q1','question':'What?','answers':[]}]}
            (data/'qasper-dev-v0.3.json').write_text(json.dumps({'p1':paper}))
            (data/'qasper_evaluator.py').write_text('import json\nprint(json.dumps({"Answer F1": 0.5}))\n')
            from research.store import Store
            store=Store(work/'research.sqlite3');project=store.create_project('test');text,_=qasper.paper_text(paper)
            store.add_document_text(project,'paper.txt',text);store.close()
            case={'paper_id':'p1','question_id':'q1','status':'completed','answer':'ANSWER: value',
                  'predicted_answer':'value','evidence_paragraphs':0,'observed_ranges':[], 'workspace':str(work)}
            original=json.dumps([case]);(partial/'cases.json').write_text(original)
            path,result=qasper.recover_partial(partial,data,root/'out',offset=0)
            self.assertEqual(result['sample_count'],1);self.assertEqual((partial/'cases.json').read_text(),original)
            case['paper_id']='wrong';(partial/'cases.json').write_text(json.dumps([case]))
            with self.assertRaisesRegex(ValueError,'partial_question_mismatch'):
                qasper.recover_partial(partial,data,root/'out',offset=0)

    def test_real_agent_loop_is_isolated_and_official_scorer_receives_predictions(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); data=root/'data';data.mkdir()
            qa={'question_id':'qid','question':'What value?', 'answers':[{'answer':{'free_form_answer':'secret_gold'}}]}
            paper={'title':'Title','abstract':'Abstract','full_text':[{'section_name':'Results','paragraphs':['value 5']}],
                   'figures_and_tables':[],'qas':[qa]}
            (data/'qasper-dev-v0.3.json').write_text(json.dumps({'paper':paper}))
            (data/'qasper_evaluator.py').write_text('import json\nprint(json.dumps({"Answer F1": 0.5}))\n')
            model=ScriptedLLM([call('read_knowledge',{'document_id':1,'start_line':4,'end_line':4},'r'),
                               call('finish',{'answer':'ANSWER: 5\nEVIDENCE: [D1:L4]'},'f')])
            with contextlib.redirect_stdout(io.StringIO()):
                path, result=qasper.run(data,root/'results',lambda:model,count=1,steps=4,workers=0)
            self.assertEqual(result['metrics']['Answer F1'],0.5)
            self.assertEqual(result['cases'][0]['evidence_paragraphs'],1)
            self.assertNotIn('secret_gold',json.dumps(model.inputs))
            self.assertIn('value 5',(path/'predictions.jsonl').read_text())
            self.assertTrue((path/'case-001'/'research.sqlite3').is_file())
            self.assertFalse((data/'research.sqlite3').exists())

if __name__=='__main__': unittest.main()
