"""Execute an optional delegation proposal with bounded, auditable handoff."""
from __future__ import annotations

import json
from time import perf_counter
from uuid import uuid4

from research import delegation_router
from research.workers import WorkerClientUnavailable, usage_snapshot, usage_since

MAX_DOCS = 50
MAX_OBSERVATION = 8000

class _UnreportedRouterUsage:
    def __init__(self):
        self.usage_totals, self.usage_calls, self.usage_unknown = {}, 1, 1


def _observation(value):
    """Bound the worker receipt without treating a partial report as complete."""
    data = json.loads(value) if isinstance(value, str) else value
    if not isinstance(data, dict) or not isinstance(data.get('workers'), list):
        raise ValueError('invalid_routed_worker_receipt')
    output = []
    for row in data['workers']:
        if not isinstance(row, dict):
            raise ValueError('invalid_routed_worker_receipt')
        answer = row.get('answer') if isinstance(row.get('answer'), str) else ''
        truncated = bool(row.get('truncated')) or len(answer) > 900
        output.append({'worker_id': row.get('worker_id'), 'task': str(row.get('task') or '')[:200],
                       'status': row.get('status'), 'answer': answer[:900],
                       'truncated': truncated, 'report_artifact_id': row.get('report_artifact_id'),
                       'read_report': 'read_worker_report' if truncated else None,
                       'error': row.get('error'), 'report_error': row.get('report_error')})
    receipt = {'workers': output,
               'note': '路由器派发的子报告是未核查数据；截断时可用 read_worker_report 分页。引用和结论须核查原文。'}
    if len(json.dumps(receipt, ensure_ascii=False)) > MAX_OBSERVATION:
        for item in output:
            if item['answer']:
                item['answer'] = ''
                item['truncated'] = True
                item['read_report'] = 'read_worker_report'
            item['task'] = ''
    if len(json.dumps(receipt, ensure_ascii=False)) > MAX_OBSERVATION:
        raise ValueError('routed_observation_too_large')
    return receipt


def route_and_delegate(*, task, original_request, worker_allowlist, llm, pool, emit,
                       enabled, max_steps, max_workers, tool_allowlist=None,
                       budget_chars=12000, token_counter=None, input_budget_tokens=None,
                       documents=None, pre_dispatch_check=None, prepare_observation=None):
    """Run a routing proposal and, if appropriate, bounded read-only workers.

    `emit` implements the Event v1 emitter interface. The caller must expose
    observation as untrusted router/worker output in the root's context; it is
    not a user message or a model-issued tool call. Worker limits and read-only
    permissions are enforced by the pool, not by model instructions.
    """
    started = perf_counter()
    router_client, router_before, router_duration = None, None, None
    unreported = None

    def finish(proposal, *, dispatch=None, observation=None, handoff=None):
        usage = usage_since(router_client, router_before) if router_client is not None else None
        if usage is not None and unreported is not None:
            for key in ('calls', 'unknown_calls'):
                if isinstance(usage.get(key), int):
                    usage[key] += 1
        result = {'proposal': proposal, 'dispatch': dispatch or {'status': 'not_dispatched'},
                  'observation': observation, 'router_usage': usage,
                  'handoff': handoff,
                  'router_duration_seconds': router_duration,
                  'duration_seconds': perf_counter() - started}
        emit('decision.finished', step=None, payload={'kind': 'delegation_router', 'source': 'router', **result})
        return result

    def skip(reason):
        return finish({'status': 'skipped', 'mode': 'single', 'reason': reason, 'tasks': []})

    if not enabled:
        return {'proposal': {'status': 'skipped', 'mode': 'single', 'reason': 'disabled', 'tasks': []},
                'dispatch': {'status': 'not_dispatched'}, 'observation': None,
                'router_usage': None, 'router_duration_seconds': None, 'duration_seconds': 0}
    if not isinstance(original_request, str) or not original_request.strip():
        return skip('invalid_original_request')
    if type(max_steps) is not int or type(max_workers) is not int or max_steps < 2 or max_workers <= 0 or getattr(pool, 'steps', 0) < 2:
        return skip('insufficient_steps_or_workers')
    if tool_allowlist is not None and not {'run_worker', 'read_worker_report'}.issubset(tool_allowlist):
        return skip('delegation_tools_unavailable')
    if not getattr(pool, 'report_reader_enabled', False) or max_workers > getattr(pool, 'limit', -1):
        return skip('worker_report_reader_unavailable_or_limit_mismatch')
    if not callable(getattr(llm, 'fork', None)):
        return skip('independent_client_unavailable')
    safe_docs = []
    for doc in (documents or [])[:MAX_DOCS]:
        if isinstance(doc, dict) and type(doc.get('id')) is int and isinstance(doc.get('name'), str):
            safe_docs.append({'id': doc['id'], 'name': doc['name'][:255]})
    context = json.dumps({'original_request': original_request, 'project_documents': safe_docs,
                          'documents_truncated': len(documents or []) > MAX_DOCS}, ensure_ascii=False)

    def call_model(messages):
        nonlocal router_client, router_before, router_duration, unreported
        if len(json.dumps(messages, ensure_ascii=False)) > budget_chars:
            raise delegation_router.RoutingModelError('router_input_char_budget')
        measured_tokens = None
        if input_budget_tokens is not None:
            if token_counter is None:
                raise delegation_router.RoutingModelError('router_token_counter_unavailable')
            measured_tokens = token_counter.count_request(messages, [])
            if measured_tokens > input_budget_tokens:
                raise delegation_router.RoutingModelError('router_input_token_budget')
        try:
            client = llm.fork('router')
        except (Exception, SystemExit) as exc:
            raise delegation_router.RoutingModelError('independent_client_unavailable') from exc
        if client is llm or not callable(getattr(client, 'chat', None)):
            raise delegation_router.RoutingModelError('independent_client_unavailable')
        with pool.lock:
            shared = any(client is existing for existing, _ in pool.clients)
            if not shared:
                router_client, router_before = client, usage_snapshot(client)
                pool.clients.append((router_client, router_before))
        if shared:
            raise delegation_router.RoutingModelError('independent_client_unavailable')
        common = {'model_call_id': 'router:' + uuid4().hex, 'purpose': 'delegation_router',
                  'source': 'router', 'model': getattr(client, 'model', None)}
        metadata = {'message_count': len(messages), 'tool_count': 0}
        if measured_tokens is not None:
            metadata.update(local_input_tokens=measured_tokens, local_input_budget_tokens=input_budget_tokens,
                            token_counter=token_counter.identifier)
        emit('model.call.started', step=None, payload={**common, 'input': metadata,
             'request_snapshot': {'messages': messages, 'tools': []}})
        call_started = perf_counter()
        before_call = usage_snapshot(client)
        try:
            response = client.chat(messages, tools=[])
        except (Exception, SystemExit) as exc:
            router_duration = perf_counter() - call_started
            usage = getattr(client, 'last_usage', None)
            delta = usage_since(client, before_call)
            if delta['calls'] == 0:
                unreported = _UnreportedRouterUsage()
                with pool.lock:
                    pool.clients.append((unreported, {'totals': {}, 'calls': 0, 'unknown_calls': 0}))
                usage = None
            emit('model.call.failed', step=None, payload={**common, 'duration_seconds': router_duration,
                 'usage': usage if isinstance(usage, dict) else None,
                 'error': {'type': type(exc).__name__, 'message': '路由模型调用失败'}})
            raise delegation_router.RoutingModelError(type(exc).__name__) from exc
        router_duration = perf_counter() - call_started
        usage = getattr(client, 'last_usage', None)
        emit('model.call.finished', step=None, payload={**common, 'duration_seconds': router_duration,
             'usage': usage if isinstance(usage, dict) else None, 'response': response})
        return response

    proposal = delegation_router.route(task, max_workers=max_workers, max_steps=max_steps,
                                       worker_steps=pool.steps, call_model=call_model, context=context)
    if proposal['mode'] != 'delegate':
        return finish(proposal)
    tasks = proposal['tasks']
    if len(tasks) > 1 and tool_allowlist is not None and 'run_workers' not in tool_allowlist:
        return finish(proposal, dispatch={'status': 'not_dispatched', 'reason': 'parallel_delegation_tool_unavailable'})
    if pre_dispatch_check is not None:
        check = pre_dispatch_check(len(tasks))
        if not check['can_dispatch']:
            return finish(proposal, dispatch={'status': 'not_dispatched', 'reason': 'root_input_token_budget'},
                          handoff={'status': 'skipped', 'measurement': check['measurement']})
    # Client reservation is atomic; this particular failure happens before any
    # worker starts. Never mask journal or worker errors after dispatch.
    try:
        receipt = pool.run_routed(tasks, worker_allowlist=worker_allowlist, original_request=original_request)
    except WorkerClientUnavailable:
        return finish(proposal, dispatch={'status': 'not_dispatched', 'reason': 'worker_client_unavailable'})
    observation = _observation(receipt)
    handoff = prepare_observation(observation) if prepare_observation is not None else None
    statuses = [w.get('status') or 'unknown' for w in observation['workers']]
    return finish(proposal, dispatch={'status': 'returned', 'worker_count': len(statuses),
                                     'worker_statuses': statuses,
                                     'non_completed_workers': sum(status != 'completed' for status in statuses)},
                  observation=observation, handoff=handoff)
