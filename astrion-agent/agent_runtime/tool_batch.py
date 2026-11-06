"""Execute one batch of model-requested tools and archive their observations."""

from memory_evidence import extract_tool_evidence
from tooling import ToolCall, ToolResult


def execute_tool_batch(calls, *, registry, memory, emit, step, dim, cyan, red):
    """Return terminal status and answer after pairing all calls with results.

    Callbacks let the main loop retain ownership of events and presentation.
    Checkpointing and run termination remain the caller's responsibility.
    """
    terminal_answer = None
    terminal_status = None
    for call in calls:
        name = call["function"]["name"]
        tool_call_id = call["id"]

        # finish 之后的调用不执行，但仍需写回结果，保持这批消息配对完整。
        if terminal_status is not None:
            skipped = ToolResult(tool_call_id, name, "skipped", "ERROR: 已调用终止工具，此工具未执行",
                                 error={"code": "skipped_after_terminal", "message": "此工具未执行"})
            observation = skipped.content
            print(f"  {cyan('act:  ')} {name}（跳过）")
            print(f"  {red('obs:  ')} {observation}")
            emit("tool.call.finished", step=step, payload={
                "tool_call_id": tool_call_id,
                "name": name,
                "outcome": skipped.outcome,
                "duration_seconds": None,
                "content": observation,
                "raw_arguments": call["function"].get("arguments"),
                "error": skipped.error,
            })
            memory.add_tool(tool_call_id, observation, outcome='skipped')
            continue

        raw_args = call["function"].get("arguments")
        if raw_args is None or raw_args == "":
            raw_args = "{}"
        print(f"  {cyan('act:  ')} {name}({raw_args})")

        common = {
            "tool_call_id": tool_call_id,
            "name": name,
        }
        result = registry.execute(
            ToolCall(tool_call_id, name, raw_args),
            on_start=lambda args: emit("tool.call.started", step=step,
                                       payload={**common, "arguments": args}),
        )
        observation = result.content
        print(f"  {red('obs:  ') if result.outcome != 'success' else dim('obs:  ')} {observation}")
        terminal_payload = {**common, "outcome": result.outcome,
                            "duration_seconds": result.duration_seconds,
                            "content": observation}
        if result.outcome == "rejected":
            terminal_payload["raw_arguments"] = raw_args
        if result.error is not None:
            terminal_payload["error"] = result.error
        emit("tool.call.finished", step=step, payload=terminal_payload)
        memory.add_tool(tool_call_id, observation, evidence=(
            extract_tool_evidence(name, observation) if result.outcome == "success" else None
        ), outcome=result.outcome)
        if result.outcome == "success" and name in {"finish", "ask_user"}:
            terminal_status = "completed" if name == "finish" else "needs_input"
            terminal_answer = observation

    return terminal_status, terminal_answer
