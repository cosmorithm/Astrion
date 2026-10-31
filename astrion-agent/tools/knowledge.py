"""Knowledge tools: one declaration set and per-agent bound observations."""
import json
from tooling import ToolSpec
from research.citations import covers_range


def spec(name, description, properties, required=()):
    return ToolSpec(name, description, {'type':'object','properties':properties,'required':list(required),'additionalProperties':False},
                    output_description='JSON；来源、完整性与分页字段明确；错误由 ToolResult.error 返回')

S={'type':'string','minLength':1}; I={'type':'integer'}
SEARCH_DOCS=spec('search_knowledge','检索当前项目资料；中英文语义与关键词融合，offset 分页；降级状态见 retrieval。',
    {'query':S,'offset':I,'limit':I,'mode':{'type':'string','enum':['hybrid','keyword','semantic']}},['query'])
READ_DOC=spec('read_knowledge','按行读原文，含首尾最多 80 行；大行可用 read_document 的 offset 接续。',
    {'document_id':I,'start_line':I,'end_line':I},['document_id','start_line','end_line'])
READ_PART=spec('read_document','按页码或章节 ID 阅读；或按行范围；字符 offset 接续到 next_offset=null。保留质量警告。',
    {'document_id':I,'page':I,'section':I,'start_line':I,'end_line':I,'offset':I,'limit':I},['document_id'])
LIST_DOCS=spec('list_documents','列出当前项目资料和文档 ID，每页最多 20 份。',{'offset':I,'limit':I})
OUTLINE=spec('document_outline','浏览文档章节或页面目录，offset 分页。',{'document_id':I,'offset':I,'limit':I},['document_id'])
SEARCH_MEM=spec('search_memory','查询全局对话记忆和当前项目有效记忆。',{'query':S},['query'])
KNOWLEDGE_SPECS=[LIST_DOCS,OUTLINE,SEARCH_DOCS,READ_DOC,READ_PART,SEARCH_MEM]


def dump(data):
    text=json.dumps(data,ensure_ascii=False,allow_nan=False)
    if len(text)>24000: raise ValueError('tool_output_limit: reduce page size or read a narrower selection')
    return text


class KnowledgeTools:
    def __init__(self,store,project):self.store=store;self.project=project;self.seen=set()
    def observe(self,data):
        results=data['results'];ranges={(r['document_id'],r['start_line'],r['end_line']) for r in results}
        count=sum(not covers_range(self.seen,*span) for span in ranges)
        self.seen.update((r['document_id'],r['start_line'],r['end_line']) for r in results if not r['truncated'])
        return dump(data|{'new_ranges':count})
    def search_knowledge(self,query,offset=0,limit=5,mode='hybrid'):
        return self.observe(self.store.search(self.project,query,offset=offset,limit=limit,mode=mode))
    def read_knowledge(self,document_id,start_line,end_line):
        return self.observe({'results':[self.store.read_document(self.project,document_id,start_line,end_line)]})
    def read_document(self,document_id,page=0,section=0,start_line=1,end_line=0,offset=0,limit=4000):
        return self.observe({'results':[self.store.document_store.read_part(self.project,document_id,page=page,section=section,start_line=start_line,end_line=end_line,offset=offset,limit=limit)]})
    def document_outline(self,document_id,offset=0,limit=30):
        return dump(self.store.document_store.outline(self.project,document_id,offset,limit))
    def list_documents(self,offset=0,limit=20):
        if offset<0 or not 1<=limit<=20:raise ValueError('invalid_pagination')
        rows=self.store.document_store.listing(self.project,offset,limit+1)
        return dump({'documents':rows[:limit],'next_offset':offset+limit if len(rows)>limit else None})
    def search_memory(self,query):return dump({'results':self.store.search_memories(self.project,query)})
    def implementations(self):return {s.name:getattr(self,s.name) for s in KNOWLEDGE_SPECS}
