import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.long_research import runner, review, aggregate, model_review, fixed_compare, diagnostics
from research.store import Store


class LongResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.db = self.root/'base.sqlite'
        store = Store(self.db); project = store.create_project('base')
        self.papers = []
        for i in range(1, 4):
            name = f'paper{i}.txt'; doc = store.add_document_text(project,name,f'evidence {i}')
            self.papers.append({'id':f'P{i:02}', 'name':name})
        store.close()
        self.case = {'id':'LRX','papers':['P01','P02'], 'source_case_ids':['R01'],
                     'initial':'Write a report', 'updates':{'2':'Add a table','3':'Add limits'}}
        self.corpus = {'papers':self.papers}
        self.calls = []

    def fake_run(self, store, project, task, llm, **kwargs):
        self.calls.append((store.path, store.documents(project), task, kwargs))
        return {'run_id':'fake','result':{'status':'completed','answer':'answer [D1:L1]'},
                'observed_ranges':[(1,1,1)],'citation_audit':{'status':'locations_found',
                'checks':[{'in_retrieved_range':True}]},'usage':{'totals':{'total_tokens':10}},
                'trace':[{'role':'root','event':{'type':'user.message.added','step':2,'payload':{'content':'Add a table'}}}]}

    def test_paired_runs_are_isolated_and_gold_is_not_in_prompt(self):
        class LLM: model='fake';base_url='https://fake.invalid'
        def run(strategy):
            return runner.run_case(self.case,strategy,LLM,self.root/'out',base=self.db,
                                   max_steps=6,budget_chars=9000,input_budget_tokens=6000)
        with patch.object(runner,'check_base',return_value=(1,{'P01':1,'P02':2,'P03':3})), \
             patch.object(runner,'load_data',return_value=(self.corpus,[])), \
             patch.object(runner,'run_research',side_effect=self.fake_run):
            a,b=run('legacy'),run('ss')
        self.assertNotEqual(self.calls[0][0],self.calls[1][0])
        self.assertEqual([x['name'] for x in self.calls[0][1]],['paper1.txt','paper2.txt'])
        self.assertEqual(self.calls[0][2],self.calls[1][2])
        self.assertEqual(self.calls[0][3]['context_strategy'],'legacy')
        self.assertEqual(self.calls[1][3]['context_strategy'],'ss')
        self.assertEqual(self.calls[0][3]['scheduled_user_updates'],{2:'Add a table',3:'Add limits'})
        self.assertEqual(self.calls[0][3]['max_workers'],0)
        self.assertNotIn('run_worker',self.calls[0][3]['tool_allowlist'])
        self.assertNotIn('R01',self.calls[0][2])
        self.assertEqual(a['case_outcome'],'pending_human_review')
        self.assertFalse(a['metrics']['all_updates_delivered'])
        self.assertEqual(a['metrics']['papers_with_observed_ranges'],['P01'])
        self.assertTrue(Path(b['report_path']).exists())
        form = review.template(a['report_path'])
        self.assertEqual(form['gold_status'], 'provisional_needs_human_review')
        self.assertTrue(all(p['status'] == 'pending' for p in form['points']))
        self.assertNotIn(form['points'][0]['expected_draft'], self.calls[0][2])
        original=Store(self.db)
        try:self.assertEqual(len(original.documents(1)),3)
        finally:original.close()

    def test_aggregate_does_not_turn_unreviewed_results_into_pass_rate(self):
        manifest = self.root/'suite.json'
        manifest.write_text(json.dumps({'groups':[{'case_id':'LRX','strategy':'ss','status':'completed',
            'usage':{'totals':{'total_tokens':12},'unknown_calls':0},'metrics':{}}]}))
        data = aggregate.aggregate([manifest])
        self.assertIsNone(data['strategies']['ss']['semantic_pass_rate'])
        self.assertEqual(data['strategies']['ss']['possibly_incomplete_usage_runs'],1)
        with self.assertRaisesRegex(ValueError, 'duplicate_case_strategy'):
            aggregate.aggregate([manifest,manifest])
        selected=aggregate.aggregate([manifest,manifest],prefer_last=True)
        self.assertEqual(len(selected['groups']),1)
        self.assertEqual(len(selected['superseded_attempts']),1)

    def test_legacy_research_rejects_uncontrolled_workers(self):
        from research.runner import run_research
        store = Store(self.db)
        try:
            with self.assertRaisesRegex(ValueError, 'legacy_strategy_requires_single_agent'):
                run_research(store, 1, 'task', object(), context_strategy='legacy', max_workers=1)
        finally:
            store.close()

    def test_reference_judge_is_provisional_and_does_not_upgrade_missing_citations(self):
        report = self.root/'report.json'
        report.write_text(json.dumps({'kind':'long_research_eval','case_id':'LRX','strategy':'ss','model':'fake'}))
        data={'answer':'alpha','key_points':[{'id':'p1'}],'source_pages':[], 'citations':[]}
        class Judge:
            model='fake';usage_totals={};usage_calls=0;usage_unknown=0
            def chat(self,messages,tools=None):
                return {'content':json.dumps({'points':[{'id':'p1','status':'correct','answer_quote':'alpha','reason':'present'}],
                    'citation_support':'pass','citation_reason':'yes','unsupported_claims':'none','unsupported_claims_reason':'checked'})}
        from tests.context_counter import CodepointCounter
        with patch.object(model_review,'build_inputs',return_value=data),              patch.object(model_review,'load_token_counter',return_value=CodepointCounter()):
            judged=model_review.judge(report,Judge(),self.root/'judge')
        self.assertEqual(judged['provisional_outcome'],'fail')
        self.assertTrue(judged['same_model_as_agent'])
        self.assertEqual(judged['gold_status'],'provisional_needs_human_review')
        self.assertEqual(judged['verdict']['citation_support'],'fail')

    def test_fixed_review_uses_fixed_answer_and_prefix_workspace(self):
        source=self.root/'source.json'
        source.write_text(json.dumps({'kind':'long_research_eval','case_id':'LRX',
            'strategy':'legacy','model':'fake','workspace':'source',
            'run':{'task':'task','result':{'answer':'source answer'}}}))
        fixed=self.root/'fixed.json'
        fixed.write_text(json.dumps({'kind':'long_research_fixed_history','source_case_id':'LRX',
            'source_report':str(source),'strategy':'ss','workspace':'frozen',
            'result':{'answer':'fixed answer'},'citation_audit':{'checks':[]}}))
        data={'answer':'fixed answer','key_points':[{'id':'p1'}],'source_pages':[], 'citations':[]}
        class Judge:
            model='fake';usage_totals={};usage_calls=0;usage_unknown=0
            def chat(self,messages,tools=None):
                return {'content':json.dumps({'points':[{'id':'p1','status':'correct','answer_quote':'fixed answer','reason':'present'}],
                    'citation_support':'pass','citation_reason':'yes','unsupported_claims':'none','unsupported_claims_reason':'checked'})}
        from tests.context_counter import CodepointCounter
        with patch.object(model_review,'build_inputs',return_value=data) as build,              patch.object(model_review,'load_token_counter',return_value=CodepointCounter()):
            model_review.judge(fixed,Judge(),self.root/'fixed_judge')
        used=build.call_args.args[0]
        self.assertEqual(used['run']['result']['answer'],'fixed answer')
        self.assertEqual(used['workspace'],'frozen')
        self.assertEqual(used['strategy'],'ss')

    def test_review_template_accepts_fixed_report_without_automatic_verdict(self):
        source=self.root/'source_for_review.json'
        source.write_text(json.dumps({'kind':'long_research_eval','case_id':'LRX',
            'strategy':'legacy','source_case_ids':['R01']}))
        fixed=self.root/'fixed_for_review.json'
        fixed.write_text(json.dumps({'kind':'long_research_fixed_history','source_case_id':'LRX',
            'source_report':str(source),'strategy':'ss'}))
        form=review.template(fixed)
        self.assertEqual(form['strategy'],'ss')
        self.assertTrue(all(item['status']=='pending' for item in form['points']))
        self.assertFalse(form['gold_reviewed'])

    def test_fixed_compare_refuses_mismatched_prefix(self):
        def item(strategy,prefix):
            return {'strategy':strategy,'prefix_sha256':prefix,'source_prefix_steps':10,
                    'raw_local_tokens':100,'input_budget_tokens':200}
        old=self.root/'old.json';new=self.root/'new.json'
        old.write_text(json.dumps({'groups':[item('legacy','x')]}))
        new.write_text(json.dumps({'groups':[item('ss','y')]}))
        with self.assertRaisesRegex(ValueError,'fixed_prefix_mismatch'):
            fixed_compare.compare(old,new)
        new.write_text(json.dumps({'groups':[item('ss','x')]}))
        self.assertEqual(len(fixed_compare.compare(old,new)['groups']),2)

    def test_diagnostic_distinguishes_archived_previews_from_direct_source(self):
        path=self.root/'events.jsonl'
        history=[{'role':'tool','content':json.dumps({'results':[{'document_id':1,'truncated':False}]})},
                 {'role':'tool','content':json.dumps({'preview':'partial document 2','archived':True})}]
        event={'type':'model.call.started','step':4,'payload':{'purpose':'decision','request_snapshot':{
            'messages':[{'role':'user','content':'update'},
                        {'role':'user','content':'history\n'+json.dumps({'execution_history':history})}]}}}
        path.write_text(json.dumps({'event':event})+'\n')
        report={'case_id':'LRX','strategy':'ss','paper_mapping':{'P01':1,'P02':2},
                'settings':{'scheduled_user_updates':{'3':'update'}},
                'run':{'event_log':str(path),'observed_ranges':[[1,1,2],[2,1,2]],
                       'result':{'answer':'fact [D1:L1]'}}}
        data=diagnostics.inspect(report)
        self.assertEqual(data['observed_papers'],['P01','P02'])
        self.assertEqual(data['direct_untruncated_tool_papers_in_last_input'],['P01'])
        self.assertEqual(data['cited_papers'],['P01'])
        self.assertEqual(data['scheduled_updates_in_last_input'],[3])

    def test_actual_cases_match_sources(self):
        real=runner.cases()
        self.assertEqual(len(real),3)
        self.assertTrue(all(len(c['papers'])==6 for c in real))

class FixedHistoryTests(unittest.TestCase):
    def test_prefix_excludes_future_and_is_identical_across_isolated_groups(self):
        from context_history import AgentHistory
        from context_store import ContextStore
        from memory import Memory
        from evaluation.long_research.fixed_history import prepare_prefix
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir()
            store=Store(source/'research.sqlite3');store.create_project('base');store.close()
            archive=ContextStore(source/'context.sqlite3');memory=Memory('system',archive=archive)
            memory.add_user('original task')
            history=AgentHistory(archive,'run')
            for step in range(1,4):
                if step==2:memory.add_user('updated instruction')
                history.begin_step(step)
                call_id=f'call{step}'
                memory.add_assistant({'content':None,'tool_calls':[{'id':call_id,'type':'function',
                    'function':{'name':'read_knowledge','arguments':'{}'}}]})
                body=json.dumps({'results':[{'document_id':step,'start_line':1,'end_line':2,'truncated':False}],
                                 'secret':'future-only' if step==3 else 'past'})
                memory.add_tool(call_id,body)
                history.record({'type':'tool.call.finished','event_id':f'e{step}','sequence':step,
                    'payload':{'name':'read_knowledge','outcome':'success','content':body}})
                history.end_step()
            memory.checkpoint(task='original task')
            report={'workspace':str(source),'run':{'context_archive':{'path':archive.path,'scope':archive.scope}}}
            groups=[]
            for strategy in ('legacy','ss'):
                path=root/strategy;path.mkdir()
                item,n,cutoff,ranges=prepare_prefix(report,path,2)
                self.assertEqual(n,2);self.assertEqual(ranges,[(1,1,2),(2,1,2)])
                self.assertNotIn('future-only',json.dumps(item.archive.snapshot()))
                with item.archive.connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM context_messages WHERE scope<>?', (item.archive.scope,)).fetchone()[0],0)
                self.assertIn('updated instruction',json.dumps(item.for_model()))
                self.assertEqual(item.check(),[])
                groups.append(item)
            self.assertNotEqual(groups[0].archive.scope,groups[1].archive.scope)
            self.assertEqual(groups[0].for_model(),groups[1].for_model())
            self.assertEqual(groups[0].archive.records,groups[1].archive.records)


if __name__ == '__main__':
    unittest.main()
