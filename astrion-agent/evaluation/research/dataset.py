"""Frozen paper identity, reproducible corpus construction and gold validation."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from research.store import Store

HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE = Path('/Users/apple/Desktop/科研/Agent Test Paper')
DEFAULT_BASE = Path('research_data/research_eval_v1/corpus.sqlite3')


def load_data():
    corpus = json.loads((HERE / 'corpus.json').read_text(encoding='utf-8'))
    cases = json.loads((HERE / 'cases.json').read_text(encoding='utf-8'))
    if corpus['corpus_id'] != cases['corpus_id']:
        raise ValueError('corpus_version_mismatch')
    return corpus, cases['cases']


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def validate(source=DEFAULT_SOURCE, *, verify_anchors=True):
    corpus, cases = load_data()
    source = Path(source).expanduser().resolve(strict=True)
    papers = {p['id']: p for p in corpus['papers']}
    errors = []
    if len(papers) != 25 or len(cases) != 20:
        errors.append('unexpected_paper_or_case_count')
    if len({c.get('id') for c in cases}) != len(cases):
        errors.append('duplicate_case_ids')
    coverage = set()
    page_cache = {}
    if verify_anchors:
        from pypdf import PdfReader
    for paper in papers.values():
        path = source / paper['name']
        if not path.is_file() or path.is_symlink():
            errors.append('paper_missing:' + paper['id']); continue
        if digest(path) != paper['sha256']:
            errors.append('paper_hash_mismatch:' + paper['id'])
    for case in cases:
        if case.get('split') not in {'dev', 'holdout'} or not case.get('key_points'):
            errors.append('invalid_case:' + str(case.get('id')))
        coverage.update(case['paper_ids'])
        for point in case['key_points']:
            for ev in point['evidence']:
                paper = papers.get(ev['paper_id'])
                if not paper or ev['paper_id'] not in case['paper_ids']:
                    errors.append('invalid_paper_ref:' + case['id']); continue
                if not 1 <= ev['pdf_page'] <= paper['pages']:
                    errors.append('invalid_pdf_page:' + point['id']); continue
                if verify_anchors:
                    key = (ev['paper_id'], ev['pdf_page'])
                    path = source / paper['name']
                    if not path.is_file(): continue
                    if key not in page_cache:
                        page_cache[key] = re.sub(r'\s+', ' ', PdfReader(path).pages[ev['pdf_page']-1].extract_text() or '').strip()
                    if ev['anchor'] not in page_cache[key]:
                        errors.append('anchor_not_found:' + point['id'])
    if coverage != set(papers): errors.append('incomplete_corpus_coverage')
    dev = {p for c in cases if c['split'] == 'dev' for p in c['paper_ids']}
    holdout = {p for c in cases if c['split'] == 'holdout' for p in c['paper_ids']}
    if dev & holdout: errors.append('split_paper_overlap:' + ','.join(sorted(dev & holdout)))
    return {'valid': not errors, 'errors': errors, 'papers': len(papers), 'cases': len(cases),
            'points': sum(len(c['key_points']) for c in cases), 'source': str(source),
            'gold_status': 'provisional_needs_human_review', 'dev_papers': len(dev), 'holdout_papers': len(holdout)}


class WarningCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records = []
    def emit(self, record):
        message = record.getMessage()
        if 'form XObject invocations' in message:
            code = 'pypdf_xobject_limit_content_may_be_skipped'
        elif 'fontTools is required' in message:
            code = 'pypdf_fonttools_missing_encoding_may_be_incomplete'
        elif 'Skipping broken line' in message:
            code = 'pypdf_font_mapping_lines_skipped'
        else:
            code = 'pypdf_extraction_warning_check_original'
        if code not in self.records:
            self.records.append(code)


def prepare(source=DEFAULT_SOURCE, target=DEFAULT_BASE):
    source = Path(source).expanduser().resolve(strict=True)
    target = Path(target).expanduser().resolve()
    mapping = target.with_name(target.stem + '-map.json')
    if target.exists() or mapping.exists(): raise ValueError('corpus_database_or_map_exists: use a new path')
    validation = validate(source)
    if not validation['valid']: raise ValueError('invalid_corpus: ' + ','.join(validation['errors']))
    corpus, _ = load_data()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.building')
    if temporary.exists(): raise ValueError('corpus_build_in_progress_or_interrupted')
    # Exclusive reservation; failed builds are preserved for inspection, never reused.
    temporary.touch(exist_ok=False)
    store = Store(temporary)
    capture = WarningCapture(); logger = logging.getLogger('pypdf'); logger.addHandler(capture)
    document_ids, extraction_warnings = {}, {}
    try:
        project = store.create_project('Astrion Research Eval v1')
        for paper in corpus['papers']:
            capture.records.clear()
            path = source / paper['name']
            identifier = store.add_document(project, path)
            document_ids[paper['id']] = identifier
            if capture.records or paper.get('known_extraction_warnings'):
                extraction_warnings[paper['id']] = list(dict.fromkeys(capture.records + paper.get('known_extraction_warnings', [])))
                row = store.db.execute('SELECT metadata FROM documents WHERE id=?', (identifier,)).fetchone()
                metadata = json.loads(row['metadata'])
                metadata.setdefault('warnings', []).append('pdf_extraction_warning_check_original')
                metadata['extraction_warning_messages'] = extraction_warnings[paper['id']]
                with store.db:
                    store.db.execute('UPDATE documents SET metadata=? WHERE id=?', (json.dumps(metadata, ensure_ascii=False), identifier))
            print(f"已导入 {len(document_ids)}/25：{paper['id']}", flush=True)
        assert len(store.documents(project)) == 25
    finally:
        logger.removeHandler(capture)
        store.close()
    # Hard-link creation is atomic and fails if a concurrent build created the
    # destination; unlike rename it cannot replace an existing corpus database.
    os.link(temporary, target)
    temporary.unlink()
    metadata = {'corpus_id': corpus['corpus_id'], 'project_id': project, 'document_ids': document_ids,
                'source': str(source), 'db': str(target), 'extraction_warnings': extraction_warnings}
    with mapping.open('x', encoding='utf-8') as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    return metadata
