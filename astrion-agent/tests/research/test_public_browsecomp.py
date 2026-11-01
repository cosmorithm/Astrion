import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from evaluation.public_research import browsecomp, scholarqa
from research.store import Store
from test_research import ScriptedLLM, call


class PublicBrowseCompTests(unittest.TestCase):
    def test_isolated_run_uses_full_index_and_does_not_leak_gold(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); c=Store(root/'corpus.sqlite3');project=c.create_project('corpus')
            c.add_document_text(project,'one.txt','irrelevant data')
            c.add_document_text(project,'two.txt','the answer is blue and recorded here')
            c.db.execute("CREATE VIRTUAL TABLE browse_fts USING fts5(content, content='documents', content_rowid='id')")
            c.db.execute("INSERT INTO browse_fts(browse_fts) VALUES ('rebuild')");c.db.execute('CREATE TABLE benchmark_metadata(key TEXT PRIMARY KEY,value TEXT)');c.db.execute('INSERT INTO benchmark_metadata VALUES (?,?)',('corpus_revision',browsecomp.REVISION));c.db.commit();c.close()
            model=ScriptedLLM([call('search_knowledge',{'query':'blue','mode':'keyword'},'s'),
                               call('finish',{'answer':'blue [D2:L1]'},'f')])
            question={'id':'1','question':'What is the color?','answer':'GOLD_SECRET'}
            with contextlib.redirect_stdout(io.StringIO()):
                path, report=browsecomp.run(root/'corpus.sqlite3',[question],root/'out',lambda:model,steps=4)
            self.assertEqual(report['cases'][0]['status'],'completed')
            self.assertEqual(report['cases'][0]['citation_audit']['status'],'locations_found')
            self.assertNotIn('GOLD_SECRET',json.dumps(model.inputs))
            self.assertEqual(json.loads((path/'private_gold.json').read_text())[0]['answer'],'GOLD_SECRET')
            self.assertIn('pending',report['scoring'])

    def test_unretrieved_or_invalid_query_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);s=Store(root/'corpus');s.create_project('corpus')
            s.db.execute("CREATE VIRTUAL TABLE browse_fts USING fts5(content, content='documents', content_rowid='id')");s.close()
            c=browsecomp.CorpusStore(root/'run',root/'corpus');c.create_project('run')
            try:
                with self.assertRaises(ValueError): c.search(1,'',mode='keyword')
                self.assertEqual(c.search(1,'absent',mode='keyword')['results'],[])
            finally:c.close()

    def test_scholar_adapter_sends_only_prompt_and_records_real_research(self):
        with tempfile.TemporaryDirectory() as temp:
            model=ScriptedLLM([call('finish',{'answer':'{"sections": []}'},'f')])
            path, result=scholarqa.execute('Research question, without rubric',lambda:model,temp,steps=3,workers=0)
            self.assertTrue((path/'summary.json').is_file())
            self.assertEqual(result['result']['result']['status'],'completed')
            self.assertIn('pending',result['score'])

if __name__=='__main__':unittest.main()
