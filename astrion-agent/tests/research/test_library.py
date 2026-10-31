import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from research.store import Store
from tooling import ToolRegistry,ToolCall
from tools.research import build_tools
from memory_evidence import extract_tool_evidence

FIXTURES=Path(__file__).resolve().parents[2]/'fixtures/research'

class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'db.sqlite');self.addCleanup(self.store.close)
        self.a=self.store.create_project('A');self.b=self.store.create_project('B')
    def test_pdf_pages_sections_scan_quality_and_source_archive(self):
        doc=self.store.add_document(self.a,FIXTURES/'reading_fixture.pdf')
        outline=self.store.document_store.outline(self.a,doc)
        self.assertEqual(outline['page_count'],3);self.assertTrue(outline['items'])
        result=self.store.document_store.read_part(self.a,doc,page=1)
        self.assertIn('Retrieval augmented',result['excerpt']);self.assertEqual(result['pages'][0]['page'],1)
        scan=self.store.document_store.read_part(self.a,doc,page=3)
        self.assertIn('missing_text',scan['pages'][0]['warnings'])
        with self.assertRaises(ValueError):self.store.document_store.read_part(self.b,doc,page=1)
        evidence=extract_tool_evidence('read_document',json.dumps({'results':[result]}))
        self.assertEqual(evidence[0]['pages'][0]['page'],1)
        raw=self.store.document_store.get(self.a,doc)
        self.assertEqual(raw['original'],(FIXTURES/'reading_fixture.pdf').read_bytes())
    def test_long_line_and_section_can_be_read_without_losing_text(self):
        text='# Method\n'+('连续原文 abc ' * 1200)+'\n# End\nend'
        doc=self.store.add_document_text(self.a,'long.md',text)
        offset=0;parts=[]
        while True:
            page=self.store.document_store.read_part(self.a,doc,section=1,offset=offset,limit=600)
            parts.append(page['excerpt']);offset=page['next_offset']
            if offset is None:break
        self.assertEqual(''.join(parts),text.split('\n# End')[0])
    def test_search_pagination_and_duplicate_document_collapse(self):
        body='\n'.join(f'agent evidence line {i}' for i in range(70))
        self.store.add_document_text(self.a,'one.txt',body);self.store.add_document_text(self.a,'duplicate.txt',body)
        self.store.add_document_text(self.b,'private.txt','agent private')
        result=self.store.search(self.a,'agent',limit=2,mode='keyword')
        self.assertEqual(result['total'],7);self.assertEqual(result['next_offset'],2)
        second=self.store.search(self.a,'agent',limit=2,offset=2,mode='keyword')
        self.assertNotEqual(result['results'][0]['start_line'],second['results'][0]['start_line'])
    def test_semantic_adapter_fusion_and_explicit_unavailability(self):
        self.store.add_document(self.a,FIXTURES/'rag_en.md');self.store.add_document(self.a,FIXTURES/'agents_zh.md')
        class Encoder:
            name='test-double'
            def encode(self,texts,query=False):return [[1.,0.] if query or 'Retrieval' in t else [0.,1.] for t in texts]
        from research.retrieval.search import search
        result=search(self.store,self.a,'如何用检索到的文献生成答案',mode='semantic',encoder=Encoder())
        self.assertEqual(result['results'][0]['name'],'rag_en.md')
        # This checks adapter wiring only. Real multilingual quality has a separate live acceptance.
        from unittest.mock import patch
        with patch('research.retrieval.search.from_env',side_effect=RuntimeError('offline')):
            result=self.store.search(self.a,'Retrieval')
        self.assertEqual(result['retrieval']['semantic'],'unavailable');self.assertTrue(result['results'])
    def test_old_database_migration_keeps_ids_and_text(self):
        path=Path(self.tmp.name)/'old.db'
        db=sqlite3.connect(path)
        db.executescript("CREATE TABLE documents(id INTEGER PRIMARY KEY,project_id INTEGER,name TEXT,source TEXT,sha256 TEXT,content TEXT,created_at TEXT); INSERT INTO documents VALUES(9,1,'old.txt','local','hash','unchanged','old');")
        db.close();old=Store(path)
        try:self.assertEqual(old.db.execute('SELECT id,content FROM documents').fetchone()[:],(9,'unchanged'))
        finally:old.close()
    def test_main_and_child_share_definitions_and_plan_is_wired(self):
        for worker in (False,True):
            specs,impl=build_tools(self.store,self.a,worker=worker,delegate=lambda **_: '{}')
            # Control implementation is bound by the loop.
            registry=ToolRegistry(specs,impl|{'update_task_plan':lambda items:'{}'})
            self.assertIn('read_document',registry.specs);self.assertIn('update_task_plan',registry.specs)
            self.assertEqual('run_worker' in registry.specs,not worker)
            result=registry.execute(ToolCall('a','list_documents','{}'))
            self.assertEqual(result.outcome,'success')
    def test_invalid_pdf_and_out_of_range_are_explicit(self):
        with self.assertRaises(ValueError):self.store.import_document(self.a,'broken.pdf',b'broken')
        doc=self.store.add_document_text(self.a,'x.txt','line')
        with self.assertRaises(ValueError):self.store.document_store.read_part(self.a,doc,page=8)
