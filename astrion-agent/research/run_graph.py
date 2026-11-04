"""Project-scoped, bounded graph of explicitly recorded research versions."""
from __future__ import annotations

import heapq
import json
import re
import sqlite3
from pathlib import Path

RUN_ID = re.compile(r'research_[a-f0-9]{32}\Z')
SCOPE = re.compile(r'[a-f0-9]{32}\Z')


def _checkpoint_status(repository, info, connection):
    """Classify a checkpoint without following arbitrary paths or restoring it."""
    if not isinstance(info, dict) or not isinstance(info.get('path'), str) or not isinstance(info.get('scope'), str):
        return 'unavailable'
    if not SCOPE.fullmatch(info['scope']):
        return 'unavailable'
    expected = Path(repository.database).parent / 'context.sqlite3'
    try:
        supplied = Path(info['path'])
        if supplied.is_symlink() or expected.is_symlink() or supplied.resolve() != expected.resolve() or not expected.is_file():
            return 'unavailable'
    except (OSError, ValueError, RuntimeError):
        return 'unavailable'
    if connection is None:
        return 'unknown'
    try:
        row = connection.execute('SELECT state FROM context_checkpoints WHERE scope=?', (info['scope'],)).fetchone()
        if not row:
            return 'unavailable'
        state = json.loads(row[0])
        if (not isinstance(state, dict) or not isinstance(state.get('system_prompt'), str)
                or not state['system_prompt'].strip() or not isinstance(state.get('history'), list)
                or state.get('archive_scope', info['scope']) != info['scope']):
            return 'unavailable'
        # Individual archived messages and evidence are checked only on restore.
        return 'checkpoint_present'
    except (ValueError, TypeError):
        return 'unavailable'
    except sqlite3.Error:
        return 'unknown'


def version_graph(repository, limit=200):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('invalid_graph_limit: limit must be 1–500')
    selected = []
    total = 0
    skipped = 0
    # Keep memory bounded even for projects with very many runs. Read via get()
    # to enforce project/database identity and reject symlinked run directories.
    for path in repository.base.glob('research_*/result.json'):
        try:
            row = repository.get(path.parent.name)
        except (ValueError, OSError, TypeError, AttributeError):
            skipped += 1
            continue
        run_id = row['run_id']
        result = row.get('result') if isinstance(row.get('result'), dict) else {}
        task = row.get('task') if isinstance(row.get('task'), str) else ''
        timestamp = row.get('recorded_at') if isinstance(row.get('recorded_at'), str) else ''
        parent = row.get('parent_run_id')
        node = {'run_id': run_id, 'parent_run_id': parent if isinstance(parent, str) else None,
                'parent_invalid': parent is not None and (not isinstance(parent, str) or not RUN_ID.fullmatch(parent)),
                'task': task[:2000], 'task_truncated': len(task) > 2000,
                'status': result.get('status', row.get('status')),
                'recorded_at': timestamp or None, 'duration_seconds': row.get('duration_seconds'),
                '_context_archive': row.get('context_archive'),
                'has_report': bool(result.get('answer') or row.get('report_artifact'))}
        key = (timestamp, run_id, node)
        total += 1
        if len(selected) < limit:
            heapq.heappush(selected, key)
        elif key[:2] > selected[0][:2]:
            heapq.heapreplace(selected, key)
    nodes = [item[2] for item in sorted(selected, key=lambda x: x[:2], reverse=True)]
    database = Path(repository.database).parent / 'context.sqlite3'
    connection = None
    try:
        if database.is_file() and not database.is_symlink():
            connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
        cache = {}
        for node in nodes:
            info = node.pop('_context_archive')
            key = (info.get('path'), info.get('scope')) if isinstance(info, dict) else (None, None)
            # Unexpected unhashable legacy metadata must not break the graph.
            if any(not isinstance(x, (str, type(None))) for x in key):
                status = 'unavailable'
            else:
                if key not in cache:
                    cache[key] = _checkpoint_status(repository, info, connection)
                status = cache[key]
            node['continuation_status'] = status
            node['can_continue'] = False if status == 'unavailable' else None
    except (sqlite3.Error, OSError):
        for node in nodes:
            node.pop('_context_archive', None)
            node['continuation_status'] = 'unknown'
            node['can_continue'] = None
    finally:
        if connection is not None:
            connection.close()
    by_id = {node['run_id']: node for node in nodes}
    missing = []
    parents = {}
    parent_cache = {}
    for node in nodes:
        child, parent = node['run_id'], node['parent_run_id']
        if node['parent_invalid']:
            missing.append({'run_id': child, 'parent_run_id': parent[:200] if isinstance(parent, str) else None,
                            'reason': 'invalid_parent'})
        elif parent is None:
            continue
        elif parent in by_id:
            parents[child] = parent
        else:
            if parent not in parent_cache:
                try:
                    repository.get(parent)
                    parent_cache[parent] = 'outside_limit'
                except (ValueError, OSError, TypeError, AttributeError):
                    parent_cache[parent] = 'not_found'
            reason = parent_cache[parent]
            missing.append({'run_id': child, 'parent_run_id': parent, 'reason': reason})
    # Each version has at most one parent. Detect cycles without recursion.
    checked, cycles = set(), []
    for start in by_id:
        path, indices, cursor = [], {}, start
        while cursor in parents and cursor not in checked:
            if cursor in indices:
                cycles.append(sorted(path[indices[cursor]:]))
                break
            indices[cursor] = len(path)
            path.append(cursor)
            cursor = parents[cursor]
        checked.update(path)
    cycles.sort()
    cyclic_nodes = {node for cycle in cycles for node in cycle}
    edges = [{'parent_run_id': parent, 'run_id': child} for child, parent in parents.items()
             if not (child in cyclic_nodes and parent in cyclic_nodes)]
    return {'nodes': nodes, 'edges': edges, 'total': total, 'limit': limit, 'skipped_records': skipped,
            'truncated': total > limit, 'missing_parents': missing, 'cycles': cycles}
