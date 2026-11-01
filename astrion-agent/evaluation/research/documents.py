"""Human-readable question and answer sheets from the same authoritative JSON."""
from .dataset import load_data


def render_questions():
    corpus,cases=load_data();papers={p['id']:p for p in corpus['papers']}
    lines=['# Astrion Research Eval v1：试题', '', '25 篇原始论文；共 20 题。每题的证据须回到原始 PDF 核验。',
           '本文件不包含标准要点；开发集和保留集使用不同论文。', '']
    for case in cases:
        lines += [f"## {case['id']} · {case['type']} · {case['split']}", '', case['task'], '',
                  '指定论文：' + '；'.join(f"{pid} `{papers[pid]['name']}`" for pid in case['paper_ids']), '']
        if case.get('continuation'):
            lines += ['续写题分两次执行：',f"1. {case['continuation']['initial_task']}",f"2. {case['continuation']['follow_up']}",'']
    return '\n'.join(lines).rstrip()+'\n'


def render_key():
    corpus,cases=load_data();papers={p['id']:p for p in corpus['papers']}
    lines=['# Astrion Research Eval v1：标准要点草案', '',
           '**这些要点由项目准备过程依据原文提取编写；尚未全部由人工独立复核，不能当作已确认金标准。**',
           '页码为 PDF 文件物理页码，从 1 开始。锚点是提取文本中的短句；格式或断行可能不同，请回看原始页面。', '']
    for case in cases:
        lines += [f"## {case['id']} · {case['split']}", '', case['task'], '']
        for point in case['key_points']:
            lines.append(f"- **{point['id']}**：{point['expected']}")
            for e in point['evidence']:
                lines.append(f"  - 依据：`{papers[e['paper_id']]['name']}`，PDF 第 {e['pdf_page']} 页；锚点：`{e['anchor']}`")
        lines.append('')
    return '\n'.join(lines).rstrip()+'\n'


def render_corpus():
    corpus,_=load_data()
    lines=['# Astrion Research Eval v1：论文清单','','共 25 篇 PDF 原件；SHA-256 固定原文件版本。','','| 编号 | 原文件 | PDF 页数 | SHA-256 | 已知提取警告 |','| --- | --- | ---: | --- | --- |']
    for paper in corpus['papers']:
        warning=', '.join(paper.get('known_extraction_warnings',[])) or '—'
        lines.append(f"| {paper['id']} | {paper['name']} | {paper['pages']} | `{paper['sha256']}` | {warning} |")
    return '\n'.join(lines).rstrip()+'\n'
