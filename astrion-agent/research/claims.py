"""Scoped immutable claim/source links; semantic judgments remain provisional."""
from __future__ import annotations

import json
import re

from context_store import encode
from .citations import covers_range
from .claim_citations import local_citations, cited_observations

MAX_CLAIMS = 20
MAX_INDEX_CHARS = 7000
VERDICTS = {'supported', 'partial', 'unsupported', 'unknown'}


def _source(memory, item):
    if not isinstance(item, dict) or set(item) != {'ref', 'quote'}:
        raise ValueError('claim_source_requires_ref_and_quote')
    ref, quote = item['ref'], item['quote']
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 500:
        raise ValueError('claim_quote_must_be_1_to_500_characters')
    record = memory.archive.get(ref)
    meta, content = record['metadata'], record['content']
    if record['kind'] != 'document' or meta.get('truncated') is not False:
        raise ValueError('claim_requires_complete_document_observation')
    doc, start, end = (meta.get(k) for k in ('document_id', 'start_line', 'end_line'))
    sha = meta.get('sha256')
    if (any(type(x) is not int for x in (doc, start, end)) or start < 1 or end < start
            or not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha)):
        raise ValueError('claim_source_metadata_invalid')
    lines = content.split('\n')
    if len(lines) == end - start + 2 and lines[-1] == '':
        lines.pop()
    if len(lines) != end - start + 1:
        raise ValueError('claim_source_line_count_mismatch')
    pos = content.find(quote)
    if pos < 0 or content.find(quote, pos + 1) != -1:
        raise ValueError('claim_quote_missing_or_ambiguous')
    first = start + content[:pos].count('\n')
    last = start + content[:pos + len(quote.rstrip('\n'))].count('\n')
    if last > end:
        raise ValueError('claim_quote_location_invalid')
    pages = sorted({p['page'] for p in meta.get('pages', []) if isinstance(p, dict)
                    and type(p.get('page')) is int and type(p.get('start_line')) is int
                    and type(p.get('end_line')) is int
                    and p['start_line'] <= last and p['end_line'] >= first})
    return {'ref': ref, 'quote': quote, 'document_id': doc, 'name': meta.get('name'),
            'sha256': sha, 'text_sha256': meta.get('text_sha256'),
            'start_line': first, 'end_line': last, 'pages': pages,
            'warnings': meta.get('warnings', []), 'content_kind': meta.get('content_kind'),
            'source_url': meta.get('source_url'), 'accessed_at': meta.get('accessed_at')}


class ClaimLedger:
    def __init__(self, memory):
        self.memory = memory

    def record(self, statement, sources):
        if not isinstance(statement, str) or not statement.strip() or len(statement) > 300:
            raise ValueError('claim_statement_must_be_1_to_300_characters')
        if not isinstance(sources, list) or not 1 <= len(sources) <= 3:
            raise ValueError('claim_requires_1_to_3_sources')
        resolved = [_source(self.memory, source) for source in sources]
        record = {'kind': 'claim', 'content': statement.strip(), 'metadata': {'sources': resolved}}
        ref = self.memory.archive.reference(record)
        ids = list(self.memory._claim_ids)
        if ref not in ids:
            ids.append(ref)
        if len(ids) > MAX_CLAIMS:
            raise ValueError('claim_index_limit')
        index = [{'claim_id': x, 'statement': statement.strip() if x == ref else self.memory.archive.get(x)['content']}
                 for x in ids]
        if len(encode(index)) > MAX_INDEX_CHARS:
            raise ValueError('claim_index_budget_exceeded')
        self.memory.archive.put(record['kind'], record['content'], record['metadata'])
        self.memory._claim_ids = ids
        return {'claim_id': ref, 'citations': [f"[D{x['document_id']}:L{x['start_line']}-L{x['end_line']}]" for x in resolved],
                'semantic_support': 'unknown'}

    def get(self, claim_id):
        if claim_id not in self.memory._claim_ids:
            raise ValueError('claim_not_in_agent_context')
        record = self.memory.archive.get(claim_id)
        if record['kind'] != 'claim':
            raise ValueError('invalid_claim_record')
        sources = record['metadata'].get('sources', [])
        for source in sources:
            if _source(self.memory, {'ref': source['ref'], 'quote': source['quote']}) != source:
                raise ValueError('claim_source_integrity_failure')
        return {'claim_id': claim_id, 'statement': record['content'], 'sources': sources}

    def list(self, offset=0, limit=3):
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 3:
            raise ValueError('invalid_claim_pagination')
        ids = self.memory._claim_ids
        return {'claims': [self.get(ref) for ref in ids[offset:offset+limit]],
                'next_offset': offset + limit if offset + limit < len(ids) else None}


def assess_claims(memory, answer):
    """Check explicit claim/text links; never infer coverage of unregistered claims."""
    answer = answer or ''
    ledger = ClaimLedger(memory)
    claims = []
    for ref in memory._claim_ids:
        item = ledger.get(ref)
        positions = [match.start() for match in re.finditer(re.escape(item['statement']), answer)]
        overflow = len(positions) > 20
        occurrences = []
        for position in positions[:20]:
            cited, candidates, binding = local_citations(answer, item['statement'], position)
            observations, complete = cited_observations(memory, cited or candidates, item['sources'])
            matched = []
            for source in item['sources']:
                if covers_range(cited, source['document_id'], source['start_line'], source['end_line']):
                    matched.append(source)
                    continue
                alternatives = []
                for obs in observations:
                    if (obs['context_truncated'] or not cited or obs['document_id'] != source['document_id']
                            or obs['cited_excerpt'].count(source['quote']) != 1):
                        continue
                    for obs_ref in obs['refs']:
                        try:
                            alternate = _source(memory, {'ref': obs_ref, 'quote': source['quote']})
                        except ValueError:
                            continue
                        if covers_range(cited, alternate['document_id'], alternate['start_line'], alternate['end_line']):
                            alternatives.append(alternate)
                unique = {(s['document_id'], s['start_line'], s['end_line']): s for s in alternatives}
                if len(unique) == 1:
                    matched.append(next(iter(unique.values())))
            occurrences.append({'position': position, 'binding': binding, 'citations': cited,
                'candidate_citations': candidates, 'cited_observations': observations,
                'cited_observations_complete': complete if cited or candidates else None,
                'matched_sources': matched,
                'location_valid': bool(cited) and complete and len(matched) == len(item['sources'])})
        # When a statement appears repeatedly, all occurrences must be bound.
        location_valid = bool(occurrences) and not overflow and all(x['location_valid'] for x in occurrences)
        first = occurrences[0] if occurrences else {}
        all_observations = {}
        for occurrence in occurrences:
            for obs in occurrence['cited_observations']:
                all_observations[(obs['ref'], obs['start_line'], obs['end_line'])] = obs
        observation_overflow = len(all_observations) > 6
        claims.append({**item, 'in_answer': bool(occurrences), 'location_valid': location_valid,
                       'occurrences': occurrences, 'matched_sources': first.get('matched_sources', []),
                       'cited_observations': list(all_observations.values())[:6],
                       'citation_context_overflow': overflow or observation_overflow,
                       'semantic_support': 'unknown'})
    return {'claims': claims, 'unregistered_claim_coverage': 'unknown',
            'note': '仅检查显式登记且逐字出现的结论；位置不证明语义支持，也不保证发现全部重要结论。'}


def prefer_cited_sources(memory, answer, statement, requested_sources):
    """Prefer an exact quote at the claim's citation without changing old records."""
    resolved = [_source(memory, source) for source in requested_sources]
    positions = [m.start() for m in re.finditer(re.escape(statement), answer)]
    if len(positions) != 1:
        return requested_sources, []
    cited, _, _ = local_citations(answer, statement, positions[0])
    if not cited:
        return requested_sources, []
    observations, _ = cited_observations(memory, cited, resolved)
    output, changes = [], []
    for original, raw in zip(resolved, requested_sources):
        if covers_range(cited, original['document_id'], original['start_line'], original['end_line']):
            output.append(raw)
            continue
        alternatives = []
        for obs in observations:
            if (obs['context_truncated'] or obs['document_id'] != original['document_id']
                    or obs['cited_excerpt'].count(original['quote']) != 1):
                continue
            for obs_ref in obs['refs']:
                try:
                    alternative = _source(memory, {'ref': obs_ref, 'quote': original['quote']})
                except ValueError:
                    continue
                if covers_range(cited, alternative['document_id'], alternative['start_line'], alternative['end_line']):
                    alternatives.append(alternative)
        unique = {(s['document_id'], s['start_line'], s['end_line']): s for s in alternatives}
        if len(unique) == 1:
            alternate = next(iter(unique.values()))
            output.append({'ref': alternate['ref'], 'quote': original['quote']})
            changes.append({'from_ref': original['ref'], 'to_ref': alternate['ref'],
                            'reason': 'exact_quote_in_cited_same_version_observation'})
        else:
            output.append(raw)
    return output, changes


def validate_review(data, expected):
    if not isinstance(data, dict) or set(data) != {'claims'} or not isinstance(data['claims'], list):
        raise ValueError('invalid_claim_review')
    items = {}
    for item in data['claims']:
        if (not isinstance(item, dict) or set(item) != {'claim_id', 'verdict', 'reason'}
                or item['claim_id'] not in expected or item['claim_id'] in items
                or item['verdict'] not in VERDICTS or not isinstance(item['reason'], str)
                or not item['reason'].strip() or len(item['reason']) > 500):
            raise ValueError('invalid_claim_review_item')
        items[item['claim_id']] = item
    if set(items) != expected:
        raise ValueError('incomplete_claim_review')
    return items
