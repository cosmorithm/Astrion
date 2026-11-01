"""DOI resolution across Crossref and DataCite, without changing tool contracts."""
from urllib.parse import quote
from .literature import Crossref,plain
from .http import PublicHTTP,ServiceError


class LiteratureCatalog:
    def __init__(self,crossref=None,http=None):
        self.crossref=crossref or Crossref();self.http=http or PublicHTTP(timeout=25,max_bytes=5_000_000)
    def search(self,*args,**kwargs):return self.crossref.search(*args,**kwargs)
    def metadata(self,doi,reference_offset=0,limit=20):
        normalized=doi.removeprefix('https://doi.org/').strip()
        try:return self.crossref.metadata(normalized,reference_offset,limit)
        except ServiceError as exc:
            if exc.code!='http_error' or 'HTTP 404' not in str(exc):raise
        data=self.http.json('https://api.datacite.org/dois/'+quote(normalized,safe=''))
        try:
            a=data['data']['attributes']
            abstract='\n'.join(plain(x.get('description')) for x in a.get('descriptions',[]) if x.get('descriptionType')=='Abstract')
            refs=[{'identifier':x.get('relatedIdentifier'),'type':x.get('relatedIdentifierType'),'relation':x.get('relationType')} for x in a.get('relatedIdentifiers',[]) if x.get('relationType') in {'References','Cites','IsSupplementTo'}]
            return {'title':a['titles'][0]['title'],'authors':[x.get('name','') for x in a.get('creators',[])][:30],
                'year':a.get('publicationYear'),'doi':a.get('doi',normalized),'url':a.get('url'),
                'source':'DataCite','content_kind':'metadata','abstract':abstract[:4000],'abstract_truncated':len(abstract)>4000,
                'full_text_read':False,'links':[],'references':refs[reference_offset:reference_offset+limit],
                'reference_count':len(refs),'next_reference_offset':reference_offset+limit if reference_offset+limit<len(refs) else None}
        except (KeyError,TypeError,IndexError) as exc:raise ServiceError('invalid_response','missing DataCite attributes') from exc
