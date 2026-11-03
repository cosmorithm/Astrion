"""Offline integration of the research exam, actual runner and independent judges."""
import contextlib
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research.store import Store
from evaluation.research import runner, review, model_judge
from evaluation.research import documents, dataset
from evaluation.research import __main__ as exam_cli
from test_research import ScriptedLLM, call


class ExamTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.base=self.root/'base.sqlite3'
        store=Store(self.base);project=store.create_project('frozen')
        self.papers=[]
        for n in range(1,26):
            name=f'{n:02}.txt';doc=store.add_document_text(project,name,f'number {n} evidence')
            row=store.db.execute('SELECT sha256 FROM documents WHERE id=?',(doc,)).fetchone()
            self.papers.append({'id':f'P{n:02}','name':name,'sha256':row[0]})
        store.close()
        self.cases=[{'id':'R01','split':'dev','type':'single','task':'Read paper P01',
                     'paper_ids':['P01'],'gold_status':'provisional_needs_human_review',
                     'key_points':[{'id':'C01-P1','expected':'number 1','evidence':[]}]},
                    {'id':'R02','split':'holdout','type':'single','task':'Read P02',
                     'paper_ids':['P02'],'gold_status':'provisional_needs_human_review','key_points':[]},
                    {'id':'R03','split':'dev','type':'single','task':'Other',
                     'paper_ids':[f'P{n:02}' for n in range(3,26)],'gold_status':'provisional_needs_human_review','key_points':[]}]
        self.load=patch.object(runner,'load_data',return_value=({'corpus_id':'agent-test-paper-v1','papers':self.papers},self.cases))
        self.load.start();self.addCleanup(self.load.stop)

    def model(self):
        return ScriptedLLM([call('read_knowledge',{'document_id':1,'start_line':1,'end_line':1},'read'),
                            call('finish',{'answer':'number 1 [D1:L1]'},'finish')])

    def test_real_research_loop_is_isolated_and_does_not_expose_gold_or_network_tools(self):
        model=self.model()
        with contextlib.redirect_stdout(io.StringIO()):
            result=runner.evaluate('R01','single',lambda:model,self.root/'out',base=self.base,steps=4)
        self.assertEqual(result['case_outcome'],'pending')
        self.assertEqual(result['settings']['worker_steps'],6)
        self.assertEqual(len(result['base_database_sha256']),64)
        self.assertEqual(result['semantic_assessment'],'pending')
        self.assertEqual([j['status'] for j in result['judgments']],['pass','pass','pending'])
        self.assertTrue(Path(result['report_path']).is_file())
        sent=json.dumps(model.inputs,ensure_ascii=False)
        self.assertNotIn('C01-P1',sent)
        names={t['function']['name'] for t in model.inputs[0][1]}
        self.assertNotIn('read_webpage',names);self.assertNotIn('execute_python',names)
        self.assertIn('read_knowledge',names)
        self.assertNotIn('P02',result['visible_paper_ids'])
        isolated=Store(Path(result['workspace'])/'research.sqlite3')
        try:
            with self.assertRaises(ValueError):isolated.read_document(1,2,1,1)
            self.assertEqual(isolated.read_document(1,1,1,1)['excerpt'],'number 1 evidence')
        finally:isolated.close()
        base=Store(self.base)
        try:self.assertEqual(len(base.documents(1)),25)
        finally:base.close()

    def test_parallel_worker_receives_same_closed_toolset(self):
        model=ScriptedLLM([call('run_worker',{'task':'read','context':''},'root1'),
            call('read_knowledge',{'document_id':1,'start_line':1,'end_line':1},'child1'),
            call('finish',{'answer':'number 1 [D1:L1]'},'child2'),
            call('finish',{'answer':'number 1 [D1:L1]'},'root2')])
        with contextlib.redirect_stdout(io.StringIO()):
            result=runner.evaluate('R01','multi',lambda:model,self.root/'out',base=self.base,steps=4,worker_steps=4)
        child={t['function']['name'] for t in model.inputs[1][1]}
        self.assertNotIn('run_worker',child);self.assertNotIn('read_webpage',child)
        self.assertEqual(len(result['run']['workers']),1)
        self.assertEqual(result['settings']['max_workers'],3)

    def test_exam_defaults_and_three_worker_limit(self):
        fake={'case_outcome':'pending','semantic_assessment':'pending','report_path':str(self.root/'fake.json')}
        with patch.dict(os.environ,{'AGENT_API_KEY':'fake'},clear=True), \
             patch.object(exam_cli.agent,'load_env'), \
             patch.object(exam_cli,'evaluate',return_value=fake) as evaluate, \
             patch.object(exam_cli,'summary',return_value={}), \
             patch.object(exam_cli,'save_report',return_value=self.root/'summary.json'), \
             contextlib.redirect_stdout(io.StringIO()):
            status=exam_cli.main(['run','--case','R01','--mode','multi','--output',str(self.root/'out')])
        self.assertEqual(status,0)
        self.assertEqual((evaluate.call_args.kwargs['steps'], evaluate.call_args.kwargs['workers'],
                          evaluate.call_args.kwargs['worker_steps']), (10,3,6))
        with self.assertRaisesRegex(ValueError,'最多创建 3 个'):
            runner.evaluate('R01','multi',self.model,self.root/'invalid',base=self.base,workers=4)
        self.assertFalse((self.root/'invalid').exists())

    def test_review_is_explicit_immutable_and_unknown_never_becomes_pass(self):
        with contextlib.redirect_stdout(io.StringIO()):
            report=runner.evaluate('R01','single',self.model,self.root/'out',base=self.base,steps=4)
        original=Path(report['report_path']).read_bytes()
        form=review.template(report);form['reviewer']='reviewer';form['gold_reviewed']=True
        form['citation_support']='pass';form['citation_reason']='original confirms location'
        form['unsupported_claims']='none';form['unsupported_claims_reason']='checked entire answer'
        form['points'][0]['reason']='needs inspection'
        reviewfile=self.root/'review.json';reviewfile.write_text(json.dumps(form))
        pending=review.apply(report['report_path'],reviewfile,self.root/'reviews')
        self.assertEqual(pending['case_outcome'],'pending')
        form['points'][0].update(status='correct',reason='verified original')
        reviewfile.write_text(json.dumps(form));graded=review.apply(report['report_path'],reviewfile,self.root/'reviews')
        self.assertEqual(graded['case_outcome'],'pass')
        self.assertEqual(Path(report['report_path']).read_bytes(),original)
        with self.assertRaisesRegex(ValueError,'duplicate_run_report'):
            runner.summary([report,graded])
        other={**report,'run':{**report['run'],'run_id':'independent-run'}}
        summary=runner.summary([other,graded])['groups']['single']
        self.assertEqual(summary['pending'],1);self.assertIsNone(summary['all_case_pass_rate'])

    def test_tampered_base_and_persistent_memories_are_rejected(self):
        store=Store(self.base)
        with store.db:store.db.execute("UPDATE documents SET original=x'00' WHERE id=1")
        store.close()
        with self.assertRaisesRegex(ValueError,'base_blob_mismatch'):runner.check_base(self.base)

    def test_published_markdown_matches_authoritative_question_data(self):
        root=Path(documents.__file__).parent
        self.assertEqual((root/'QUESTIONS.md').read_text(),documents.render_questions())
        self.assertEqual((root/'ANSWER_KEY.md').read_text(),documents.render_key())
        self.assertEqual((root/'CORPUS.md').read_text(),documents.render_corpus())

    def test_pdf_extraction_warnings_are_bounded_and_classified(self):
        capture=dataset.WarningCapture()
        for message in ('Skipping broken line in mapping', 'Skipping broken line again',
                        'fontTools is required for this font',
                        'Exceeded 5000 form XObject invocations while extracting text'):
            capture.handle(logging.LogRecord('pypdf._page',logging.WARNING,'',1,message,(),None))
        self.assertEqual(capture.records,['pypdf_font_mapping_lines_skipped',
                                          'pypdf_fonttools_missing_encoding_may_be_incomplete',
                                          'pypdf_xobject_limit_content_may_be_skipped'])

    def test_prepare_builds_new_corpus_and_never_overwrites_it(self):
        source=self.root/'sources';source.mkdir()
        for n in range(1,26): (source/f'{n:02}.txt').write_text(f'number {n} evidence')
        target=self.root/'prepared.sqlite3'
        with patch.object(dataset,'load_data',return_value=({'corpus_id':'fixture','papers':self.papers},self.cases)), \
             patch.object(dataset,'validate',return_value={'valid':True}), contextlib.redirect_stdout(io.StringIO()):
            result=dataset.prepare(source,target)
            self.assertEqual(len(result['document_ids']),25)
            before=target.read_bytes()
            with self.assertRaisesRegex(ValueError,'exists'):dataset.prepare(source,target)
            self.assertEqual(target.read_bytes(),before)

    def test_independent_model_review_is_provisional_and_fabricated_quote_is_rejected(self):
        with contextlib.redirect_stdout(io.StringIO()):
            report=runner.evaluate('R01','single',self.model,self.root/'out',base=self.base,steps=4)
        verdict={'points':[{'id':'C01-P1','status':'correct','answer_quote':'number 1','reason':'一致'}],
                 'citation_support':'pass','citation_reason':'原文支持',
                 'unsupported_claims':'none','unsupported_claims_reason':'已检查'}
        data={'answer':'number 1 [D1:L1]', 'source_pages':[{'truncated':False}],
              'citations':[{'in_retrieved_range':True,'truncated':False}]}
        judge=ScriptedLLM([{'role':'assistant','content':json.dumps(verdict)}])
        with patch.object(model_judge,'inputs',return_value=data):
            result=model_judge.judge(report['report_path'],judge,self.root/'judged')
        self.assertEqual(result['model_review']['provisional_outcome'],'pass')
        self.assertEqual(result['case_outcome'],'pending')
        data['citations'][0]['in_retrieved_range'] = False
        judge=ScriptedLLM([{'role':'assistant','content':json.dumps(verdict)}])
        with patch.object(model_judge,'inputs',return_value=data):
            uncertain=model_judge.judge(report['report_path'],judge,self.root/'judged')
        self.assertEqual(uncertain['model_review']['provisional_outcome'],'unknown')
        data['citations'][0]['in_retrieved_range'] = True
        verdict['points'][0]['answer_quote']='从未出现'
        judge=ScriptedLLM([{'role':'assistant','content':json.dumps(verdict)}])
        with patch.object(model_judge,'inputs',return_value=data):
            invalid=model_judge.judge(report['report_path'],judge,self.root/'judged')
        self.assertEqual(invalid['model_review']['status'],'error')
        self.assertEqual(invalid['model_review']['provisional_outcome'],'unknown')

    def test_resume_task_accounts_for_both_runs_and_restores_previous_context(self):
        case=self.cases[0]
        case['continuation']={'initial_task':'Initial P01','follow_up':'Continue P01'}
        model=ScriptedLLM([call('read_knowledge',{'document_id':1,'start_line':1,'end_line':1},'read'),
                           call('finish',{'answer':'first [D1:L1]'},'f1'),
                           call('finish',{'answer':'number 1 [D1:L1]'},'f2')])
        with contextlib.redirect_stdout(io.StringIO()):
            result=runner.evaluate('R01','single',lambda:model,self.root/'out',base=self.base,steps=4)
        self.assertEqual(len(result['run']['continuation_stages']),2)
        self.assertEqual(result['run']['parent_run_id'],result['run']['continuation_stages'][0]['run_id'])
        sent=json.dumps(model.inputs[2][0],ensure_ascii=False)
        self.assertIn('Continue P01',sent);self.assertIn('first [D1:L1]',sent)
