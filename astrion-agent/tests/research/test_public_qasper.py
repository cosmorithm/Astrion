import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
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
                      'cases':[{'question_id':qid,'workspace':str(workspace),'usage':{'totals':{'total_tokens':10},'unknown_calls':0},'duration_seconds':1}]}
                (suite/'summary.json').write_text(json.dumps(data))
                (suite/'gold.json').write_text(json.dumps({paper:{'qas':[{'question_id':qid}]}}))
                (suite/'predictions.jsonl').write_text(json.dumps({'question_id':qid,'predicted_answer':'x','predicted_evidence':[]})+'\n')
                suites.append(suite)
            path,result=qasper.merge(suites,evaluator,root/'merged')
            self.assertEqual(result['sample_count'],2)
            self.assertEqual(result['metrics']['Answer F1'],1.0)
            self.assertEqual(result['model'],'example')
            self.assertEqual(result['known_total_tokens'],20)
            with self.assertRaisesRegex(ValueError,'duplicate_paper'):
                qasper.merge([suites[0],suites[0]],evaluator,root/'merged')

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
