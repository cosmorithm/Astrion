"""External evidence capability definitions; provider choice stays outside the loop."""
from .knowledge import spec,S,I,dump
from research.providers.literature import Crossref
from research.providers.doi import LiteratureCatalog
from research.providers.web import WebReader

SEARCH=spec('search_literature','搜索文献元数据（不是已读全文），返回标题、作者、年份、DOI/URL；cursor 分页。',{'query':S,'cursor':S,'limit':I},['query'])
DOI=spec('doi_metadata','获取 DOI 元数据、论文摘要及参考文献分页；不代表阅读全文。',{'doi':S,'reference_offset':I,'limit':I},['doi'])
WEB=spec('read_webpage','获取公开网页或 PDF 并存入当前项目，保留访问时间和快照；只返回首段，余下用 read_document 接续。',{'url':S},['url'])
EXTERNAL_SPECS=[SEARCH,DOI,WEB]


class ExternalTools:
    def __init__(self,store,project,literature=None,web=None):
        self.store=store;self.project=project;self.literature=literature or LiteratureCatalog();self.web=web or WebReader(store)
    def search_literature(self,query,cursor='*',limit=5):return dump(self.literature.search(query,cursor,limit))
    def doi_metadata(self,doi,reference_offset=0,limit=20):return dump(self.literature.metadata(doi,reference_offset,limit))
    def read_webpage(self,url):return dump(self.web.read(self.project,url))
    def implementations(self):return {s.name:getattr(self,s.name) for s in EXTERNAL_SPECS}
