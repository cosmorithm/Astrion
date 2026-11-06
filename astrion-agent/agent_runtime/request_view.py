"""按预算组装发送给模型的工具结果视图。"""

import copy
import json

from response_contract import finalization_messages


def recent_tool_request_view(memory, *, final_prompt=None, tools=None, token_counter=None, token_budget=None):
    """Restore newly observed results in a request-only view when the whole request fits.

    The journal and the compacted working history remain unchanged. A receipt or its
    preview is not evidence that the model has seen the archived source text.
    """
    history = copy.deepcopy(memory._history)
    # A later assistant response means that batch has already been delivered.
    # A user update may follow the batch without consuming it.
    latest = next((unit for unit in reversed(memory.units())
                   if unit[0].get('role') != 'user'), [])
    ids = {m['_seq'] for m in latest if m.get('role') == 'tool' and m.get('_reduced')}
    candidates = [m for m in history if m.get('_seq') in ids and m.get('_ref')]
    if not candidates:
        messages = memory.for_model()
        return (finalization_messages(messages, final_prompt) if final_prompt else messages), [], []
    warning = ("部分工具结果可能只是档案引用或预览，不代表已看到原文；"
               "不可据预览声称读过原文。"
               + ("当前不能调用工具，未看到的正文须说明限制。" if final_prompt
                  else "需要正文时请用 read_evidence 读取。"))

    def build(include_warning=True):
        messages = memory._messages(history, memory._summary)
        if include_warning:
            messages[0]['content'] += "\n\n" + warning
        return finalization_messages(messages, final_prompt) if final_prompt else messages

    restored, unavailable = [], []
    for message in candidates:
        reduced = message['_reduced']
        old_content = message['content']
        message['content'] = memory.archive.get(message['_ref'])['content']
        del message['_reduced']
        request = build()
        fits_chars = len(json.dumps(request, ensure_ascii=False)) <= memory.budget_chars
        fits_tokens = (token_counter is None or
                       token_counter.count_request(request, tools or []) <= token_budget)
        if fits_chars and fits_tokens:
            restored.append(message['_seq'])
        else:
            message['content'], message['_reduced'] = old_content, reduced
            unavailable.append(message['_seq'])
    return build(include_warning=bool(unavailable)), restored, unavailable
