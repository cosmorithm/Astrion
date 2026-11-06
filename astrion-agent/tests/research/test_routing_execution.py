import json
import threading
import unittest

from research import routing_execution as routing


class Client:
    model = 'test-model'
    def __init__(self, response=None, failure=None):
        self.response, self.failure = response, failure
        self.usage_totals = {'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}
        self.usage_calls = self.usage_unknown = 0
        self.last_usage = None
    def chat(self, messages, tools=None):
        if self.failure: raise self.failure
        self.usage_totals.update(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        self.usage_calls += 1
        self.last_usage = dict(self.usage_totals)
        return self.response
    def fork(self, role): return Client(self.response, self.failure)


class Pool:
    limit, steps, report_reader_enabled = 3, 6, True
    def __init__(self, llm):
        self.lock = threading.Lock()
        self.clients = [(llm, {'totals':{},'calls':0,'unknown_calls':0})]
        self.calls = []
    def run_routed(self, tasks, *, worker_allowlist, original_request):
        self.calls.append((tasks, worker_allowlist, original_request))
        return json.dumps({'workers': [
            {'worker_id': f'worker_{i}', 'task': task['task'], 'status':'completed',
             'answer':'result '*200, 'truncated':False} for i, task in enumerate(tasks,1)]})


class RoutingTests(unittest.TestCase):
    def make(self, payload):
        client = Client({'role':'assistant','content':json.dumps(payload,ensure_ascii=False)})
        pool, events = Pool(client), []
        def emit(name, *, step=None, payload=None): events.append((name,step,payload))
        return client,pool,events,emit
    def call(self,c,p,emit,**overrides):
        params=dict(task='对比两篇论文，不要联网',original_request='原始问题：A和B；补充：对比，不要联网',
                    worker_allowlist={'read_knowledge','finish'},llm=c,pool=p,emit=emit,
                    enabled=True,max_steps=10,max_workers=3,budget_chars=12000)
        params.update(overrides)
        return routing.route_and_delegate(**params)
    def test_two_tasks_dispatch_and_costs(self):
        c,p,e,emit=self.make({'mode':'delegate','reason':'可独立阅读','tasks':[
            {'task':'阅读A','context':'只读'}, {'task':'阅读B','context':'只读'}]})
        result=self.call(c,p,emit)
        self.assertEqual(result['proposal']['status'],'ok')
        self.assertEqual(result['dispatch']['status'],'returned')
        self.assertEqual(len(p.calls[0][0]),2)
        self.assertEqual(p.calls[0][2],'原始问题：A和B；补充：对比，不要联网')
        self.assertEqual(result['router_usage']['totals']['total_tokens'],15)
        self.assertTrue(result['observation']['workers'][0]['truncated'])
        self.assertEqual(result['observation']['workers'][0]['read_report'],'read_worker_report')
        self.assertEqual([x[0] for x in e],['model.call.started','model.call.finished','decision.finished'])
        self.assertTrue(all(x[2]['source']=='router' and x[1] is None for x in e))
    def test_direct_and_invalid_proposal_fallback(self):
        for payload, status in [({'mode':'single','reason':'查阅','tasks':[]},'ok'),
            ({'mode':'delegate','reason':'x','tasks':[{'task':'A','context':''}]*2},'fallback'),
            ({'mode':'delegate','reason':'x','tasks':[{'task':'A','context':''}]*4},'fallback')]:
            c,p,e,emit=self.make(payload)
            self.assertEqual(self.call(c,p,emit)['proposal']['status'],status)
            self.assertFalse(p.calls)
    def test_limits_and_allowlist_skip_without_model_call(self):
        for overrides in [{'max_steps':1},{'max_workers':0},{'tool_allowlist':{'run_worker'}}]:
            c,p,e,emit=self.make({'mode':'single','reason':'x','tasks':[]})
            self.assertEqual(self.call(c,p,emit,**overrides)['proposal']['status'],'skipped')
            self.assertEqual(len(p.clients),1)
    def test_parallel_without_allowlist_does_not_dispatch(self):
        c,p,e,emit=self.make({'mode':'delegate','reason':'x','tasks':[{'task':'a','context':''},{'task':'b','context':''}]})
        result=self.call(c,p,emit,tool_allowlist={'run_worker','read_worker_report'})
        self.assertEqual(result['dispatch']['reason'],'parallel_delegation_tool_unavailable')
        self.assertFalse(p.calls)
    def test_model_error_fallback_and_sink_failure_propagates(self):
        c,p,e,emit=self.make({});c.failure=RuntimeError('private details')
        self.assertEqual(self.call(c,p,emit)['proposal']['status'],'fallback')
        self.assertNotIn('private details',str(e))
        self.assertEqual([x[0] for x in e],['model.call.started','model.call.failed','decision.finished'])
        c,p,e,emit=self.make({'mode':'single','reason':'x','tasks':[]})
        def broken(*args,**kwargs): raise RuntimeError('journal failure')
        with self.assertRaisesRegex(RuntimeError,'journal failure'): self.call(c,p,broken)
    def test_pool_failure_propagates(self):
        c,p,e,emit=self.make({'mode':'delegate','reason':'x','tasks':[{'task':'a','context':''}]})
        def bad(*args,**kwargs): raise RuntimeError('journal failure')
        p.run_routed=bad
        with self.assertRaisesRegex(RuntimeError,'journal failure'): self.call(c,p,emit)

    def test_system_exit_falls_back_with_unknown_usage(self):
        c,p,e,emit=self.make({}); c.failure=SystemExit('private provider response')
        result=self.call(c,p,emit)
        self.assertEqual(result['proposal']['status'], 'fallback')
        self.assertEqual(result['router_usage']['calls'], 1)
        self.assertEqual(result['router_usage']['unknown_calls'], 1)
        self.assertNotIn('private provider response', str(e))

    def test_failed_client_reservation_falls_back_without_workers(self):
        c,p,e,emit=self.make({'mode':'delegate','reason':'x','tasks':[{'task':'a','context':''}]})
        def bad(*args, **kwargs): raise routing.WorkerClientUnavailable('unavailable')
        p.run_routed=bad
        self.assertEqual(self.call(c,p,emit)['dispatch']['reason'], 'worker_client_unavailable')
    def test_tool_call_and_budget_rejected(self):
        c,p,e,emit=self.make({'mode':'single','reason':'x','tasks':[]})
        c.response['tool_calls']=[{'id':'x'}]
        self.assertEqual(self.call(c,p,emit)['proposal']['status'],'fallback')
        c,p,e,emit=self.make({'mode':'single','reason':'x','tasks':[]})
        self.assertEqual(self.call(c,p,emit,input_budget_tokens=10)['proposal']['status'],'fallback')

if __name__=='__main__': unittest.main()
