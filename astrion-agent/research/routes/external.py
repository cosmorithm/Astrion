import json
from fastapi import APIRouter,Request
from pydantic import BaseModel,Field
from .dependencies import database
from tools.external import ExternalTools

router=APIRouter(prefix='/api/projects/{project_id}')
class SourceInput(BaseModel):url:str=Field(min_length=1,max_length=4000)

@router.get('/literature')
def literature(project_id:int,request:Request,query:str,cursor:str='*',limit:int=5):
    with database(request) as store:
        store.project(project_id)
        return json.loads(ExternalTools(store,project_id).search_literature(query,cursor,limit))

@router.get('/doi')
def doi(project_id:int,request:Request,doi:str,reference_offset:int=0):
    with database(request) as store:
        store.project(project_id)
        return json.loads(ExternalTools(store,project_id).doi_metadata(doi,reference_offset))

@router.post('/sources/import')
def source(project_id:int,request:Request,body:SourceInput):
    with database(request) as store:return json.loads(ExternalTools(store,project_id).read_webpage(body.url))
