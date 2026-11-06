"""Context compaction policy and event recording for the agent loop."""

import json


def maybe_compact_context(*, memory, history, task, llm, context_strategy,
                          selected_tools, strict_counter, input_budget_tokens,
                          event_step, emit, call_model, error_data,
                          context_constructor, summarize_prompt, perf_counter, dim):
    """Compact when needed without changing the loop's decision budget."""
    # Cheap five-layer cleanup remains available for oversized individual
    # results. A full step-based rebuild starts at the 80% watermark.
    local_request_tokens = strict_counter.count_request(memory.for_model(), selected_tools) if strict_counter else None
    if context_strategy == "ss" and input_budget_tokens is not None:
        # A measured token budget is the SS strategy's watermark. Mixing
        # it with the independent character threshold can immediately undo
        # a valid half-window rebuild and trigger another model call.
        full_rebuild = bool(memory.archive.path and len(history.steps()) >= 1
                            and local_request_tokens >= int(input_budget_tokens * 0.8))
        check_compaction = full_rebuild
    else:
        full_rebuild = bool(memory.archive.path and len(history.steps()) >= 1 and (
            memory.chars() >= int(memory.budget_chars * 0.8)
            or (input_budget_tokens is not None and local_request_tokens >= int(input_budget_tokens * 0.8))))
        check_compaction = memory.needs_compaction() or full_rebuild
    if check_compaction:
        before_chars = memory.chars()
        print(dim(f"  [记忆] 当前 {before_chars} 字符，预算 {memory.budget_chars}；检查五层压缩…"))
        emit("memory.compaction.started", step=event_step, payload={
            "before_chars": before_chars,
            "budget_chars": memory.budget_chars,
            "compactions_before": memory.compactions,
        })

        def summarize(transcript: str) -> str:
            return call_model(
                lambda: llm.summarize(transcript),
                purpose="compaction",
                step=event_step,
                input_metadata={"transcript_chars": len(transcript)},
                request_snapshot={"messages": [{"role": "system", "content": summarize_prompt},
                                               {"role": "user", "content": transcript}],
                                  "tools": []},
            )

        compaction_started = perf_counter()
        rebuild_error = None
        rebuild_detail = None
        try:
            compacted = False
            if full_rebuild and context_strategy == "ss":
                def context_model_call(purpose, request_messages):
                    return call_model(
                        lambda: llm.chat(request_messages, tools=[]), purpose=purpose,
                        step=event_step,
                        input_metadata={"message_count": len(request_messages),
                                        "message_chars": len(json.dumps(request_messages, ensure_ascii=False)),
                                        "tool_count": 0},
                        request_snapshot={"messages": request_messages, "tools": []},
                    )
                try:
                    constructor = context_constructor(memory, history, context_model_call,
                                                     model=getattr(llm, 'model', None),
                                                     usage=lambda: getattr(llm, 'last_usage', None),
                                                     token_counter=strict_counter)
                    rebuild_detail = constructor.rebuild(
                        task, tools=selected_tools, input_budget_tokens=input_budget_tokens
                    )
                    compacted = True
                except (Exception, SystemExit) as exc:
                    # Failed plans/summaries never replace the last good view.
                    rebuild_error = {'reason': str(exc)[:300], 'error_type': type(exc).__name__}
                    history.record_build({'status': 'failed', **rebuild_error})
                    print(dim(f"  [记忆] Step 重构未完成，沿用已有压缩：{type(exc).__name__}: {str(exc)[:300]}"))
            if not compacted and (memory.needs_compaction() or
                                  (full_rebuild and context_strategy == "legacy")):
                compacted = memory.compact(summarize,
                                           force=bool(full_rebuild and context_strategy == "legacy"))
        except BaseException as exc:
            emit("memory.compaction.failed", step=event_step, payload={
                "before_chars": before_chars,
                "after_chars": memory.chars(),
                "duration_seconds": perf_counter() - compaction_started,
                "error": error_data(exc),
                "layers": memory.layer_reports,
            })
            raise
        emit("memory.compaction.finished", step=event_step, payload={
            "compacted": compacted,
            "before_chars": before_chars,
            "after_chars": memory.chars(),
            "duration_seconds": perf_counter() - compaction_started,
            "compactions_after": memory.compactions,
            "reason": ("step_context_rebuild_failed" if rebuild_error and not compacted
                       else memory.compaction_reason),
            "rebuild_error": rebuild_error,
            "rebuild": rebuild_detail,
            "protected_evidence_count": memory.protected_evidence_count,
            "over_budget": memory.chars() > memory.budget_chars,
            "local_input_tokens": (strict_counter.count_request(memory.for_model(), selected_tools)
                                   if strict_counter else None),
            "local_input_budget_tokens": input_budget_tokens,
            "layers": memory.layer_reports,
            "archive_scope": memory.archive.scope,
        })
        if compacted:
            print(dim(f"  [记忆] 压缩后 {memory.chars()} 字符"))
        else:
            print(dim(f"  [记忆] 保留原记录：{memory.compaction_reason}"))
