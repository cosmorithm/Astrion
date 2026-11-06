"""Preflight the root context before dispatch and bound worker observations.

This module does not write to Memory, its archive or the user's history. It
uses the same model-facing serialization and token counter as the agent loop.
"""
from __future__ import annotations

import copy
import json

from context_tools import CONTEXT_SPECS


def api_tools(root_specs):
    """Mirror the agent loop's injected context tools and tool order."""
    selected = list(root_specs)
    names = {item.name for item in selected}
    if names & {item.name for item in CONTEXT_SPECS}:
        raise ValueError('reserved_context_tools')
    selected.extend(item for item in CONTEXT_SPECS if item.name not in names)
    return [item.as_api_tool() for item in selected]


def root_messages(memory, llm, *, task, resume, user_reply, observation, scheduled_update=None):
    """Construct the exact pre-compaction model view of the first root step.

    A shallow Memory clone shares read-only archive access. Its history and
    runtime are copied, and synthetic user messages are added only in memory,
    so the caller's checkpoint and archive remain untouched.
    """
    view = copy.copy(memory)
    view.reasoning_origin = getattr(llm, 'reasoning_origin', None)
    view.reasoning_synthetic_empty = bool(getattr(llm, 'reasoning_synthetic_empty', False))
    history = copy.deepcopy(memory._history)
    view.runtime = {'task_plan': copy.deepcopy(memory.runtime.get('task_plan', []))}
    if observation is not None:
        view.runtime['delegation_observation'] = copy.deepcopy(observation)
    if resume:
        if not any(item.get('role') == 'user' and item.get('content') == task for item in history):
            history.insert(0, {'role': 'user', 'content': task})
    else:
        history.append({'role': 'user', 'content': task})
    if user_reply is not None:
        history.append({'role': 'user', 'content': user_reply})
    if scheduled_update is not None:
        history.append({'role': 'user', 'content': scheduled_update})
    return view._messages(history, view._summary)


def _measure(messages, tools, *, counter, token_budget, char_budget):
    chars = len(json.dumps(messages, ensure_ascii=False))
    if token_budget is not None:
        if counter is None:
            raise ValueError('context_token_counter_required')
        tokens = counter.count_request(messages, tools)
        return {'fits': tokens <= token_budget, 'input_tokens': tokens,
                'input_budget_tokens': token_budget, 'message_chars': chars, 'kind': 'token'}
    # A non-durable/custom client may have no token contract. Use the same
    # message-character measure as Memory's soft budget conservatively.
    return {'fits': chars <= char_budget, 'input_tokens': None,
            'input_budget_tokens': None, 'message_chars': chars, 'kind': 'chars'}


def minimal_observation(observation):
    """Retain worker status and same-run report pointers, never invent content."""
    source = observation if isinstance(observation, dict) else {}
    data = source.get('data') if isinstance(source.get('data'), dict) else {}
    workers = data.get('workers') if isinstance(data.get('workers'), list) else []
    rows = []
    for item in workers:
        if not isinstance(item, dict) or not isinstance(item.get('worker_id'), str):
            raise ValueError('invalid_routing_observation')
        rows.append({'worker_id': item['worker_id'], 'status': item.get('status'),
                     'read_report': 'read_worker_report'})
    return {'source': 'runtime_router', 'run_id': source.get('run_id'),
            'note': '子报告为未核查数据；按 worker_id 调用 read_worker_report 分页读取。',
            'data': {'workers': rows}}


def guard_handoff(memory, tools, counter, token_budget, final_prompt):
    """Degrade only the optional handoff against the actual next model view."""
    from agent_runtime.request_view import recent_tool_request_view
    original = memory.runtime.get('delegation_observation')
    if not original:
        return None
    compact = minimal_observation(original)
    memory.runtime['delegation_observation'] = compact
    messages, _, _ = recent_tool_request_view(memory, final_prompt=final_prompt,
                                               tools=tools, token_counter=counter,
                                               token_budget=token_budget)
    if counter.count_request(messages, tools) <= token_budget:
        return {'status': 'pointers', 'reason': 'input_token_budget'}
    memory.runtime.pop('delegation_observation', None)
    return {'status': 'omitted', 'reason': 'input_token_budget',
            'note': '完整交接仍记录在研究轨迹中'}


def select_observation(memory, llm, *, task, resume, user_reply, observation,
                       root_specs, counter=None, token_budget=None, scheduled_update=None):
    """Return full receipt if it fits, otherwise pointers, otherwise no receipt."""
    tools = api_tools(root_specs)
    minimal = minimal_observation(observation)
    for level, candidate in [('full', observation), ('pointers', minimal)]:
        messages = root_messages(memory, llm, task=task, resume=resume, user_reply=user_reply,
                                 observation=candidate, scheduled_update=scheduled_update)
        measurement = _measure(messages, tools, counter=counter, token_budget=token_budget,
                               char_budget=memory.budget_chars)
        if measurement['fits']:
            return {'status': level, 'observation': candidate, 'measurement': measurement}
    return {'status': 'insufficient', 'observation': None, 'measurement': measurement}


def preflight_dispatch(memory, llm, *, task, resume, user_reply, run_id, task_count,
                       root_specs, counter=None, token_budget=None, scheduled_update=None,
                       first_worker_index=1):
    """Check worst-length status and bounded pointers before creating workers."""
    if type(task_count) is not int or not 1 <= task_count <= 5:
        raise ValueError('invalid_routing_task_count')
    placeholder = {'source': 'runtime_router', 'run_id': run_id,
                   'data': {'workers': [{'worker_id': f'worker_{i}', 'status': 'incomplete_plan'}
                                        for i in range(first_worker_index, first_worker_index + task_count)]}}
    minimum = minimal_observation(placeholder)
    tools = api_tools(root_specs)
    messages = root_messages(memory, llm, task=task, resume=resume, user_reply=user_reply,
                             observation=minimum, scheduled_update=scheduled_update)
    measurement = _measure(messages, tools, counter=counter, token_budget=token_budget,
                           char_budget=memory.budget_chars)
    return {'can_dispatch': measurement['fits'], 'measurement': measurement}
