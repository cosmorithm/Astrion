"""Independent post-run checks; never confuse citation location with truth."""
from evaluation.judges import JudgeRegistry


def execution(case, record, spec):
    result = record.get('result') or {}
    if result.get('status') not in {'completed', 'no_tool_call'} or not result.get('answer'):
        return {'status': 'fail', 'reason': '运行失败、尚未完成计划或没有回答', 'evidence': {'status': result.get('status')}}
    return {'status': 'pass', 'reason': '产生了回答；不代表任务或结论正确', 'evidence': {'status': result.get('status')}}


def citations(case, record, spec):
    audit = record.get('citation_audit') or {}
    state = audit.get('status')
    if state == 'locations_found':
        return {'status': 'pass', 'reason': '工具返回范围覆盖引用；不证明模型完整看到原文或语义得到支持', 'evidence': audit}
    if state == 'no_citations':
        return {'status': 'fail', 'reason': '没有可核验的行号引用', 'evidence': audit}
    if state == 'invalid_locations':
        return {'status': 'pending', 'reason': '部分行号未被工具返回范围覆盖；格式示例或否定性说明也可能被识别，需人工核对', 'evidence': audit}
    return {'status': 'unknown', 'reason': '没有可用引用检查'}


def human_review(case, record, spec):
    return {'status': 'pending', 'reason': '需要逐要点核对答案及原始 PDF；模型自述或关键词不等于正确'}


def registry():
    result = JudgeRegistry()
    result.register('execution', execution)
    result.register('citation_location', citations)
    result.register('human_semantic_review', human_review)
    return result
