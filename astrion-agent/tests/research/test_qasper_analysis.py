import json
import tempfile
import unittest
from pathlib import Path
from evaluation.public_research import qasper_analysis as qa
from research.store import Store


def paper_and_question():
    paper={'title':'t','abstract':'a','full_text':[{'section_name':'s','paragraphs':['evidence one','evidence two','other']}],
           'figures_and_tables':[]}
    question={'question':'question','answers':[{'answer':{'unanswerable':False,'evidence':['evidence one','evidence two']}}]}
    return paper,question


def event(name,excerpts):
    return {'role':'root','event':{'type':'tool.call.finished','payload':{'name':name,'outcome':'success',
            'content':json.dumps({'results':[{'document_id':1,'excerpt':x} for x in excerpts]})}}}


def finished(): return {'role':'root','event':{'type':'run.finished'}}


class Judge:
    def __init__(self,verdict):
        self.verdict=verdict;self.model='fixture';self.usage_totals={'total_tokens':0};self.usage_calls=0;self.usage_unknown=0
    def chat(self,messages,tools=None):
        self.usage_totals['total_tokens']+=12;self.usage_calls+=1
        return {'content':json.dumps(self.verdict)}


class AnalysisTests(unittest.TestCase):
    def test_first_search_top_k_and_cumulative_reads_are_separate(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);logs=p/'runs'/'1'/'id';logs.mkdir(parents=True)
            events=[event('search_knowledge',['other','evidence one','other','other','other','evidence two']),
                    event('search_knowledge',['evidence two']),event('read_knowledge',['evidence two']),finished()]
            (logs/'events.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in events))
            paper,question=paper_and_question();r=qa.retrieval({'workspace':str(p)},paper,question,k=5)
            self.assertEqual(r['first_search_recall_at_k'],.5)
            self.assertEqual(r['all_search_recall'],1.)
            self.assertEqual(r['all_observed_recall'],1.)
            self.assertEqual(r['search_calls'],2)
            # A partial snippet does not count as observing a complete gold paragraph.
            self.assertEqual(qa.match_recall([{'evidence one'}],['evidence on']),0.)
            long_para='a'*100+'b'*100
            self.assertEqual(qa.match_recall([{long_para}],['a'*80]),0.)
            self.assertEqual(qa.partial_match_recall([{long_para}],['a'*80]),1.)

    def test_failed_first_search_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            logs=Path(t)/'runs'/'1'/'id';logs.mkdir(parents=True)
            failed={'role':'root','event':{'type':'tool.call.finished','payload':{'name':'search_knowledge','outcome':'error','content':'error'}}}
            events=[failed,event('search_knowledge',['evidence one','evidence two']),finished()]
            (logs/'events.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in events))
            paper,question=paper_and_question();r=qa.retrieval({'workspace':t},paper,question)
            self.assertEqual(r['first_search_recall_at_k'],0.)
            self.assertEqual(r['all_search_recall'],1.)
            self.assertEqual(r['failed_search_calls'],1)

    def test_missing_table_and_disputed_unanswerable_excluded(self):
        paper,q=paper_and_question();q['answers'][0]['answer']['evidence'].append('FLOAT SELECTED: table')
        self.assertEqual(qa.eligible_gold(paper,q)[1],'no_complete_text_reference')
        q['answers'].append({'answer':{'unanswerable':True,'evidence':[]}})
        self.assertEqual(qa.eligible_gold(paper,q)[1],'unanswerable_or_disputed')

    def test_judge_requires_complete_observed_citations_and_valid_answer_quote(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);s=Store(p/'research.sqlite3');project=s.create_project('isolated')
            s.add_document_text(project,'a.txt','evidence one');s.close()
            case={'workspace':str(p),'answer':'The value is one [D1:L1]',
                  'citation_audit':{'checks':[{'citation':'[D1:L1-L1]','in_retrieved_range':True}]}}
            model=Judge({'verdict':'supported','answer_quote':'The value is one','reason':'supported'})
            result=qa.judge(case,'question',model)
            self.assertEqual(result['status'],'supported');self.assertEqual(result['usage']['totals']['total_tokens'],12)
            guarded=qa.support_guard({'status':'supported'},'Unanswerable [D1:L1]')
            self.assertEqual(guarded['status'],'unknown')
            self.assertEqual(guarded['model_verdict_before_guard'],'supported')
            invalid=Judge({'verdict':'supported','answer_quote':'not in answer','reason':'wrong'})
            self.assertEqual(qa.judge(case,'question',invalid)['status'],'unknown')
            case['citation_audit']['checks'][0]['in_retrieved_range']=False
            unused=Judge({});result=qa.judge(case,'question',unused)
            self.assertEqual(result['reason'],'citation_not_fully_observed');self.assertEqual(unused.usage_calls,0)

    def test_reuse_requires_identical_answer_sources_question_and_model(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); suite=root/'suite'; suite.mkdir(); work=suite/'case';work.mkdir()
            store=Store(work/'research.sqlite3');project=store.create_project('test')
            store.add_document_text(project,'p.txt','evidence one');store.close()
            logs=work/'runs'/'1'/'id';logs.mkdir(parents=True);(logs/'events.jsonl').write_text(json.dumps(event('search_knowledge',['evidence one']))+'\n'+json.dumps(finished())+'\n')
            paper,question=paper_and_question();question['question_id']='q1'
            (suite/'gold.json').write_text(json.dumps({'p':{**paper,'qas':[question]}}))
            case={'question_id':'q1','paper_id':'p','workspace':str(work),'answer':'answer [D1:L1]',
                  'citation_audit':{'checks':[{'citation':'[D1:L1-L1]','in_retrieved_range':True}]}}
            merged={'benchmark':'QASPER','source_suites':[str(suite)],'cases':[case],'model':'example',
                    'dataset_sha256':'data','settings':{},'metrics':{}}
            source=root/'summary.json';source.write_text(json.dumps(merged))
            review=Judge({'verdict':'supported','answer_quote':'answer','reason':'grounded'})
            first,report=qa.analyze(source,root/'out',judge_factory=lambda:review)
            self.assertEqual(review.usage_calls,1)
            second_review=Judge({'verdict':'unknown','answer_quote':'','reason':'test'})
            _,again=qa.analyze(source,root/'out',judge_factory=lambda:second_review,reuse=first/'summary.json')
            self.assertEqual(second_review.usage_calls,0)
            self.assertEqual(again['statistics']['support_reviews_reused'],1)
            merged['cases'][0]['answer']='changed [D1:L1]';source.write_text(json.dumps(merged))
            qa.analyze(source,root/'out',judge_factory=lambda:second_review,reuse=first/'summary.json')
            self.assertEqual(second_review.usage_calls,1)

    def test_missing_or_corrupt_journal_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as t:
            paper,q=paper_and_question();case={'workspace':t}
            self.assertEqual(qa.retrieval(case,paper,q)['status'],'unknown')
            logs=Path(t)/'runs'/'1'/'id';logs.mkdir(parents=True);(logs/'events.jsonl').write_text('bad json')
            self.assertEqual(qa.retrieval(case,paper,q)['reason'],'invalid_journal')

if __name__=='__main__':unittest.main()
