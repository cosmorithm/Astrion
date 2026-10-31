from fastapi import APIRouter, Request, UploadFile, File, Form
from .dependencies import database

router=APIRouter(prefix='/api/projects/{project_id}')

@router.post('/documents/upload')
def upload(project_id:int, request:Request, file:UploadFile=File(...), ocr:bool=Form(False)):
    data=file.file.read(20_000_001)
    with database(request) as store:
        doc=store.import_document(project_id,file.filename or '',data,ocr=ocr)
        return {'id':doc,'outline':store.document_store.outline(project_id,doc)}

@router.get('/documents/{document_id}/outline')
def outline(project_id:int,document_id:int,request:Request,offset:int=0,limit:int=30):
    with database(request) as store:return store.document_store.outline(project_id,document_id,offset,limit)

@router.get('/documents/{document_id}/read')
def read(project_id:int,document_id:int,request:Request,page:int=0,section:int=0,start_line:int=1,end_line:int=0,offset:int=0,limit:int=4000):
    with database(request) as store:
        return store.document_store.read_part(project_id,document_id,page=page,section=section,start_line=start_line,end_line=end_line,offset=offset,limit=limit)

@router.get('/search')
def search(project_id:int,request:Request,query:str,offset:int=0,limit:int=5,mode:str='hybrid'):
    with database(request) as store:return store.search(project_id,query,offset=offset,limit=limit,mode=mode)
