import json
import unittest
from unittest.mock import patch
from memory import Memory
from context_store import ContextStore

from research import claim_autoregister as mod

class Counter:
 def count_request(self,messages,tools): return len(json.dumps(messages, ensure_ascii=False)) // 4

class Judge:
 model='mock'
 def __init__(self, mode='valid'):
  self.mode=mode
  self.usage_totals={'total_tokens': 0}
  self.usage_calls=0
  self.usage_unknown=0
 def chat(self,messages,tools):
  self.usage_totals['total_tokens']+=100
  self.usage_calls+=1
  data=json.loads(messages[1]['content'])
  ref=data['observations'][0]['ref']
  if self.mode=='foreign': ref='fake'
  statement='The sample was 128.' if self.mode!='statement' else 'The sample was 129.'
  quote='Sample was 128.' if self.mode!='quote' else 'fake quote'
  if self.mode=='oversize': return {'content':'x'*20001}
  return {'content':json.dumps({'claims':[{'statement':statement,'sources':[{'ref':ref,'quote':quote}]}]})}

class TestRegistration(unittest.TestCase):
 def setUp(self):
  self.memory=Memory('system',archive=ContextStore())
  self.ref=self.memory.archive.put('document','Sample was 128.',{'document_id':3,'name':'p.pdf','sha256':'a'*64,'start_line':1,'end_line':1,'truncated':False})
  self.answer='The sample was 128. [D3:L1-L1]'
 def run_it(self,mode='valid',budget=4096):
  with patch.object(mod,'load_token_counter',return_value=Counter()):
   return mod.register_report_claims(self.memory,self.answer,Judge(mode),input_budget_tokens=budget)
 def test_valid(self):
  out=self.run_it(); self.assertEqual(out['status'],'processed');self.assertEqual(len(out['registered_claim_ids']),1);self.assertEqual(out['usage']['totals']['total_tokens'],100); self.assertEqual(out['coverage'],'unknown')
 def test_rejects_foreign_forged_or_unmatched(self):
  for mode in ['foreign','statement','quote','oversize']:
   with self.subTest(mode=mode):
    out=self.run_it(mode);self.assertTrue(out['errors']);self.assertEqual(len(self.memory._claim_ids),0)
 def test_no_budget_overrun(self):
  out=self.run_it(budget=10); self.assertTrue(out['errors']);self.assertEqual(out['usage']['calls'],0)
 def test_skips_truncated_documents(self):
  self.memory.archive.records.clear()
  self.memory.archive.put('document','Sample was 128.',{'document_id':3,'truncated':True})
  out=self.run_it();self.assertEqual(out['status'],'no_eligible_observations');self.assertEqual(out['usage']['calls'],0)
 def test_prioritizes_observation_matching_report_citation(self):
  for line in range(2,10):
   self.memory.archive.put('document',f'Other line {line}.',{'document_id':3,'name':'p.pdf',
     'sha256':'a'*64,'start_line':line,'end_line':line,'truncated':False})
  out=self.run_it();self.assertEqual(len(out['registered_claim_ids']),1)
  self.assertFalse(out['errors'])
class TestRealTokenization(unittest.TestCase):
 def setUp(self):
  self.memory=Memory('system',archive=ContextStore())
  self.ref=self.memory.archive.put('document','Sample was 128.',{'document_id':3,'name':'p.pdf','sha256':'a'*64,'start_line':1,'end_line':1,'truncated':False})
  self.answer='The sample was 128. [D3:L1-L1]'
 def test_unavailable_tokenizer_is_fail_closed(self):
  judge=Judge()
  with patch.object(mod,'load_token_counter',side_effect=ValueError('unavailable')):
   result=mod.register_report_claims(self.memory,self.answer,judge)
  self.assertEqual(result['status'],'failed')
  self.assertEqual(result['usage']['calls'],0)
  self.assertFalse(self.memory._claim_ids)
 def test_actual_counter_budget_boundary(self):
  try:
   counter=mod.load_token_counter('deepseek-v4-flash')
  except Exception as exc:
   self.skipTest(str(exc))
  obs={'ref':self.ref,'document_id':3,'start_line':1,'end_line':1,'content':'Sample was 128.'}
  exact=counter.count_request(mod._messages(self.answer,[obs]),[])
  for budget,expected_calls in [(exact-1,0),(exact,1)]:
   with self.subTest(budget=budget),patch.dict('os.environ', {'ASTRION_CONTEXT_INPUT_BUDGET_TOKENS':''}):
    judge=Judge();judge.model='deepseek-v4-flash';result=mod.register_report_claims(self.memory,self.answer,judge,input_budget_tokens=budget)
    self.assertEqual(result['usage']['calls'],expected_calls)
    if expected_calls:
     self.assertEqual(result['batches'][0]['input_tokens'],exact)
 def test_environment_budget_cannot_be_raised_by_caller(self):
  judge=Judge()
  with patch.object(mod,'load_token_counter',return_value=Counter()),patch.dict('os.environ', {'ASTRION_CONTEXT_INPUT_BUDGET_TOKENS':'10'}):
   result=mod.register_report_claims(self.memory,self.answer,judge,input_budget_tokens=4096)
  self.assertEqual(result['input_budget_tokens'],10)
  self.assertEqual(result['usage']['calls'],0)

if __name__=='__main__': unittest.main()
