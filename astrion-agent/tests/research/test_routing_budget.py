import copy
import json
import unittest

from memory import Memory
from tools.builtin import TOOL_SPECS
from context_tools import CONTEXT_SPECS

from research import routing_budget as routing


class Model:
    reasoning_origin = 'deepseek:test'
    reasoning_synthetic_empty = True


class Counter:
    identifier = 'test-chars'
    def count_request(self, messages, tools):
        return len(json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False))


def observation():
    return {'source': 'runtime_router', 'run_id': 'research_'+'a'*32, 'note': '待核查',
            'data': {'workers': [{'worker_id':'worker_1','status':'completed', 'answer':'文'*1500,
                                  'read_report':'read_worker_report', 'truncated':True}]}}


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.memory = Memory('固定规则', budget_chars=20000)
        self.llm = Model()
        self.tools = [tool for tool in TOOL_SPECS if tool.name not in {spec.name for spec in CONTEXT_SPECS}]
        self.counter = Counter()

    def kwargs(self, **overrides):
        p = {'task':'研究任务', 'resume':False, 'user_reply':None,
             'root_specs':self.tools, 'counter':self.counter, 'token_budget':100000}
        p.update(overrides)
        return p

    def test_new_request_matches_real_memory_and_does_not_mutate(self):
        before = copy.deepcopy(self.memory.to_dict())
        actual = routing.root_messages(self.memory, self.llm, task='研究任务', resume=False,
                                       user_reply=None, observation=observation(), scheduled_update='新要求')
        real = Memory.from_dict(before)
        real.reasoning_origin = self.llm.reasoning_origin
        real.reasoning_synthetic_empty = self.llm.reasoning_synthetic_empty
        real.runtime['delegation_observation'] = observation()
        real.add_user('研究任务')
        real.add_user('新要求')
        self.assertEqual(actual, real.for_model())
        self.assertEqual(self.memory.to_dict(), before)

    def test_resume_ensures_missing_task_and_preserves_existing_history(self):
        self.memory.add_user('先前补充')
        self.memory.add_assistant({'role':'assistant','content':'答复'})
        before = copy.deepcopy(self.memory.to_dict())
        actual = routing.root_messages(self.memory, self.llm, task='原始任务', resume=True,
                                       user_reply='最新补充', observation=observation())
        real = Memory.from_dict(before)
        real.reasoning_origin = self.llm.reasoning_origin
        real.reasoning_synthetic_empty = self.llm.reasoning_synthetic_empty
        real.runtime['delegation_observation'] = observation()
        real.ensure_task('原始任务')
        real.add_user('最新补充')
        self.assertEqual(actual, real.for_model())
        self.assertEqual(self.memory.to_dict(), before)

    def test_full_and_pointer_downgrade_respect_real_counter(self):
        full = observation()
        tools = routing.api_tools(self.tools)
        full_messages = routing.root_messages(self.memory, self.llm, task='研究任务', resume=False,
                                               user_reply=None, observation=full)
        pointer_messages = routing.root_messages(self.memory, self.llm, task='研究任务', resume=False,
                                                  user_reply=None, observation=routing.minimal_observation(full))
        full_tokens = self.counter.count_request(full_messages, tools)
        pointer_tokens = self.counter.count_request(pointer_messages, tools)
        self.assertGreater(full_tokens, pointer_tokens)
        self.assertEqual(routing.select_observation(self.memory,self.llm,observation=full,**self.kwargs(token_budget=full_tokens))['status'], 'full')
        selected = routing.select_observation(self.memory,self.llm,observation=full,**self.kwargs(token_budget=pointer_tokens))
        self.assertEqual(selected['status'], 'pointers')
        self.assertEqual(selected['measurement']['input_tokens'], pointer_tokens)
        self.assertEqual(selected['observation']['data']['workers'][0]['read_report'], 'read_worker_report')
        self.assertEqual(routing.select_observation(self.memory,self.llm,observation=full,**self.kwargs(token_budget=pointer_tokens-1))['status'], 'insufficient')

    def test_predispatch_conservatively_reserves_actual_pointer_size(self):
        full = observation(); full['data']['workers'][0]['status'] = 'incomplete_plan'
        minimal = routing.minimal_observation(full)
        messages = routing.root_messages(self.memory,self.llm,task='研究任务',resume=False,user_reply=None,observation=minimal)
        n = self.counter.count_request(messages,routing.api_tools(self.tools))
        p = dict(task='研究任务',resume=False,user_reply=None,run_id=full['run_id'],task_count=1,
                 root_specs=self.tools,counter=self.counter)
        self.assertTrue(routing.preflight_dispatch(self.memory,self.llm,token_budget=n,**p)['can_dispatch'])
        self.assertFalse(routing.preflight_dispatch(self.memory,self.llm,token_budget=n-1,**p)['can_dispatch'])

    def test_requires_counter_if_token_budget_set(self):
        with self.assertRaisesRegex(ValueError,'token_counter_required'):
            routing.select_observation(self.memory,self.llm,observation=observation(),**self.kwargs(counter=None))

if __name__ == '__main__': unittest.main()
