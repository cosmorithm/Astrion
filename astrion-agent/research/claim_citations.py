"""Conservative claim-local citation parsing and same-version observation lookup."""
from __future__ import annotations

import re

CITATION = r'\[D(\d+):L(\d+)(?:-L(\d+))?\]'
CITATION_TOKEN = r'\[D\d+:L\d+(?:-L\d+)?\]'


def _ranges(text):
    items = []
    for doc, first, last in re.findall(CITATION, text):
        first, last = int(first), int(last or first)
        if first <= last:
            items.append([int(doc), first, last])
    return items


def _container_content(line):
    # Strip common blockquote/list prefixes solely to recognize fence markers.
    while True:
        match = re.match(r'^ {0,3}(?:>[ \t]?|(?:[-+*]|\d+[.)])[ \t]+)', line)
        if not match:
            return line
        line = line[match.end():]


def _inside_code(answer, position):
    line_start = answer.rfind('\n', 0, position) + 1
    fence = None
    for line in answer[:line_start].splitlines():
        content = _container_content(line)
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', content)
        if not marker:
            continue
        value = marker.group(1)
        if fence is None:
            fence = value
        elif value[0] == fence[0] and len(value) >= len(fence) and not marker.group(2).strip():
            fence = None
    line = answer[line_start:answer.find('\n', line_start) if '\n' in answer[line_start:] else len(answer)]
    if fence or re.match(r'^(?: {4}|\t| {0,3}(?:`{3,}|~{3,}))', _container_content(line)):
        return True
    return answer[line_start:position].count('`') % 2 == 1


def local_citations(answer, statement, position):
    """Return definite local citations and separately marked quote-block candidates."""
    if (_inside_code(answer, position) or statement.count('`') % 2 or any(re.match(r'^ {0,3}(?:`{3,}|~{3,})', _container_content(line))
                                               for line in statement.splitlines())):
        return [], [], 'code_example'
    # A trailing citation embedded inside the claim can only refer to that final
    # sentence, not an earlier sentence of a multi-sentence claim.
    terminal = re.search(r'((?:' + CITATION_TOKEN + r'[ \t,，;；]*)+)[ \t。.!！?？:：]*$', statement)
    prefix = statement[:terminal.start()] if terminal else statement
    multi_sentence = bool(re.search(r'[。！？!?；;]\s*\S|(?<!\d)\.[ \t\r\n]*(?=[A-Za-z\u4e00-\u9fff])', prefix))
    direct = _ranges(terminal.group(1)) if terminal and not multi_sentence else []
    after = answer[position + len(statement):position + len(statement) + 1000]
    adjacent = re.match(r'^[ \t。.!！?？]*(?:' + CITATION_TOKEN + r'[ \t,，;；]*)+', after)
    if adjacent and not multi_sentence:
        direct.extend(_ranges(adjacent.group()))
    line_start = answer.rfind('\n', 0, position) + 1
    if not direct and not multi_sentence and answer[line_start:position].lstrip().startswith('>'):
        quoted = re.match(r'^[ \t]*\n>[ \t]*(?:' + CITATION_TOKEN + r'[ \t,，;；]*)+', after)
        if quoted:
            direct.extend(_ranges(quoted.group()))
    # A bridging phrase and a following Markdown quote may be relevant, but
    # syntax alone cannot prove that the quote supports the preceding claim.
    candidate = []
    bridge = re.match(r'^[ \t]*(?:原文(?:摘要|原文|中|里)?(?:写道|指出|表述|如下)|原文写道)[：:]'
                      r'[ \t]*\n\s*\n>[ \t]*[^\n]{1,700}(' + CITATION_TOKEN + r')', after)
    if not direct and bridge:
        candidate = _ranges(bridge.group())
    return direct, candidate, 'direct' if direct else 'quote_block_candidate' if candidate else 'none'


def cited_observations(memory, ranges, sources):
    """Read only complete same-version observations from this agent's scope."""
    observations = []
    complete = True
    for doc, first, last in ranges:
        if last - first + 1 > 2000:
            complete = False
            continue
        versions = {(s['sha256'], s.get('text_sha256')) for s in sources if s['document_id'] == doc}
        if len(versions) != 1:
            complete = False
            continue
        candidates = []
        for ref, indexed in memory.archive.records.items():
            meta = indexed['metadata']
            if (indexed['kind'] != 'document' or meta.get('truncated') is not False
                    or meta.get('document_id') != doc or type(meta.get('start_line')) is not int
                    or type(meta.get('end_line')) is not int
                    or meta['end_line'] < first - 80 or meta['start_line'] > last + 80):
                continue
            # A citation must refer to the same original and extraction snapshot
            # as at least one registered source. Missing extraction hashes cannot
            # establish this equivalence.
            if not any(s['document_id'] == doc and s['sha256'] == meta.get('sha256')
                       and (s['ref'] == ref or (s.get('text_sha256')
                            and s['text_sha256'] == meta.get('text_sha256'))) for s in sources):
                continue
            record = memory.archive.get(ref)  # integrity check before using contents
            meta, content = record['metadata'], record['content']
            lines = content.split('\n')
            if len(lines) == meta['end_line'] - meta['start_line'] + 2 and lines[-1] == '':
                lines.pop()
            if len(lines) != meta['end_line'] - meta['start_line'] + 1:
                continue
            candidates.append((ref, meta, content, lines))
        # Without an extraction hash, separate observations cannot be proven to
        # share a text version. Use a single observation covering the full range.
        if next(iter(versions))[1] is None:
            candidates = [item for item in candidates
                          if item[1]['start_line'] <= first <= last <= item[1]['end_line']]
            if len(candidates) != 1:
                complete = False
                continue
        # Join overlapping/adjacent observations only when every line is known
        # and overlapping copies agree. Missing lines are never reconstructed.
        line_map, conflict = {}, set()
        for ref, meta, _, lines in candidates:
            for index, line in enumerate(lines, start=meta['start_line']):
                if index in line_map and line_map[index] != line:
                    conflict.add(index)
                else:
                    line_map[index] = line
        if not candidates or any(i not in line_map or i in conflict for i in range(first, last + 1)):
            complete = False
            continue
        excerpt = '\n'.join(line_map[i] for i in range(first, last + 1))
        # Expand only through known, non-conflicting surrounding lines. This
        # preserves nearby negations without claiming anything about unread text.
        left, right = first, last
        while left > max(1, first - 80) and left - 1 in line_map and left - 1 not in conflict:
            left -= 1
        while right < last + 80 and right + 1 in line_map and right + 1 not in conflict:
            right += 1
        context_conflict = left - 1 in conflict or right + 1 in conflict
        window_limited = ((left == first - 80 and left > 1) or right == last + 80)
        content = '\n'.join(line_map[i] for i in range(left, right + 1))
        text_start = sum(len(line_map[i]) + 1 for i in range(left, first))
        limit = 8000
        start = max(0, min(text_start - max(0, limit - len(excerpt)) // 2, len(content) - limit))
        context = content[start:start + limit]
        context_first = left + content[:start].count('\n')
        context_last = context_first + context.rstrip('\n').count('\n')
        ref, meta, _, _ = candidates[0]
        refs = [candidate[0] for candidate in candidates]
        pages = sorted({p['page'] for _, metadata, _, _ in candidates
                        for p in metadata.get('pages', []) if isinstance(p, dict)
                        and type(p.get('page')) is int and type(p.get('start_line')) is int
                        and type(p.get('end_line')) is int
                        and p['start_line'] <= last and p['end_line'] >= first})
        observations.append({'ref': ref, 'document_id': doc, 'sha256': meta['sha256'],
            'refs': refs,
            'text_sha256': meta.get('text_sha256'), 'start_line': first, 'end_line': last,
            'cited_excerpt': excerpt[:limit], 'observed_context': context,
            'context_start_line': context_first, 'context_end_line': context_last,
            'context_start_partial': start > 0 and content[start - 1] != '\n',
            'context_end_partial': start + len(context) < len(content) and not context.endswith('\n'),
            'context_conflict': context_conflict, 'context_window_limited': window_limited,
            'context_truncated': len(content) > len(context) or len(excerpt) > limit,
            'pages': pages, 'warnings': list(dict.fromkeys(w for _, metadata, _, _ in candidates
                                                          for w in metadata.get('warnings', []) if isinstance(w, str))),
            'content_kind': meta.get('content_kind')})
    return observations, complete
