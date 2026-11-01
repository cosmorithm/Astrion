"""Run Astrion over QASPER v0.3 and invoke its unmodified official evaluator."""
from __future__ import annotations
import contextlib
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import urllib.request
import uuid
from pathlib import Path
from time import perf_counter

from evaluation.research.runner import ALLOWED, code_version
from research.citations import covers_range
from research.runner import run_research
from research.store import Store

TRAIN_DEV = 'https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-train-dev-v0.3.tgz'
TEST_EVALUATOR = 'https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-test-and-evaluator-v0.3.tgz'
SPLITS = {'dev': 'qasper-dev-v0.3.json', 'test': 'qasper-test-v0.3.json'}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def prepare(dest):
    """Download official archives and extract only the two needed named files."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for url, archive_name, files in [
        (TRAIN_DEV, 'train-dev-v0.3.tgz', [SPLITS['dev']]),
        (TEST_EVALUATOR, 'test-evaluator-v0.3.tgz', [SPLITS['test'], 'qasper_evaluator.py']),
    ]:
        archive = dest / archive_name
        if not archive.exists():
            tmp = archive.with_suffix('.download')
            try:
                with urllib.request.urlopen(url, timeout=90) as response, tmp.open('wb') as out:
                    while chunk := response.read(1024 * 1024):
                        out.write(chunk)
                tmp.replace(archive)
            finally:
                tmp.unlink(missing_ok=True)
        with tarfile.open(archive, 'r:gz') as tar:
            for name in files:
                member = tar.getmember(name)
                if not member.isfile() or member.size > 100_000_000:
                    raise ValueError('invalid_official_archive_member')
                data = tar.extractfile(member).read()
                path = dest / name
                if path.exists() and path.read_bytes() != data:
                    raise ValueError('existing_qasper_file_differs:' + name)
                if not path.exists(): path.write_bytes(data)
    return {name: sha256(dest / name) for name in [*SPLITS.values(), 'qasper_evaluator.py']}


def select(data, count, offset=0):
    if type(count) is not int or count < 1 or type(offset) is not int or offset < 0:
        raise ValueError('count must be positive and offset nonnegative')
    # Take one question per paper, in official release order, without examining answers.
    chosen = [(pid, paper, paper['qas'][0]) for pid, paper in data.items() if paper.get('qas')][offset:offset+count]
    if len(chosen) != count:
        raise ValueError('not_enough_papers')
    return chosen


def paper_text(paper):
    lines = [paper['title'].replace('\n', ' '), paper.get('abstract', '').replace('\n', ' ')]
    paragraphs = {}
    for section in paper['full_text']:
        lines.append('# ' + (section.get('section_name') or 'Untitled').replace('\n', ' '))
        for paragraph in section.get('paragraphs', []):
            if paragraph.strip():
                lines.append(' '.join(paragraph.splitlines()))
                paragraphs[len(lines)] = paragraph
    # QASPER supplies captions, not the full contents of figures/tables.
    for figure in paper.get('figures_and_tables', []):
        caption = figure.get('caption') or ''
        if caption.strip(): lines.append('Figure/table caption: ' + ' '.join(caption.splitlines()))
    return '\n'.join(lines), paragraphs


def prediction(answer, doc_id, observed, paragraphs):
    """Convert cited and actually observed complete paragraphs into QASPER evidence."""
    answer = answer or ''
    matches = re.findall(r'\[D(\d+):L(\d+)(?:-L(\d+))?\]', answer)
    selected = set()
    for doc, start, end in matches:
        doc, start, end = int(doc), int(start), int(end or start)
        if doc != doc_id or not covers_range(observed, doc, start, end): continue
        for n, paragraph in paragraphs.items():
            if start <= n <= end and covers_range(observed, doc, n, n):
                selected.add(n)
    # The official scorer needs a short answer rather than citation markup.
    clean = re.sub(r'\[D\d+:L\d+(?:-L\d+)?\]', '', answer)
    clean = re.split(r'(?im)^\s*(?:EVIDENCE|REFERENCES|证据|引用)\s*[:：]', clean, maxsplit=1)[0]
    clean = re.sub(r'^\s*(?:ANSWER|答案)\s*[:：]\s*', '', clean, flags=re.I).strip()
    return clean, [paragraphs[n] for n in sorted(selected)]


def run(data_dir, output, factory, *, count=2, offset=0, split='dev', steps=10, workers=3, worker_steps=6, budget_chars=12000):
    if split not in SPLITS: raise ValueError('unknown_split')
    data_dir, output = Path(data_dir), Path(output)
    path, evaluator = data_dir / SPLITS[split], data_dir / 'qasper_evaluator.py'
    data = json.loads(path.read_text(encoding='utf-8'))
    cases = select(data, count, offset)
    suite = output / ('qasper-' + uuid.uuid4().hex)
    suite.mkdir(parents=True, exist_ok=False)
    gold, predictions, reports = {}, [], []
    for index, (paper_id, paper, qa) in enumerate(cases, 1):
        case_path = suite / f'case-{index:03d}'
        case_path.mkdir()
        store = Store(case_path / 'research.sqlite3')
        project = store.create_project('QASPER isolated ' + str(index))
        text, paragraphs = paper_text(paper)
        doc = store.add_document_text(project, 'paper.txt', text, source='QASPER v0.3 ' + paper_id)
        task = ('Answer the following question using only the supplied paper. '
                'Read evidence before answering. Output a short answer starting with ANSWER: '
                'and cite the supporting source lines as [D1:Lx-Ly]. If the paper cannot answer, '
                'answer Unanswerable. Do not infer facts not supported by the paper.\n\nQuestion: ' + qa['question'])
        gold[paper_id] = {**paper, 'qas': [qa]}
        print(f'[{index}/{len(cases)}] QASPER {qa["question_id"]} running', flush=True)
        started = perf_counter()
        llm = factory()
        try:
            with (case_path / 'console.log').open('w', encoding='utf-8') as log, contextlib.redirect_stdout(log):
                record = run_research(store, project, task, llm, max_steps=steps, max_workers=workers,
                                      worker_steps=worker_steps, budget_chars=budget_chars, tool_allowlist=ALLOWED)
            raw = (record.get('result') or {}).get('answer') or ''
            result, evidence = prediction(raw, doc, record.get('observed_ranges') or [], paragraphs)
            pred = {'question_id': qa['question_id'], 'predicted_answer': result, 'predicted_evidence': evidence}
            predictions.append(pred)
            reports.append({'paper_id': paper_id, 'question_id': qa['question_id'], 'status': record.get('result', {}).get('status'),
                'answer': raw, 'predicted_answer': result, 'evidence_paragraphs': len(evidence),
                'citation_audit': record.get('citation_audit'), 'observed_ranges': record.get('observed_ranges'),
                'duration_seconds': record.get('duration_seconds'), 'usage': record.get('usage'),
                'workers': len(record.get('workers') or []), 'run_id': record.get('run_id'), 'workspace': str(case_path)})
        except Exception as exc:
            reports.append({'paper_id': paper_id, 'question_id': qa['question_id'], 'status': 'error',
                            'error_type': type(exc).__name__, 'duration_seconds': perf_counter()-started,
                            'workspace': str(case_path)})
        finally: store.close()
        (suite / 'cases.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    (suite/'gold.json').write_text(json.dumps(gold, ensure_ascii=False), encoding='utf-8')
    (suite/'predictions.jsonl').write_text(''.join(json.dumps(p, ensure_ascii=False)+'\n' for p in predictions), encoding='utf-8')
    scored = subprocess.run([sys.executable, str(evaluator.resolve()), '--gold', str((suite/'gold.json').resolve()),
                             '--predictions', str((suite/'predictions.jsonl').resolve())], capture_output=True, text=True, check=True)
    result = {'benchmark':'QASPER', 'version':'0.3', 'split':split, 'sample_count':count, 'selection':f'first question per paper, official order, offset={offset}',
        'dataset_sha256':sha256(path), 'official_evaluator_sha256':sha256(evaluator), 'code_version':code_version(),
        'settings':{'steps':steps,'workers':workers,'worker_steps':worker_steps,'budget_chars':budget_chars},
        'scoring':'unmodified official evaluator, adapted answer and cited+observed paragraph evidence',
        'scope':'QASPER structured text; captions included, figure/table visual content not provided',
        'metrics':json.loads(scored.stdout), 'cases':reports}
    (suite/'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return suite, result


def merge(suites, evaluator, output):
    """Aggregate compatible, non-overlapping runs using the official evaluator."""
    import importlib.util
    suites = [Path(p) for p in suites]
    if not suites: raise ValueError('at least one suite required')
    evaluator = Path(evaluator)
    fingerprint = sha256(evaluator)
    combined_gold, combined_pred, all_cases, seen = {}, {}, [], set()
    models = set()
    baseline = None
    for suite in suites:
        report = json.loads((suite/'summary.json').read_text(encoding='utf-8'))
        identity = (report['version'], report['split'], report['dataset_sha256'], report['official_evaluator_sha256'], report['settings'])
        if report['benchmark'] != 'QASPER' or report['official_evaluator_sha256'] != fingerprint:
            raise ValueError('mismatched_evaluator')
        if baseline is None: baseline = identity
        if identity != baseline: raise ValueError('incompatible_suite_settings')
        gold = json.loads((suite/'gold.json').read_text(encoding='utf-8'))
        for paper_id, paper in gold.items():
            if paper_id in combined_gold: raise ValueError('duplicate_paper')
            combined_gold[paper_id] = paper
            for qa in paper['qas']:
                if qa['question_id'] in seen: raise ValueError('duplicate_question')
                seen.add(qa['question_id'])
        for line in (suite/'predictions.jsonl').read_text(encoding='utf-8').splitlines():
            row=json.loads(line); qid=row['question_id']
            if qid in combined_pred: raise ValueError('duplicate_prediction')
            combined_pred[qid]={'answer':row['predicted_answer'],'evidence':row['predicted_evidence']}
        for case in report['cases']:
            events = Path(case['workspace']) / 'runs'
            for event_file in events.rglob('events.jsonl'):
                with event_file.open(encoding='utf-8') as f:
                    for line in f:
                        event = json.loads(line).get('event') or {}
                        if event.get('type') == 'model.call.started':
                            model = (event.get('payload') or {}).get('model')
                            if model: models.add(model)
            all_cases.append(case)
    spec=importlib.util.spec_from_file_location('qasper_official_evaluator',evaluator)
    official=importlib.util.module_from_spec(spec);spec.loader.exec_module(official)
    references=official.get_answers_and_evidence(combined_gold,False)
    if set(references) != seen or set(combined_pred)-seen: raise ValueError('invalid_predictions_or_gold')
    if len(models) != 1: raise ValueError('missing_or_mixed_models')
    metrics=official.evaluate(references,combined_pred)
    for case in all_cases:
        qid=case['question_id']
        grade=official.evaluate({qid:references[qid]}, {qid:combined_pred[qid]} if qid in combined_pred else {})
        case['official_metrics']={'Answer F1':grade['Answer F1'],'Evidence F1':grade['Evidence F1']}
    duration=sum(c.get('duration_seconds') or 0 for c in all_cases)
    token_values=[(c.get('usage') or {}).get('totals',{}).get('total_tokens') for c in all_cases]
    result={'benchmark':'QASPER','version':baseline[0],'split':baseline[1], 'dataset_sha256':baseline[2],
            'official_evaluator_sha256':baseline[3], 'settings':baseline[4], 'model':next(iter(models)), 'sample_count':len(seen),
            'selection':'first question per paper, merged independent official-order samples; see source suites',
            'metrics':metrics,'cases':all_cases,'duration_seconds':duration,
            'known_total_tokens':sum(x for x in token_values if x is not None),
            'unknown_token_runs':sum(x is None or (c.get('usage') or {}).get('unknown_calls') != 0 for x,c in zip(token_values,all_cases)),
            'source_suites':[str(p.resolve()) for p in suites],
            'note':'Pilot sample only. Answer F1 is token overlap; evidence F1 uses cited, observed paragraphs. Neither is semantic correctness.'}
    target=Path(output)/('qasper-merged-'+uuid.uuid4().hex)
    target.mkdir(parents=True,exist_ok=False)
    (target/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return target,result
