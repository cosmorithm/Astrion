"""Per-run event journal and result; independent of the model context archive."""
import json
from datetime import datetime, timezone
from uuid import uuid4


class RunLog:
    def __init__(self,store,project):
        self.run_id='research_'+uuid4().hex
        self.directory=store.path.parent/'runs'/str(project)/self.run_id
        self.directory.mkdir(parents=True)
        self.path=self.directory/'events.jsonl'
    def append(self,item):
        with self.path.open('a',encoding='utf-8') as f:
            f.write(json.dumps(item,ensure_ascii=False,allow_nan=False)+'\n')
    def finish(self,result):
        payload={k:v for k,v in result.items() if k!='trace'}
        payload['recorded_at']=datetime.now(timezone.utc).isoformat()
        (self.directory/'result.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
